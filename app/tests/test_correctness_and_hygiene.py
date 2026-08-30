"""Tests for the P3 correctness, hygiene, and coverage work.

Covers the silent publisher failure, the shared repository, the lifespan migration,
packaging, and the smaller correctness items — plus the repository and scheduler
coverage gaps the review called out.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.models.aggregator import AggregatedContent, FeedSource
from app.models.content import Article, Tip
from app.models.editor import EditedArticle
from app.models.publisher import PublicationResult
from app.models.summarizer import ArticleDraft
from app.services.aggregator import (
    AggregationResult,
    LongevityNewsAggregator,
    _has_tracking,
    _is_tracking_param,
    _normalise_url,
)
from app.services.pipeline import ContentPipeline
from app.services.pipeline_scheduler import (
    JobConfig,
    PipelineScheduleStore,
    scheduler_enabled,
)
from app.services.sqlite_repo import LocalSQLiteContentRepository, _iso_now

NOW = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parents[2]


# ----------------------------------------------------------------------
# #22 A broken publisher must not read as success
# ----------------------------------------------------------------------


def _item(url: str = "https://example.test/story") -> AggregatedContent:
    return AggregatedContent(
        title="A study", url=url, summary="Summary", published_at=NOW, source="Feed"
    )


class _Aggregator:
    def __init__(self, items: list[AggregatedContent]) -> None:
        self.items = items

    def gather(self, *, limit_per_feed: int = 5) -> AggregationResult:
        return AggregationResult(items=list(self.items), errors=[])


class _Summarizer:
    def summarize(self, items):  # type: ignore[no-untyped-def]
        return ArticleDraft(title="T", summary="S", body="B")


class _Editor:
    def revise(self, draft):  # type: ignore[no-untyped-def]
        return EditedArticle(title="T", summary="S", body="B")


class _Publisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def publish(self, article, *, slug=None, commit_message=None, published_at=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.fail:
            raise RuntimeError("disk full")
        return PublicationResult(
            slug=slug or "slug", path=Path("db/articles/slug"), commit_hash=None,
            published_at=published_at or NOW,
        )


def _pipeline(publisher: _Publisher, items: list[AggregatedContent] | None = None) -> ContentPipeline:
    return ContentPipeline(
        aggregator=_Aggregator(items or [_item()]),
        summarizer=_Summarizer(),
        editor=_Editor(),
        publisher=publisher,
    )


def test_a_publisher_failure_is_recorded_as_an_error() -> None:
    """The append was commented out, so a broken publisher produced no error at all."""

    result = _pipeline(_Publisher(fail=True)).run()

    assert not result.succeeded
    assert result.errors, "a publisher exception must surface as a pipeline error"
    assert "Publisher failed" in result.errors[0]
    assert "disk full" in result.errors[0]


def test_a_publisher_failure_makes_the_cli_exit_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """It used to report 'finished without producing content' and exit 0."""

    from app.scripts import run_pipeline

    monkeypatch.setattr(run_pipeline, "_build_pipeline", lambda *a, **k: _pipeline(_Publisher(fail=True)))

    assert run_pipeline.run([]) == 1


def test_a_successful_run_still_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scripts import run_pipeline

    monkeypatch.setattr(run_pipeline, "_build_pipeline", lambda *a, **k: _pipeline(_Publisher()))

    assert run_pipeline.run([]) == 0


# ----------------------------------------------------------------------
# #30 max_articles is what decides how many get published
# ----------------------------------------------------------------------


def test_one_article_is_published_by_default() -> None:
    publisher = _Publisher()
    items = [_item(f"https://example.test/{i}") for i in range(5)]

    result = _pipeline(publisher, items).run(limit_per_feed=5)

    assert publisher.calls == 1
    assert result.published_count == 1


def test_max_articles_publishes_more() -> None:
    publisher = _Publisher()
    items = [_item(f"https://example.test/{i}") for i in range(5)]

    result = _pipeline(publisher, items).run(limit_per_feed=5, max_articles=3)

    assert publisher.calls == 3
    assert result.published_count == 3
    # The single-publication accessor still points at the first one.
    assert result.publication is result.publications[0]


def test_max_articles_is_capped_by_available_content() -> None:
    publisher = _Publisher()

    result = _pipeline(publisher, [_item()]).run(max_articles=10)

    assert result.published_count == 1


def test_already_published_sources_are_skipped() -> None:
    class _Repo:
        def find_article_by_source_url(self, url: str):
            return Article(title="old", content_body="b") if url.endswith("/0") else None

    publisher = _Publisher()
    items = [_item(f"https://example.test/{i}") for i in range(3)]
    pipeline = ContentPipeline(
        aggregator=_Aggregator(items),
        summarizer=_Summarizer(),
        editor=_Editor(),
        publisher=publisher,
        repository=_Repo(),
    )

    result = pipeline.run(max_articles=5)

    assert result.published_count == 2


# ----------------------------------------------------------------------
# #23 One repository for the process
# ----------------------------------------------------------------------


def test_the_repository_is_reused_across_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It used to be rebuilt per request: mkdir, connect, and six DDL statements."""

    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))
    monkeypatch.setenv("LIVEON_STORAGE", "sqlite")

    from app import main as main_module

    main_module.app.state.content_repository = None
    try:
        first = main_module.get_repository()
        second = main_module.get_repository()
        assert first is second
    finally:
        main_module.app.state.content_repository = None


