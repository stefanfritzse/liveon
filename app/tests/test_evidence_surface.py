"""Tests for what a reader sees, and for the run log behind it.

Item 8 asks that a reader can tell an interesting early mouse result from a well-supported
human recommendation without reading the paper. Item 10 asks that six months later we can
reconstruct why the system published something. Both are checked here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.models.content import Article, Tip
from app.services.evidence.runlog import RunLog, retention_days
from app.services.sqlite_repo import LocalSQLiteContentRepository, hide_legacy_content

NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


def _article(**overrides) -> Article:
    defaults = dict(
        title="An eight-hour window",
        content_body="A randomised trial reported lower mortality.",
        summary="A trial of time-restricted eating.",
        published_date=NOW,
        source_urls=["https://doi.org/10.1001/jama.2026.1"],
        tags=["nutrition"],
        evidence_bundle_id="bundle-1",
        evidence_keys=["doi:10.1001/jama.2026.1"],
        evidence_grade="moderate",
        evidence_summary="Moderate — 1 human randomised trial",
        evidence_limitations=["Single trial", "Surrogate endpoint"],
    )
    defaults.update(overrides)
    return Article(**defaults)


def _legacy_article(**overrides) -> Article:
    defaults = dict(
        title="An older piece",
        content_body="Written before the evidence layer existed.",
        published_date=NOW - timedelta(days=200),
    )
    defaults.update(overrides)
    return Article(**defaults)


# -- the published surface ---------------------------------------------


def test_the_grade_and_limitations_survive_storage(tmp_path: Path) -> None:
    repository = LocalSQLiteContentRepository(tmp_path / "content.db")

    stored = repository.save_article(_article())
    reloaded = repository.get_article(stored.id)

    assert reloaded is not None
    assert reloaded.evidence_summary == "Moderate — 1 human randomised trial"
    assert reloaded.evidence_limitations == ["Single trial", "Surrogate endpoint"]


def test_an_article_page_shows_its_evidence(tmp_path: Path, monkeypatch) -> None:
    """A reader should not have to interpret the paper to know how settled this is."""

    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))
    repository = LocalSQLiteContentRepository(tmp_path / "content.db")
    stored = repository.save_article(_article())

    from app import main

    monkeypatch.setattr(main, "get_repository", lambda: repository)
    with TestClient(main.app) as client:
        page = client.get(f"/articles/{stored.id}").text

    assert "Moderate — 1 human randomised trial" in page
    assert "What this does not show" in page
    assert "Single trial" in page
    assert "Studies cited" in page


def test_a_legacy_article_says_it_was_not_assessed(tmp_path: Path, monkeypatch) -> None:
    """Grandfathered, not retro-graded: we do not know, and we say so."""

    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))
    repository = LocalSQLiteContentRepository(tmp_path / "content.db")
    stored = repository.save_article(_legacy_article())

    from app import main

    monkeypatch.setattr(main, "get_repository", lambda: repository)
    with TestClient(main.app) as client:
        page = client.get(f"/articles/{stored.id}").text

    assert "published before evidence review" in page
    assert "Moderate" not in page


def test_the_article_list_carries_a_compact_badge(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))
    repository = LocalSQLiteContentRepository(tmp_path / "content.db")
    repository.save_article(_article())
    repository.save_article(_legacy_article())

    from app import main

    monkeypatch.setattr(main, "get_repository", lambda: repository)
    with TestClient(main.app) as client:
        page = client.get("/articles").text

    assert "Evidence: Moderate" in page
    assert "Evidence: not assessed" in page


def test_a_tip_page_carries_its_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))
    repository = LocalSQLiteContentRepository(tmp_path / "content.db")
    repository.save_tip(
        Tip(
            title="Try an eight-hour window",
            content_body="Keeping meals inside eight hours was studied in a trial.",
            published_date=NOW,
            evidence_bundle_id="bundle-1",
            evidence_grade="preliminary",
            evidence_summary="Preliminary — 1 animal study",
        )
    )

    from app import main

    monkeypatch.setattr(main, "get_repository", lambda: repository)
    with TestClient(main.app) as client:
        page = client.get("/tips").text

    assert "Preliminary — 1 animal study" in page


# -- hiding the archive ------------------------------------------------


def test_legacy_content_is_kept_by_default() -> None:
    assert hide_legacy_content() is False


def test_legacy_content_can_be_hidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = LocalSQLiteContentRepository(tmp_path / "content.db")
    repository.save_article(_article())
    repository.save_article(_legacy_article())

    assert repository.browse_articles().total == 2

    monkeypatch.setenv("LIVEON_HIDE_LEGACY", "1")
    page = repository.browse_articles()

    # Filtered before the count, so pagination does not develop holes.
    assert page.total == 1
    assert page.items[0].evidence_assessed is True


# -- the run log -------------------------------------------------------


@pytest.fixture()
def runlog(tmp_path: Path) -> RunLog:
    with RunLog(tmp_path / "content.db") as opened:
        yield opened


def test_a_run_records_its_identity_and_outcome(runlog: RunLog) -> None:
    run_id = runlog.start("articles", now=NOW)

    runlog.finish(
        run_id,
        "published",
        model_id="qwen2.5:14b",
        prompt_versions={"review": "1"},
        data={"grade": "moderate"},
        now=NOW + timedelta(minutes=4),
    )

    record = runlog.get(run_id)
    assert record is not None
    assert record.job == "articles"
    assert record.outcome == "published"
    assert record.model_id == "qwen2.5:14b"
    assert record.prompt_versions == {"review": "1"}
    assert record.data["grade"] == "moderate"
    assert record.finished_at == NOW + timedelta(minutes=4)


def test_events_are_ordered_and_carry_their_payloads(runlog: RunLog) -> None:
    run_id = runlog.start("articles", now=NOW)

    runlog.event(run_id, "rank", "candidates", [{"topic": "sleep", "score": 4.2}], now=NOW)
    runlog.event(run_id, "review", "decision", {"status": "approved"}, now=NOW)

    events = runlog.events(run_id)
    assert [event["stage"] for event in events] == ["rank", "review"]
    assert events[0]["seq"] < events[1]["seq"]
    assert events[0]["data"][0]["topic"] == "sleep"
    assert events[1]["data"]["status"] == "approved"


def test_a_run_with_no_events_reads_back_empty(runlog: RunLog) -> None:
    run_id = runlog.start("tips", now=NOW)

    assert runlog.events(run_id) == []
    assert runlog.get("nonexistent") is None


def test_recent_runs_are_listed_newest_first(runlog: RunLog) -> None:
    older = runlog.start("articles", now=NOW - timedelta(days=2))
    newer = runlog.start("articles", now=NOW)
    runlog.start("tips", now=NOW - timedelta(days=1))

    assert [record.run_id for record in runlog.recent(job="articles")] == [newer, older]
    assert len(runlog.recent()) == 3


def test_old_runs_are_pruned(runlog: RunLog) -> None:
    stale = runlog.start("articles", now=NOW - timedelta(days=400))
    runlog.event(stale, "rank", "candidates", {"x": 1}, now=NOW - timedelta(days=400))
    fresh = runlog.start("articles", now=NOW)

    removed = runlog.prune(now=NOW, days=365)

    assert removed == 1
    assert runlog.get(stale) is None
    assert runlog.events(stale) == []
    assert runlog.get(fresh) is not None


def test_the_retention_window_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert retention_days() == 365

    monkeypatch.setenv("LIVEON_RUN_RETENTION_DAYS", "30")
    assert retention_days() == 30

    monkeypatch.setenv("LIVEON_RUN_RETENTION_DAYS", "nonsense")
    assert retention_days() == 365


def test_the_three_timestamps_stay_apart(tmp_path: Path) -> None:
    """A write-up of an old study must not claim the study's date as its own."""

    from app.models.evidence import Classification, EvidenceRecord

    record = EvidenceRecord(
        source_key="doi:10.1/x",
        source_published_at=datetime(2023, 5, 1, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        classification=Classification(design="rct", subject="human"),
    )
    article = _article(published_date=NOW)

    assert record.source_published_at.year == 2023
    assert record.retrieved_at.year == 2026
    assert article.published_date == NOW
    assert article.published_date != record.source_published_at


def test_the_pipeline_writes_a_reconstructable_run(tmp_path: Path) -> None:
    """The end of item 10: why did it publish that, and what produced the decision."""

    import json
    from typing import Any

    from langchain_core.messages import AIMessage

    from app.services.evidence.extractor import ExtractorAgent
    from app.services.evidence.reviewer import EvidenceReviewer
    from app.services.evidence.store import EvidenceStore
    from app.services.evidence.synthesizer import SynthesizerAgent
    from app.services.evidence.writers import ArticleWriter
    from app.services.evidence_pipeline import EvidencePipeline, run_article
    from app.tests.test_evidence_pipeline import (
        ARTICLE,
        CLAIMS,
        EXTRACTION,
        Publisher,
        StubAcquirer,
    )

    class StubLLM:
        def __init__(self, payload: Any) -> None:
            self._payload = json.dumps(payload)

        def invoke(self, _input: Any, **_: Any) -> AIMessage:
            return AIMessage(content=self._payload)

    store = EvidenceStore(tmp_path / "content.db")
    log = RunLog(tmp_path / "content.db")
    pipeline = EvidencePipeline(
        store=store,
        acquirer=StubAcquirer(),
        extractor=ExtractorAgent(llm=StubLLM(EXTRACTION), model_id="stub"),
        synthesizer=SynthesizerAgent(
            llm=StubLLM(CLAIMS), model_id="stub", bundle_id_factory=lambda: "bundle-1"
        ),
        reviewer=EvidenceReviewer(model_id="stub", now=lambda: NOW),
        queries=["longevity"],
        now=lambda: NOW,
        runlog=log,
    )

    result = run_article(pipeline, ArticleWriter(llm=StubLLM(ARTICLE)), Publisher())

    assert result.run_id
    record = log.get(result.run_id)
    assert record is not None
    assert record.outcome == "published"
    assert record.prompt_versions["review"] == "1"

    stages = {event["stage"] for event in log.events(result.run_id)}
    assert {"acquire", "rank", "review", "write", "publish"} <= stages

    review = next(
        event for event in log.events(result.run_id) if event["event"] == "decision"
    )
    assert review["data"]["status"] == "approved"
    assert review["data"]["grade"] == "moderate"
    assert review["data"]["rationale"]

    store.close()
    log.close()
