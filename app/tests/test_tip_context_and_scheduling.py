"""Tests for tip sourcing, novelty comparison, and pipeline scheduling.

Covers the content-facing P1 items: tips are now drawn from the aggregated news pool
rather than three rotating presets, the novelty check sees tip bodies, and the
scheduler runs on a cadence that matches what the site promises.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.aggregator import AggregatedContent
from app.models.content import Tip
from app.services.aggregator import AggregationResult
from app.services.pipeline_scheduler import (
    JobConfig,
    PipelineScheduleStore,
    PipelineScheduler,
    create_pipeline_scheduler,
)
from app.services.tip_context import DailyTipContextProvider
from app.services.tip_editor import TipEditorAgent

NOW = datetime(2026, 3, 4, 9, 0, tzinfo=timezone.utc)


def _item(title: str, summary: str = "", *, url: str = "", topic: str | None = None) -> AggregatedContent:
    return AggregatedContent(
        title=title,
        url=url or f"https://example.test/{title.lower().replace(' ', '-')}",
        summary=summary,
        published_at=NOW,
        source="Test Feed",
        topic=topic,
    )


class _StubAggregator:
    def __init__(self, items=None, errors=None, error: Exception | None = None) -> None:
        self.items = items or []
        self.errors = errors or []
        self.error = error
        self.calls: list[int] = []

    def gather(self, *, limit_per_feed: int = 5) -> AggregationResult:
        self.calls.append(limit_per_feed)
        if self.error is not None:
            raise self.error
        return AggregationResult(items=list(self.items), errors=list(self.errors))


@pytest.fixture(autouse=True)
def _no_preset_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVEON_TIP_CONTEXT_PRESETS", raising=False)


# ----------------------------------------------------------------------
# Tip context now comes from the news pool
# ----------------------------------------------------------------------


def test_context_is_built_from_aggregated_news() -> None:
    aggregator = _StubAggregator(
        [
            _item("Grip strength predicts healthspan", "A cohort study links grip to mortality.", topic="research"),
            _item("Fibre and the gut", "More fibre, steadier glucose.", topic="research"),
        ]
    )
    provider = DailyTipContextProvider(aggregator=aggregator, now_provider=lambda: NOW)

    context = provider.build()

    assert "Grip strength predicts healthspan" in context.notes_block()
    assert "cohort study" in context.notes_block()
    assert "https://example.test/grip-strength-predicts-healthspan" in context.sources_block()
    assert context.theme == "research"
    assert context.current_date == NOW.date()


def test_notes_are_limited_to_the_configured_count() -> None:
    aggregator = _StubAggregator([_item(f"Story {i}") for i in range(10)])
    provider = DailyTipContextProvider(aggregator=aggregator, now_provider=lambda: NOW, note_count=3)

    context = provider.build()

    assert len(context.notes) == 3


def test_feed_markup_is_stripped_from_notes() -> None:
    aggregator = _StubAggregator(
        [_item("Sleep study", "<p>Deep sleep &amp; memory<br/>consolidation</p>")]
    )
    provider = DailyTipContextProvider(aggregator=aggregator, now_provider=lambda: NOW)

    notes = provider.build().notes_block()

    assert "<p>" not in notes
    assert "&amp;" not in notes
    assert "Deep sleep & memory consolidation" in notes


def test_a_very_long_note_is_trimmed() -> None:
    aggregator = _StubAggregator([_item("Long story", "x" * 2000)])
    provider = DailyTipContextProvider(aggregator=aggregator, now_provider=lambda: NOW)

    note = provider.build().notes[0]

    assert len(note) <= 321
    assert note.endswith("…")


def test_presets_are_used_when_no_aggregator_is_configured() -> None:
    provider = DailyTipContextProvider(now_provider=lambda: NOW)

    context = provider.build()

    assert context.notes
    assert context.theme in {"Strength snacks", "Circadian-friendly light", "Nutrient timing"}


def test_presets_are_used_when_aggregation_fails() -> None:
    """A feed outage must degrade to offline notes, not abort the run."""

    aggregator = _StubAggregator(error=RuntimeError("network down"))
    provider = DailyTipContextProvider(aggregator=aggregator, now_provider=lambda: NOW)

    context = provider.build()

    assert context.notes
    assert context.theme in {"Strength snacks", "Circadian-friendly light", "Nutrient timing"}


def test_presets_are_used_when_aggregation_returns_nothing() -> None:
    provider = DailyTipContextProvider(aggregator=_StubAggregator([]), now_provider=lambda: NOW)

    assert provider.build().notes


def test_news_backed_context_varies_with_the_news() -> None:
    """The preset rotation repeated every third day regardless of the world."""

    monday = DailyTipContextProvider(
        aggregator=_StubAggregator([_item("Monday finding")]), now_provider=lambda: NOW
    ).build()
    thursday = DailyTipContextProvider(
        aggregator=_StubAggregator([_item("Thursday finding")]),
        now_provider=lambda: NOW + timedelta(days=3),
    ).build()

    assert monday.notes != thursday.notes


def test_feed_limit_is_passed_through() -> None:
    aggregator = _StubAggregator([_item("Story")])
    DailyTipContextProvider(aggregator=aggregator, now_provider=lambda: NOW, feed_limit=9).build()

    assert aggregator.calls == [9]


# ----------------------------------------------------------------------
# Novelty comparison sees bodies, not just titles
# ----------------------------------------------------------------------


def test_existing_tip_bodies_reach_the_novelty_prompt() -> None:
    tips = [
        Tip(id="1", title="Walk after meals", content_body="A 10-minute walk blunts glucose spikes."),
        Tip(id="2", title="Lift twice weekly", content_body="Two sessions preserve muscle mass."),
    ]

    rendered = TipEditorAgent._format_existing_titles(tips)

    assert "Walk after meals" in rendered
    assert "blunts glucose spikes" in rendered
    assert "preserve muscle mass" in rendered


def test_existing_tip_bodies_are_trimmed() -> None:
    tips = [Tip(id="1", title="Long tip", content_body="y" * 1000)]

    rendered = TipEditorAgent._format_existing_titles(tips)

    assert len(rendered) < 400
    assert "…" in rendered


def test_no_existing_tips_tells_the_editor_not_to_claim_repetition() -> None:
    """With an empty list the model invented "repetitive of recent tips" rejections."""

    rendered = TipEditorAgent._format_existing_titles([])

    assert "No tips have been published yet" in rendered
    assert "Do not claim repetition" in rendered


def test_plain_strings_are_still_accepted() -> None:
    assert "Older tip" in TipEditorAgent._format_existing_titles(["Older tip"])


# ----------------------------------------------------------------------
# Scheduling
# ----------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> PipelineScheduleStore:
    return PipelineScheduleStore(tmp_path / "schedule.db")


def _job(name: str = "tips", days: int = 1) -> JobConfig:
    return JobConfig(name=name, runner=lambda _at: True, interval_days=days)


def test_a_job_that_never_ran_is_due_immediately(store: PipelineScheduleStore) -> None:
    """A fresh install used to publish nothing for a week (articles) or a month (tips)."""

    assert store.is_due(_job(), NOW) is True


def test_checking_due_does_not_start_the_clock(store: PipelineScheduleStore) -> None:
    job = _job()
    store.is_due(job, NOW)

    # The old implementation stamped last_run here, deferring the first real run.
    assert store.get_last_run(job.name) is None
    assert store.is_due(job, NOW) is True


def test_a_recent_run_is_not_due(store: PipelineScheduleStore) -> None:
    job = _job()
    store.set_last_run(job.name, NOW)

    assert store.is_due(job, NOW + timedelta(hours=6)) is False


def test_an_elapsed_interval_is_due(store: PipelineScheduleStore) -> None:
    job = _job()
    store.set_last_run(job.name, NOW)

    assert store.is_due(job, NOW + timedelta(days=1, minutes=1)) is True


def test_next_due_is_unknown_before_the_first_run(store: PipelineScheduleStore) -> None:
    assert store.next_due(_job()) is None


def test_default_cadences_match_the_product(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The homepage promises a "Tip of the Day"; tips used to run monthly."""

    monkeypatch.delenv("LIVEON_DISABLE_SCHEDULER", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("LIVEON_ARTICLE_INTERVAL_DAYS", raising=False)
    monkeypatch.delenv("LIVEON_TIP_INTERVAL_DAYS", raising=False)
    monkeypatch.delenv("LIVEON_TIP_INTERVAL_MONTHS", raising=False)
    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))

    scheduler = create_pipeline_scheduler()

    assert scheduler is not None
    jobs = {job["name"]: job for job in scheduler.describe_jobs()}
    assert set(jobs) == {"articles", "tips"}
    # Never run, so both are due right away.
    assert all(job["next_run"] is None for job in jobs.values())