def test_memory_storage_is_a_supported_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """`memory` used to work only by falling through the 'unsupported' branch."""

    monkeypatch.setenv("LIVEON_STORAGE", "memory")

    from app import main as main_module

    repository = main_module.build_repository()

    assert isinstance(repository, main_module._InMemoryContentRepository)


def test_an_unreadable_database_falls_back_to_memory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIVEON_STORAGE", "sqlite")
    # A path that cannot be a database file: its parent is a file, not a directory.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("LIVEON_DB_PATH", str(blocker / "content.db"))

    from app import main as main_module

    assert isinstance(main_module.build_repository(), main_module._InMemoryContentRepository)


# ----------------------------------------------------------------------
# #25 Deprecated APIs
# ----------------------------------------------------------------------


def test_the_app_uses_a_lifespan_not_on_event() -> None:
    source = (REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8")

    # Match the decorator itself, not the prose explaining why it is gone.
    decorators = [line for line in source.splitlines() if line.startswith("@app.on_event")]
    assert decorators == []
    assert "lifespan=lifespan" in source


def test_lifespan_starts_and_stops_cleanly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))

    from app.main import app

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    # Shutdown released the repository.
    assert getattr(app.state, "content_repository", None) is None


def test_timestamps_are_utc_aware_and_marked() -> None:
    stamp = _iso_now()

    assert stamp.endswith("Z"), "timestamps must carry their zone"
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 60


def test_the_coach_no_longer_imports_the_deprecated_client() -> None:
    source = (REPO_ROOT / "app" / "services" / "coach.py").read_text(encoding="utf-8")

    assert "from langchain_community" not in source


def test_the_maintained_ollama_client_is_preferred() -> None:
    from app.services.llm_factory import resolve_chat_ollama_class

    resolved = resolve_chat_ollama_class()

    assert "langchain_ollama" in resolved.__module__


# ----------------------------------------------------------------------
# #26 Packaging
# ----------------------------------------------------------------------


def test_runtime_requirements_are_fully_pinned() -> None:
    lines = (REPO_ROOT / "app" / "requirements.txt").read_text(encoding="utf-8").splitlines()
    requirements = [line.strip() for line in lines if line.strip() and not line.startswith("#")]

    assert requirements
    for requirement in requirements:
        assert "==" in requirement, f"{requirement} is unpinned"


def test_test_tooling_is_not_in_the_runtime_image() -> None:
    runtime = (REPO_ROOT / "app" / "requirements.txt").read_text(encoding="utf-8")

    assert "pytest" not in runtime


def test_dev_requirements_include_the_runtime_set_and_pytest() -> None:
    dev = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

    assert "-r app/requirements.txt" in dev
    assert "pytest==" in dev


def test_the_ollama_client_is_a_declared_dependency() -> None:
    runtime = (REPO_ROOT / "app" / "requirements.txt").read_text(encoding="utf-8")

    assert "langchain-ollama==" in runtime


# ----------------------------------------------------------------------
# #27 Repository hygiene
# ----------------------------------------------------------------------


def test_gitignore_covers_the_usual_artifacts() -> None:
    patterns = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").split()

    for expected in ["__pycache__/", ".venv/", ".pytest_cache/", "*.db", "*.log", "*.bak"]:
        assert expected in patterns, f"{expected} is not ignored"


def test_gitignore_has_no_run_together_last_line() -> None:
    """A missing trailing newline had fused two patterns, making both inert."""

    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert text.endswith("\n")
    for line in text.splitlines():
        assert line.count("/") <= 1 or line.strip().startswith("#") or " " not in line


