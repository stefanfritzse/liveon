"""Background scheduler for the article and tip pipelines."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import asyncio
import logging
import os
from pathlib import Path
import sqlite3
from typing import Callable, Iterable

from app.services.sqlite_repo import DEFAULT_DB_PATH


logger = logging.getLogger("liveon.scheduler")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _positive_int(raw: str | None, default: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class JobConfig:
    name: str
    runner: Callable[[datetime], bool]
    interval_days: int | None = None
    interval_months: int | None = None

    def next_run_at(self, last_run: datetime) -> datetime:
        if self.interval_months:
            return _add_months(last_run, self.interval_months)
        if self.interval_days:
            return last_run + timedelta(days=self.interval_days)
        raise ValueError("JobConfig requires interval_days or interval_months.")


class PipelineScheduleStore:
    """Persist the last successful run time for each scheduled pipeline."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_schedule (
                    job_name TEXT PRIMARY KEY,
                    last_run_at TEXT NOT NULL
                );
                """
            )

    def get_last_run(self, job_name: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT last_run_at FROM pipeline_schedule WHERE job_name = ?;",
            (job_name,),
        ).fetchone()
        if not row:
            return None
        return _parse_timestamp(row["last_run_at"])

    def set_last_run(self, job_name: str, last_run: datetime) -> None:
        timestamp = _format_timestamp(last_run)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO pipeline_schedule(job_name, last_run_at)
                VALUES (?, ?)
                ON CONFLICT(job_name) DO UPDATE SET
                    last_run_at = excluded.last_run_at;
                """,
                (job_name, timestamp),
            )

    def ensure_initialized(self, job_name: str, now: datetime) -> datetime:
        last_run = self.get_last_run(job_name)
        if last_run is None:
            self.set_last_run(job_name, now)
            logger.info("Initialized pipeline schedule for %s at %s", job_name, now.isoformat())
            return now
        return last_run


class PipelineScheduler:
    """Periodically run the article and tip pipelines inside the web app."""

    def __init__(
        self,
        store: PipelineScheduleStore,
        jobs: Iterable[JobConfig],
        *,
        check_interval_sec: int = 3600,
    ) -> None:
        self._store = store
        self._jobs = list(jobs)
        self._check_interval_sec = max(check_interval_sec, 60)
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def run_forever(self) -> None:
        await self.run_once()
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._check_interval_sec)
            except asyncio.TimeoutError:
                await self.run_once()

    async def run_once(self) -> None:
        async with self._lock:
            now = _utc_now()
            for job in self._jobs:
                last_run = self._store.ensure_initialized(job.name, now)
                next_run = job.next_run_at(last_run)
                if now < next_run:
                    continue
                logger.info("Starting scheduled pipeline: %s", job.name)
                success = await asyncio.to_thread(job.runner, now)
                if success:
                    self._store.set_last_run(job.name, now)
                    logger.info("Scheduled pipeline finished: %s", job.name)
                else:
                    logger.warning("Scheduled pipeline failed: %s", job.name)


def scheduler_enabled() -> bool:
    if os.getenv("LIVEON_DISABLE_SCHEDULER"):
        return False
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    raw = (os.getenv("LIVEON_ENABLE_SCHEDULER") or "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _resolve_db_path() -> Path:
    raw = (os.getenv("LIVEON_DB_PATH") or "").strip()
    if raw:
        return Path(raw)
    return DEFAULT_DB_PATH


def _run_article_pipeline(run_at: datetime) -> bool:
    from app.scripts import run_pipeline

    try:
        storage = (os.getenv("LIVEON_STORAGE") or "sqlite").strip().lower()
        db_path = os.getenv("LIVEON_DB_PATH")
        feed_limit = _positive_int(os.getenv("LIVEON_FEED_LIMIT"), 5)
        pipeline = run_pipeline._build_pipeline(storage, db_path, feed_limit)
        result = pipeline.run(limit_per_feed=feed_limit, published_at=run_at)
    except Exception:
        logger.exception("Article pipeline execution failed")
        return False

    if result.errors:
        logger.warning("Article pipeline errors: %s", "; ".join(result.errors))
        return False
    if result.warnings:
        logger.info("Article pipeline warnings: %s", "; ".join(result.warnings))
    return True


def _run_tip_pipeline(run_at: datetime) -> bool:
    from app.scripts import run_tip_pipeline

    try:
        provider = run_tip_pipeline._default_model_provider()
        model_name = os.getenv("LIVEON_TIP_MODEL_NAME")
        allow_local_stub = run_tip_pipeline._env_bool("LIVEON_ALLOW_LOCAL_LLM")
        llm = run_tip_pipeline._create_tip_llm(
            provider,
            model_name=model_name,
            allow_local_stub=allow_local_stub,
        )
        pipeline = run_tip_pipeline._build_pipeline(llm)
        result = pipeline.run(published_at=run_at)
    except Exception:
        logger.exception("Tip pipeline execution failed")
        return False

    if result.errors:
        logger.warning("Tip pipeline errors: %s", "; ".join(result.errors))
        return False
    if result.warnings:
        logger.info("Tip pipeline warnings: %s", "; ".join(result.warnings))
    return True


def create_pipeline_scheduler() -> PipelineScheduler | None:
    if not scheduler_enabled():
        return None

    article_days = _positive_int(os.getenv("LIVEON_ARTICLE_INTERVAL_DAYS"), 7)
    tip_months = _positive_int(os.getenv("LIVEON_TIP_INTERVAL_MONTHS"), 1)
    check_interval = _positive_int(os.getenv("LIVEON_PIPELINE_CHECK_INTERVAL_SEC"), 3600)

    try:
        store = PipelineScheduleStore(_resolve_db_path())
    except Exception:
        logger.exception("Pipeline scheduler unavailable: failed to open schedule store")
        return None
    jobs = [
        JobConfig(name="articles", runner=_run_article_pipeline, interval_days=article_days),
        JobConfig(name="tips", runner=_run_tip_pipeline, interval_months=tip_months),
    ]
    return PipelineScheduler(store, jobs, check_interval_sec=check_interval)