def test_monthly_tips_remain_configurable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("LIVEON_DISABLE_SCHEDULER", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("LIVEON_TIP_INTERVAL_MONTHS", "1")
    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))

    scheduler = create_pipeline_scheduler()

    assert scheduler is not None
    assert "tips" in scheduler.job_names


# ----------------------------------------------------------------------
# Manual triggering
# ----------------------------------------------------------------------


@pytest.mark.parametrize("succeeds", [True, False])
def test_trigger_runs_a_job_and_records_only_success(
    store: PipelineScheduleStore, succeeds: bool
) -> None:
    import asyncio

    calls: list[datetime] = []

    def runner(at: datetime) -> bool:
        calls.append(at)
        return succeeds

    job = JobConfig(name="tips", runner=runner, interval_days=1)
    scheduler = PipelineScheduler(store, [job])

    async def scenario() -> bool:
        accepted = await scheduler.trigger("tips")
        # Let the background task run to completion.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if calls:
                break
        await asyncio.sleep(0.05)
        return accepted

    accepted = asyncio.run(scenario())

    assert accepted is True
    assert len(calls) == 1
    assert (store.get_last_run("tips") is not None) is succeeds


def test_trigger_rejects_an_unknown_job(store: PipelineScheduleStore) -> None:
    import asyncio

    scheduler = PipelineScheduler(store, [_job()])

    assert asyncio.run(scheduler.trigger("nope")) is False