def test_build_artifacts_are_not_tracked() -> None:
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:  # pragma: no cover - not a git checkout
        pytest.skip("not a git repository")

    tracked = result.stdout.split()
    offenders = [
        path
        for path in tracked
        if path.endswith((".log", ".npy", ".db", ".bak")) or path == "controller_manifest.json"
    ]

    assert offenders == [], f"build/run artifacts are tracked: {offenders}"


# ----------------------------------------------------------------------
# #30 Smaller items
# ----------------------------------------------------------------------


def test_the_aggregator_closes_the_client_it_opened() -> None:
    aggregator = LongevityNewsAggregator([FeedSource(name="f", url="https://example.test/feed")])

    assert aggregator._client.is_closed is False
    aggregator.close()
    assert aggregator._client.is_closed is True


def test_the_aggregator_works_as_a_context_manager() -> None:
    with LongevityNewsAggregator([FeedSource(name="f", url="https://example.test/feed")]) as agg:
        client = agg._client
    assert client.is_closed is True


def test_a_caller_supplied_client_is_left_alone() -> None:
    client = httpx.Client()
    aggregator = LongevityNewsAggregator(
        [FeedSource(name="f", url="https://example.test/feed")], client=client
    )

    aggregator.close()

    assert client.is_closed is False
    client.close()


@pytest.mark.parametrize("key", ["utm_source", "UTM_Medium", "fbclid", "gclid", "msclkid", "mc_cid"])
def test_tracking_parameters_are_recognised(key: str) -> None:
    assert _is_tracking_param(key) is True


@pytest.mark.parametrize("key", ["id", "page", "q", "story"])
def test_content_parameters_are_not_tracking(key: str) -> None:
    assert _is_tracking_param(key) is False


def test_links_differing_only_by_tracking_normalise_together() -> None:
    """Only `utm_` was stripped, so fbclid/gclid variants deduplicated as separate items."""

    base = "https://example.test/story"

    assert _normalise_url(f"{base}?fbclid=a") == _normalise_url(f"{base}?gclid=b")
    assert _normalise_url(f"{base}?utm_source=x") == _normalise_url(base)
    # A genuine query parameter is preserved.
    assert _normalise_url(f"{base}?id=7") != _normalise_url(base)


def test_tracking_detection_covers_more_than_utm() -> None:
    assert _has_tracking("https://example.test/a?fbclid=1") is True
    assert _has_tracking("https://example.test/a?id=1") is False


