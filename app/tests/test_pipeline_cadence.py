"""Tests for operator-adjustable publishing cadences.

Articles and tips each get their own rhythm — daily, weekly, every two weeks, or
monthly — chosen in the admin console and persisted across restarts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pytest
from fastapi.testclient import TestClient

from app.main import ContentRepository, _paginate_in_memory, app, get_repository
from app.models.content import Article, Tip
from app.services.pipeline_scheduler import (
    CADENCES,
    CADENCES_BY_KEY,
    JobConfig,
    PipelineScheduleStore,
    PipelineScheduler,
    resolve_cadence_key,
)

NOW = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
ADMIN = ("admin", "cadence-secret")
SAME_ORIGIN = {"Origin": "http://testserver"}


# ----------------------------------------------------------------------
# The cadence vocabulary
# ----------------------------------------------------------------------


def test_the_four_offered_cadences() -> None:
    assert [c.key for c in CADENCES] == ["daily", "weekly", "biweekly", "monthly"]


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("daily", NOW + timedelta(days=1)),
        ("weekly", NOW + timedelta(days=7)),
        ("biweekly", NOW + timedelta(days=14)),
        ("monthly", datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)),
    ],
)
def test_each_cadence_computes_its_next_run(key: str, expected: datetime) -> None:
    assert CADENCES_BY_KEY[key].next_run_at(NOW) == expected


def test_monthly_handles_short_months() -> None:
    """31 January + 1 month has to land on a day February actually has."""

    january = datetime(2026, 1, 31, 9, 0, tzinfo=timezone.utc)

    assert CADENCES_BY_KEY["monthly"].next_run_at(january) == datetime(
        2026, 2, 28, 9, 0, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("raw", ["daily", "WEEKLY", " monthly ", "biweekly"])
def test_known_cadence_keys_are_accepted(raw: str) -> None:
    assert resolve_cadence_key(raw) == raw.strip().lower()


@pytest.mark.parametrize("raw", ["hourly", "", None, "yearly", "1 day", "drop table"])
def test_unknown_cadence_keys_are_rejected(raw: str | None) -> None:
    assert resolve_cadence_key(raw) is None


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> PipelineScheduleStore:
    return PipelineScheduleStore(tmp_path / "schedule.db")


def _job(name: str = "articles", days: int = 1) -> JobConfig:
    return JobConfig(name=name, runner=lambda _at: True, interval_days=days)


def test_no_choice_means_the_configured_interval_applies(store: PipelineScheduleStore) -> None:
    assert store.get_cadence_key("articles") is None
    assert store.cadence_for(_job()) is None


def test_a_choice_is_stored_and_read_back(store: PipelineScheduleStore) -> None:
    store.set_cadence_key("articles", "weekly")

    assert store.get_cadence_key("articles") == "weekly"
    assert store.cadence_for(_job()).key == "weekly"


def test_a_choice_survives_a_restart(tmp_path: Path) -> None:
    """The console setting has to outlive the process that made it."""

    path = tmp_path / "schedule.db"
    PipelineScheduleStore(path).set_cadence_key("tips", "monthly")

    assert PipelineScheduleStore(path).get_cadence_key("tips") == "monthly"


def test_choosing_again_replaces_the_previous_choice(store: PipelineScheduleStore) -> None:
    store.set_cadence_key("articles", "weekly")
    store.set_cadence_key("articles", "daily")

    assert store.get_cadence_key("articles") == "daily"


def test_articles_and_tips_are_independent(store: PipelineScheduleStore) -> None:
    store.set_cadence_key("articles", "monthly")
    store.set_cadence_key("tips", "daily")

    assert store.get_cadence_key("articles") == "monthly"
    assert store.get_cadence_key("tips") == "daily"


def test_an_unknown_cadence_is_refused(store: PipelineScheduleStore) -> None:
    with pytest.raises(ValueError, match="Unknown cadence"):
        store.set_cadence_key("articles", "hourly")


def test_clearing_restores_the_configured_interval(store: PipelineScheduleStore) -> None:
    store.set_cadence_key("articles", "weekly")
    store.clear_cadence("articles")

    assert store.get_cadence_key("articles") is None


# ----------------------------------------------------------------------
# The chosen cadence drives scheduling
# ----------------------------------------------------------------------


def test_a_stored_cadence_overrides_the_configured_interval(store: PipelineScheduleStore) -> None:
    job = _job(days=1)  # configured daily
    store.set_last_run("articles", NOW)
    store.set_cadence_key("articles", "weekly")

    assert store.is_due(job, NOW + timedelta(days=2)) is False
    assert store.is_due(job, NOW + timedelta(days=8)) is True
    assert store.next_due(job) == NOW + timedelta(days=7)


def test_shortening_the_interval_can_make_a_job_due_at_once(store: PipelineScheduleStore) -> None:
    """Switching monthly to daily should not wait out the old month."""

    job = _job()
    store.set_last_run("articles", NOW)
    store.set_cadence_key("articles", "monthly")
    assert store.is_due(job, NOW + timedelta(days=3)) is False

    store.set_cadence_key("articles", "daily")

    assert store.is_due(job, NOW + timedelta(days=3)) is True


def test_lengthening_the_interval_defers_the_next_run(store: PipelineScheduleStore) -> None:
    job = _job()
    store.set_last_run("articles", NOW)
    store.set_cadence_key("articles", "biweekly")

    assert store.is_due(job, NOW + timedelta(days=10)) is False
    assert store.is_due(job, NOW + timedelta(days=15)) is True


def test_a_never_run_job_stays_due_whatever_the_cadence(store: PipelineScheduleStore) -> None:
    store.set_cadence_key("articles", "monthly")

    assert store.is_due(_job(), NOW) is True


# ----------------------------------------------------------------------
# Scheduler surface
# ----------------------------------------------------------------------


def _scheduler(store: PipelineScheduleStore) -> PipelineScheduler:
    return PipelineScheduler(
        store,
        [
            JobConfig(name="articles", runner=lambda _at: True, interval_days=1),
            JobConfig(name="tips", runner=lambda _at: True, interval_days=1),
        ],
    )


def test_set_cadence_accepts_a_known_job(store: PipelineScheduleStore) -> None:
    assert _scheduler(store).set_cadence("articles", "weekly") is True
    assert store.get_cadence_key("articles") == "weekly"


def test_set_cadence_rejects_an_unknown_job(store: PipelineScheduleStore) -> None:
    assert _scheduler(store).set_cadence("nope", "weekly") is False


def test_describe_jobs_reports_the_effective_cadence(store: PipelineScheduleStore) -> None:
    scheduler = _scheduler(store)
    scheduler.set_cadence("tips", "monthly")

    jobs = {job["name"]: job for job in scheduler.describe_jobs()}

    assert jobs["tips"]["cadence_key"] == "monthly"
    assert jobs["tips"]["cadence_label"] == "Monthly"
    assert jobs["tips"]["cadence_source"] == "console"
    # Untouched jobs still report their configured interval.
    assert jobs["articles"]["cadence_key"] == "daily"
    assert jobs["articles"]["cadence_source"] == "configuration"


def test_a_configured_interval_without_a_name_is_shown_as_custom(store: PipelineScheduleStore) -> None:
    """An operator running "every 3 days" should not see it silently rounded."""

    scheduler = PipelineScheduler(
        store, [JobConfig(name="articles", runner=lambda _at: True, interval_days=3)]
    )

    described = scheduler.describe_jobs()[0]

    assert described["cadence_is_custom"] is True
    assert described["cadence_key"] is None
    assert described["cadence_label"] == "Every 3 days"


def test_monthly_configuration_maps_to_the_monthly_cadence(store: PipelineScheduleStore) -> None:
    scheduler = PipelineScheduler(
        store, [JobConfig(name="tips", runner=lambda _at: True, interval_months=1)]
    )

    assert scheduler.describe_jobs()[0]["cadence_key"] == "monthly"


# ----------------------------------------------------------------------
# The admin console
# ----------------------------------------------------------------------


class _Repo(ContentRepository):
    def __init__(self) -> None:
        self._articles = [Article(id="a", title="A", content_body="b")]
        self._tips = [Tip(id="t", title="T", content_body="b")]

    def get_latest_articles(self, *, limit: int = 5): return self._articles[:limit]
    def get_article(self, article_id: str): return None
    def get_latest_tips(self, *, limit: int = 5): return self._tips[:limit]
    def get_latest_tip(self): return self._tips[0]
    def delete_article(self, article_id: str) -> bool: return False
    def delete_tip(self, tip_id: str) -> bool: return False
    def browse_articles(self, **kw): return _paginate_in_memory(list(self._articles), **kw)
    def browse_tips(self, **kw): return _paginate_in_memory(list(self._tips), **kw)


@pytest.fixture()
def admin_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterable[TestClient]:
    monkeypatch.setenv("LIVEON_ADMIN_PASSWORD", ADMIN[1])
    store = PipelineScheduleStore(tmp_path / "schedule.db")
    scheduler = _scheduler(store)

    app.dependency_overrides[get_repository] = lambda: _Repo()
    with TestClient(app) as client:
        app.state.pipeline_scheduler = scheduler
        client.scheduler = scheduler  # type: ignore[attr-defined]
        client.store = store  # type: ignore[attr-defined]
        yield client
    app.state.pipeline_scheduler = None
    app.dependency_overrides.pop(get_repository, None)


def test_the_console_offers_every_cadence(admin_client: TestClient) -> None:
    page = admin_client.get("/admin", auth=ADMIN).text

    for cadence in CADENCES:
        assert f'value="{cadence.key}"' in page
        assert cadence.label in page
    # One selector per pipeline, each posting to its own job.
    assert "/admin/pipelines/articles/interval" in page
    assert "/admin/pipelines/tips/interval" in page


def test_changing_a_cadence_persists_it(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/admin/pipelines/articles/interval",
        data={"cadence": "weekly"},
        auth=ADMIN,
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert admin_client.store.get_cadence_key("articles") == "weekly"


def test_the_two_pipelines_are_set_independently(admin_client: TestClient) -> None:
    for job, cadence in (("articles", "monthly"), ("tips", "daily")):
        admin_client.post(
            f"/admin/pipelines/{job}/interval",
            data={"cadence": cadence},
            auth=ADMIN,
            headers=SAME_ORIGIN,
            follow_redirects=False,
        )

    assert admin_client.store.get_cadence_key("articles") == "monthly"
    assert admin_client.store.get_cadence_key("tips") == "daily"


def test_the_console_shows_the_saved_choice_as_selected(admin_client: TestClient) -> None:
    admin_client.post(
        "/admin/pipelines/tips/interval",
        data={"cadence": "biweekly"},
        auth=ADMIN,
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )

    page = admin_client.get("/admin", auth=ADMIN).text

    assert 'value="biweekly"\n                        selected' in page.replace("\r\n", "\n") or (
        'value="biweekly" selected' in " ".join(page.split())
    )


def test_an_unknown_cadence_is_refused(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/admin/pipelines/articles/interval",
        data={"cadence": "hourly"},
        auth=ADMIN,
        headers=SAME_ORIGIN,
    )

    assert response.status_code == 400
    assert admin_client.store.get_cadence_key("articles") is None


def test_an_unknown_pipeline_is_refused(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/admin/pipelines/nope/interval",
        data={"cadence": "weekly"},
        auth=ADMIN,
        headers=SAME_ORIGIN,
    )

    assert response.status_code == 404


def test_changing_a_cadence_requires_authentication(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/admin/pipelines/articles/interval",
        data={"cadence": "weekly"},
        headers=SAME_ORIGIN,
    )

    assert response.status_code == 401
    assert admin_client.store.get_cadence_key("articles") is None


def test_changing_a_cadence_rejects_cross_origin_submissions(admin_client: TestClient) -> None:
    """The same protection the delete endpoints get."""

    response = admin_client.post(
        "/admin/pipelines/articles/interval",
        data={"cadence": "weekly"},
        auth=ADMIN,
        headers={"Origin": "http://evil.example"},
    )

    assert response.status_code == 403
    assert admin_client.store.get_cadence_key("articles") is None


def test_an_oversized_body_is_refused(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/admin/pipelines/articles/interval",
        content=b"cadence=" + b"x" * 9000,
        headers={**SAME_ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
        auth=ADMIN,
    )

    assert response.status_code == 413


def test_the_next_due_time_reflects_a_new_cadence(admin_client: TestClient) -> None:
    admin_client.store.set_last_run("articles", NOW)

    admin_client.post(
        "/admin/pipelines/articles/interval",
        data={"cadence": "monthly"},
        auth=ADMIN,
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )

    described = {j["name"]: j for j in admin_client.scheduler.describe_jobs()}["articles"]
    assert described["next_run"] == datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


def test_the_scheduler_honours_a_console_choice_when_running(store: PipelineScheduleStore) -> None:
    """End to end: choose monthly, and a daily-configured job stops being due."""

    calls: list[datetime] = []
    job = JobConfig(
        name="articles", runner=lambda at: (calls.append(at), True)[1], interval_days=1
    )
    scheduler = PipelineScheduler(store, [job])

    # First run establishes a last_run.
    asyncio.run(scheduler.run_once())
    assert len(calls) == 1

    scheduler.set_cadence("articles", "monthly")

    # A second cycle immediately afterwards must not run it again.
    asyncio.run(scheduler.run_once())
    assert len(calls) == 1