def test_describe_jobs_reports_run_times(store: PipelineScheduleStore) -> None:
    job = _job("articles")
    store.set_last_run("articles", NOW)
    scheduler = PipelineScheduler(store, [job])

    described = scheduler.describe_jobs()[0]

    assert described["name"] == "articles"
    assert described["last_run"] == NOW
    assert described["next_run"] == NOW + timedelta(days=1)
    assert described["running"] is False


# ----------------------------------------------------------------------
# Retries look at a different story
# ----------------------------------------------------------------------


def test_focused_rotates_the_notes() -> None:
    from app.models.tip_context import TipGenerationContext

    context = TipGenerationContext(notes=["a", "b", "c"])

    assert context.focused(0).notes == ["a", "b", "c"]
    assert context.focused(1).notes == ["b", "c", "a"]
    assert context.focused(2).notes == ["c", "a", "b"]
    # Wraps rather than running off the end.
    assert context.focused(4).notes == ["b", "c", "a"]


def test_focused_is_safe_with_no_notes() -> None:
    from app.models.tip_context import TipGenerationContext

    assert TipGenerationContext(notes=[]).focused(2).notes == []


def test_focused_preserves_the_rest_of_the_context() -> None:
    from app.models.tip_context import TipGenerationContext

    context = TipGenerationContext(
        notes=["a", "b"], sources=["https://s"], theme="research", guidance="do it"
    )
    rotated = context.focused(1)

    assert rotated.sources == ["https://s"]
    assert rotated.theme == "research"
    assert rotated.guidance == "do it"


def test_each_retry_leads_with_a_different_story() -> None:
    """Re-sending the same ordering made the generator repeat the rejected tip."""

    from app.models.tip import TipDraft
    from app.models.tip_context import TipGenerationContext
    from app.models.tip_editor import TipReviewResult
    from app.services.pipeline import TipPipeline

    context = TipGenerationContext(notes=["story-one", "story-two", "story-three"])
    seen_first_notes: list[str] = []

    class _Generator:
        def generate(self, *, context, feedback=None, published_tips=()):
            seen_first_notes.append(context.notes[0])
            return TipDraft(title="T", body="B")

    class _Editor:
        def __init__(self) -> None:
            self.calls = 0

        def review(self, draft, existing_tips):
            self.calls += 1
            approved = self.calls >= 3
            return TipReviewResult(
                is_approved=approved, feedback=None if approved else "too repetitive"
            )

    class _Repo:
        def get_latest_tips(self, *, limit=5):
            return []

    class _Publisher:
        def publish(self, draft, *, published_at=None):
            from app.services.tip_publisher import TipPublicationResult

            return TipPublicationResult(tip=Tip(title=draft.title, content_body=draft.body), created=True)

    class _Context:
        def build(self):
            return context

    result = TipPipeline(
        context_provider=_Context(),
        generator=_Generator(),
        editor=_Editor(),
        publisher=_Publisher(),
        repository=_Repo(),
    ).run()

    assert result.succeeded
    assert seen_first_notes == ["story-one", "story-two", "story-three"]