def test_the_pipeline_module_is_silent_on_import() -> None:
    """Importing it used to announce a pipeline start, including from the web app."""

    result = subprocess.run(
        [sys.executable, "-c", "import logging;logging.basicConfig(level=0);import app.scripts.run_pipeline"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )

    assert "PIPELINE_START" not in result.stdout + result.stderr


def test_the_tip_generation_protocol_matches_the_real_signature() -> None:
    import inspect

    from app.services.pipeline import SupportsTipGeneration
    from app.services.tip_generator import TipGenerator

    protocol = set(inspect.signature(SupportsTipGeneration.generate).parameters)
    concrete = set(inspect.signature(TipGenerator.generate).parameters)

    assert protocol == concrete


# ----------------------------------------------------------------------
# #30 Cross-process scheduler lock
# ----------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> PipelineScheduleStore:
    return PipelineScheduleStore(tmp_path / "schedule.db")


def test_only_one_worker_can_claim_a_job(store: PipelineScheduleStore) -> None:
    """uvicorn --workers N previously gave N schedulers racing on the same row."""

    assert store.try_acquire("tips", "worker-a", now=NOW, ttl_seconds=3600) is True
    assert store.try_acquire("tips", "worker-b", now=NOW, ttl_seconds=3600) is False


def test_a_released_claim_can_be_taken(store: PipelineScheduleStore) -> None:
    store.try_acquire("tips", "worker-a", now=NOW, ttl_seconds=3600)
    store.release("tips", "worker-a")

    assert store.try_acquire("tips", "worker-b", now=NOW, ttl_seconds=3600) is True


def test_a_claim_is_not_released_by_another_owner(store: PipelineScheduleStore) -> None:
    store.try_acquire("tips", "worker-a", now=NOW, ttl_seconds=3600)
    store.release("tips", "worker-b")

    assert store.try_acquire("tips", "worker-c", now=NOW, ttl_seconds=3600) is False


def test_an_expired_claim_is_reclaimed(store: PipelineScheduleStore) -> None:
    """A worker that dies mid-run must not wedge the job forever."""

    store.try_acquire("tips", "crashed", now=NOW, ttl_seconds=60)

    assert store.try_acquire("tips", "healthy", now=NOW + timedelta(hours=2), ttl_seconds=60) is True


def test_separate_jobs_do_not_block_each_other(store: PipelineScheduleStore) -> None:
    assert store.try_acquire("tips", "a", now=NOW, ttl_seconds=60) is True
    assert store.try_acquire("articles", "a", now=NOW, ttl_seconds=60) is True


def test_a_locked_job_is_skipped_rather_than_run(store: PipelineScheduleStore) -> None:
    import asyncio

    from app.services.pipeline_scheduler import PipelineScheduler

    calls: list[datetime] = []
    job = JobConfig(name="tips", runner=lambda at: (calls.append(at), True)[1], interval_days=1)
    scheduler = PipelineScheduler(store, [job])

    # Another process is holding the claim. The claim must be live relative to the
    # real clock the scheduler reads, not the fixed NOW used elsewhere here.
    store.try_acquire(
        "tips", "someone-else", now=datetime.now(timezone.utc), ttl_seconds=3600
    )

    asyncio.run(scheduler.run_once())

    assert calls == []
    assert store.get_last_run("tips") is None


# ----------------------------------------------------------------------
# #29 Coverage gaps: the SQLite repository and scheduler defaults
# ----------------------------------------------------------------------


@pytest.fixture()
def repo(tmp_path: Path) -> LocalSQLiteContentRepository:
    return LocalSQLiteContentRepository(db_path=tmp_path / "content.db")


def test_articles_round_trip(repo: LocalSQLiteContentRepository) -> None:
    stored = repo.save_article(
        Article(
            id="a1", title="Title", content_body="Body", summary="Summary",
            source_urls=["https://example.test/a"], tags=["sleep"], published_date=NOW,
        )
    )

    assert stored.id == "a1"
    fetched = repo.get_article("a1")
    assert fetched is not None
    assert fetched.title == "Title"
    assert fetched.tags == ["sleep"]
    assert fetched.published_date == NOW


def test_a_missing_article_returns_none(repo: LocalSQLiteContentRepository) -> None:
    assert repo.get_article("nope") is None


def test_articles_are_returned_newest_first(repo: LocalSQLiteContentRepository) -> None:
    repo.save_article(Article(id="old", title="Old", content_body="b", published_date=NOW - timedelta(days=5)))
    repo.save_article(Article(id="new", title="New", content_body="b", published_date=NOW))

    assert [a.id for a in repo.get_latest_articles(limit=5)] == ["new", "old"]


def test_articles_are_found_by_source_url(repo: LocalSQLiteContentRepository) -> None:
    repo.save_article(
        Article(id="a1", title="T", content_body="b", source_urls=["https://example.test/story?utm_source=x"])
    )

    # The lookup normalises, so the tracking parameter does not hide the match.
    assert repo.find_article_by_source_url("https://example.test/story") is not None
    assert repo.find_article_by_source_url("https://example.test/other") is None


def test_saving_an_article_again_updates_it(repo: LocalSQLiteContentRepository) -> None:
    repo.save_article(Article(id="a1", title="First", content_body="b"))
    repo.save_article(Article(id="a1", title="Second", content_body="b"))

    assert repo.get_article("a1").title == "Second"
    assert len(repo.get_latest_articles(limit=10)) == 1


def test_deleting_an_article_removes_its_source_index(repo: LocalSQLiteContentRepository) -> None:
    repo.save_article(Article(id="a1", title="T", content_body="b", source_urls=["https://example.test/s"]))

    assert repo.delete_article("a1") is True
    assert repo.get_article("a1") is None
    assert repo.find_article_by_source_url("https://example.test/s") is None
    assert repo.delete_article("a1") is False


def test_tips_round_trip_and_delete(repo: LocalSQLiteContentRepository) -> None:
    repo.save_tip(Tip(id="t1", title="Tip", content_body="Body", tags=["sleep"], published_date=NOW))

    assert repo.get_tip("t1").title == "Tip"
    assert repo.get_latest_tip().id == "t1"
    assert repo.find_tip_by_title("Tip") is not None
    assert repo.find_tip_by_tags(["sleep"]) is not None
    assert repo.delete_tip("t1") is True
    assert repo.get_latest_tip() is None


def test_seeding_only_fills_an_empty_database(repo: LocalSQLiteContentRepository) -> None:
    created = repo.seed_if_empty(
        articles=[Article(id="seed", title="Seed", content_body="b")],
        tips=[Tip(id="seed-tip", title="Seed tip", content_body="b")],
    )
    assert created["articles"] == ["seed"]

    again = repo.seed_if_empty(articles=[Article(id="other", title="Other", content_body="b")])
    assert again["articles"] == []


def test_the_scheduler_is_on_by_default_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pytest guard means the default-on path is otherwise never exercised."""

    monkeypatch.delenv("LIVEON_DISABLE_SCHEDULER", raising=False)
    monkeypatch.delenv("LIVEON_ENABLE_SCHEDULER", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    assert scheduler_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_the_scheduler_can_be_switched_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.delenv("LIVEON_DISABLE_SCHEDULER", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("LIVEON_ENABLE_SCHEDULER", value)

    assert scheduler_enabled() is False


def test_the_disable_switch_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("LIVEON_ENABLE_SCHEDULER", "1")
    monkeypatch.setenv("LIVEON_DISABLE_SCHEDULER", "1")

    assert scheduler_enabled() is False


# ----------------------------------------------------------------------
# #30 The JSON API is symmetric
# ----------------------------------------------------------------------


def test_articles_are_available_over_the_json_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The public API exposed tips but not articles."""

    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))
    from app import main as main_module

    main_module.app.state.content_repository = None
    repository = LocalSQLiteContentRepository(db_path=tmp_path / "content.db")
    repository.save_article(
        Article(id="a1", title="Sleep study", content_body="Body", summary="S", tags=["sleep"], published_date=NOW)
    )
    main_module.app.dependency_overrides[main_module.get_repository] = lambda: repository

    try:
        with TestClient(main_module.app) as client:
            listing = client.get("/api/articles")
            assert listing.status_code == 200
            payload = listing.json()
            assert payload["total"] == 1
            assert payload["items"][0]["title"] == "Sleep study"

            filtered = client.get("/api/articles?tag=sleep")
            assert filtered.json()["total"] == 1
            assert client.get("/api/articles?tag=nope").json()["total"] == 0

            single = client.get("/api/articles/a1")
            assert single.status_code == 200
            assert single.json()["id"] == "a1"

            missing = client.get("/api/articles/nope")
            assert missing.status_code == 404
            assert missing.headers["content-type"].startswith("application/json")
    finally:
        main_module.app.dependency_overrides.pop(main_module.get_repository, None)
        main_module.app.state.content_repository = None


# ----------------------------------------------------------------------
# A scheduled job must never take the process down
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "boom",
    [
        SystemExit("misconfigured provider"),
        KeyboardInterrupt(),
        RuntimeError("ordinary failure"),
    ],
)
def test_a_job_that_raises_does_not_kill_the_scheduler(
    store: PipelineScheduleStore, boom: BaseException
) -> None:
    """A misconfigured tip provider raised SystemExit, which is a BaseException.

    It sailed past `except Exception`, escaped the worker thread, and terminated the
    whole web server — found by running the scheduler for real rather than in a test.
    """

    import asyncio

    from app.services.pipeline_scheduler import PipelineScheduler

    def _explode(_at: datetime) -> bool:
        raise boom

    job = JobConfig(name="tips", runner=_explode, interval_days=1)
    scheduler = PipelineScheduler(store, [job])

    asyncio.run(scheduler.run_once())  # must return, not propagate

    # The failure is recorded as "not done", so it is retried rather than skipped.
    assert store.get_last_run("tips") is None


def test_a_misconfigured_provider_fails_one_job_not_the_process(
    store: PipelineScheduleStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real path: the scheduler's tip runner with no local-stub opt-in."""

    import asyncio

    from app.services import pipeline_scheduler

    monkeypatch.delenv("LIVEON_ALLOW_LOCAL_LLM", raising=False)
    monkeypatch.delenv("LIVEON_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LIVEON_TIP_MODEL", raising=False)
    monkeypatch.delenv("LIVEON_SUMMARIZER_MODEL", raising=False)

    job = JobConfig(name="tips", runner=pipeline_scheduler._run_tip_pipeline, interval_days=1)
    scheduler = pipeline_scheduler.PipelineScheduler(store, [job])

    asyncio.run(scheduler.run_once())

    assert store.get_last_run("tips") is None
