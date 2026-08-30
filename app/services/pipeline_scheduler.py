"""Background scheduler for the article and tip pipelines."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import asyncio
import logging
import os
import socket
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


def _positive_int(raw: str | None, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


@dataclass(frozen=True)
class Cadence:
    """A named publishing rhythm offered in the admin console."""

    key: str
    label: str
    days: int | None = None
    months: int | None = None

    def next_run_at(self, last_run: datetime) -> datetime:
        if self.months:
            return _add_months(last_run, self.months)
        if self.days:
            return last_run + timedelta(days=self.days)
        raise ValueError(f"Cadence {self.key!r} has no interval.")


#: Ordered so the admin console can render them shortest-first.
CADENCES: tuple[Cadence, ...] = (
    Cadence(key="daily", label="Daily", days=1),
    Cadence(key="weekly", label="Weekly", days=7),
    Cadence(key="biweekly", label="Every two weeks", days=14),
    Cadence(key="monthly", label="Monthly", months=1),
)

CADENCES_BY_KEY: dict[str, Cadence] = {cadence.key: cadence for cadence in CADENCES}


def resolve_cadence_key(raw: str | None) -> str | None:
    """Return a known cadence key, or ``None`` when ``raw`` is not one."""

    cleaned = (raw or "").strip().lower()
    return cleaned if cleaned in CADENCES_BY_KEY else None


@dataclass(frozen=True)
class JobConfig:
    name: str
    runner: Callable[[datetime], bool]
    interval_days: int | None = None
    interval_months: int | None = None

    def next_run_at(self, last_run: datetime, cadence: Cadence | None = None) -> datetime:
        """Return when this job is next due.

        ``cadence`` is the operator's stored choice; without one the interval
        configured through the environment applies.
        """

        if cadence is not None:
            return cadence.next_run_at(last_run)
        if self.interval_months:
            return _add_months(last_run, self.interval_months)
        if self.interval_days:
            return last_run + timedelta(days=self.interval_days)
        raise ValueError("JobConfig requires interval_days or interval_months.")

    @property
    def configured_cadence_key(self) -> str | None:
        """The cadence matching this job's environment configuration, if any.

        An interval like "every 3 days" has no name in the console, so it reports
        ``None`` and is shown as a custom setting rather than silently rounded.
        """

        for cadence in CADENCES:
            if cadence.months and cadence.months == self.interval_months:
                return cadence.key
            if cadence.days and not self.interval_months and cadence.days == self.interval_days:
                return cadence.key
        return None

    @property
    def configured_description(self) -> str:
        if self.interval_months:
            unit = "month" if self.interval_months == 1 else "months"
            return f"Every {self.interval_months} {unit}"
        days = self.interval_days or 0
        unit = "day" if days == 1 else "days"
        return f"Every {days} {unit}"


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
            # Claimed while a job runs, so N uvicorn workers do not each start the
            # same pipeline against the same database.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_lock (
                    job_name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )
            # The operator's cadence choice, which overrides the environment default
            # and survives restarts.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_interval (
                    job_name TEXT PRIMARY KEY,
                    cadence_key TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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

    def get_cadence_key(self, job_name: str) -> str | None:
        """Return the stored cadence choice for ``job_name``, if one was made."""

        row = self._conn.execute(
            "SELECT cadence_key FROM pipeline_interval WHERE job_name = ?;",
            (job_name,),
        ).fetchone()
        return resolve_cadence_key(row["cadence_key"]) if row else None

    def set_cadence_key(self, job_name: str, cadence_key: str) -> None:
        """Persist a cadence choice, replacing any previous one."""

        key = resolve_cadence_key(cadence_key)
        if key is None:
            raise ValueError(f"Unknown cadence: {cadence_key!r}")

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO pipeline_interval(job_name, cadence_key, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(job_name) DO UPDATE SET
                    cadence_key = excluded.cadence_key,
                    updated_at = excluded.updated_at;
                """,
                (job_name, key, _format_timestamp(_utc_now())),
            )

    def clear_cadence(self, job_name: str) -> None:
        """Drop a stored choice so the environment default applies again."""

        with self._conn:
            self._conn.execute(
                "DELETE FROM pipeline_interval WHERE job_name = ?;", (job_name,)
            )

    def cadence_for(self, job: "JobConfig") -> Cadence | None:
        """Return the cadence in force for ``job``: the stored choice, if any."""

        key = self.get_cadence_key(job.name)
        return CADENCES_BY_KEY.get(key) if key else None

    def is_due(self, job: "JobConfig", now: datetime) -> bool:
        """Return ``True`` when ``job`` should run.

        A job with no recorded run is due immediately. Stamping ``last_run = now`` on
        first boot — as this used to — meant a fresh install produced no articles for a
        week and no tips for a month, which reads as a broken site rather than a
        scheduled one.
        """

        last_run = self.get_last_run(job.name)
        if last_run is None:
            logger.info(
                "No previous run recorded for %s; running now", job.name,
                extra={"event": "pipeline_scheduler.first_run", "job": job.name},
            )
            return True
        return now >= job.next_run_at(last_run, self.cadence_for(job))

    def try_acquire(self, job_name: str, owner: str, *, now: datetime, ttl_seconds: int) -> bool:
        """Claim ``job_name`` for ``owner``, returning ``False`` if someone else holds it.

        The claim expires so a worker that dies mid-run cannot wedge the job forever.
        """

        expires_at = _format_timestamp(now + timedelta(seconds=ttl_seconds))
        try:
            with self._conn:  # BEGIN ... COMMIT, so the read and write are atomic
                self._conn.execute(
                    "DELETE FROM pipeline_lock WHERE job_name = ? AND expires_at <= ?;",
                    (job_name, _format_timestamp(now)),
                )
                cursor = self._conn.execute(
                    """
                    INSERT INTO pipeline_lock(job_name, owner, expires_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(job_name) DO NOTHING;
                    """,
                    (job_name, owner, expires_at),
                )
        except sqlite3.OperationalError:
            # Another process holds the write lock; it is running the job.
            return False
        return cursor.rowcount > 0

    def release(self, job_name: str, owner: str) -> None:
        """Release a claim held by ``owner``; a claim held by anyone else is left alone."""

        with self._conn:
            self._conn.execute(
                "DELETE FROM pipeline_lock WHERE job_name = ? AND owner = ?;",
                (job_name, owner),
            )

    def next_due(self, job: "JobConfig") -> datetime | None:
        """Return when ``job`` is next due, or ``None`` when it has never run."""

        last_run = self.get_last_run(job.name)
        return job.next_run_at(last_run, self.cadence_for(job)) if last_run else None


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
        self._running: set[str] = set()
        # Identifies this process when claiming a job across workers.
        self._owner = f"{socket.gethostname()}:{os.getpid()}"
        # Long enough for a slow local model to finish a run, short enough that a
        # crashed worker's claim is reclaimed rather than blocking forever.
        self._lock_ttl_sec = max(self._check_interval_sec * 2, 3600)

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
                if not self._store.is_due(job, now):
                    continue
                await self._execute(job, now)

    async def _execute(self, job: JobConfig, now: datetime) -> bool:
        """Run one job under a cross-process claim, stamping only on success."""

        if not self._store.try_acquire(
            job.name, self._owner, now=now, ttl_seconds=self._lock_ttl_sec
        ):
            logger.info(
                "Pipeline %s is claimed by another worker; skipping this cycle",
                job.name,
                extra={"event": "pipeline_scheduler.locked", "job": job.name},
            )
            return False

        try:
            logger.info("Starting pipeline: %s", job.name)
            try:
                success = await asyncio.to_thread(job.runner, now)
            except BaseException:  # noqa: BLE001 - including SystemExit
                # A job is background work; nothing it raises may end the process.
                logger.exception(
                    "Pipeline raised: %s",
                    job.name,
                    extra={"event": "pipeline_scheduler.job_raised", "job": job.name},
                )
                success = False
            if success:
                self._store.set_last_run(job.name, now)
                logger.info("Pipeline finished: %s", job.name)
            else:
                logger.warning("Pipeline failed: %s", job.name)
            return success
        finally:
            self._store.release(job.name, self._owner)

    # ------------------------------------------------------------------
    # Manual control
    # ------------------------------------------------------------------
    @property
    def job_names(self) -> list[str]:
        return [job.name for job in self._jobs]

    def describe_jobs(self) -> list[dict[str, object]]:
        """Summarise each job for the admin console."""

        summaries: list[dict[str, object]] = []
        for job in self._jobs:
            stored_key = self._store.get_cadence_key(job.name)
            effective_key = stored_key or job.configured_cadence_key
            cadence = CADENCES_BY_KEY.get(effective_key) if effective_key else None
            summaries.append(
                {
                    "name": job.name,
                    "last_run": self._store.get_last_run(job.name),
                    "next_run": self._store.next_due(job),
                    "running": job.name in self._running,
                    "cadence_key": effective_key,
                    "cadence_label": cadence.label if cadence else job.configured_description,
                    "cadence_is_custom": effective_key is None,
                    "cadence_source": "console" if stored_key else "configuration",
                }
            )
        return summaries

    def set_cadence(self, job_name: str, cadence_key: str) -> bool:
        """Change how often ``job_name`` runs, returning ``False`` if unknown.

        The next run is recalculated from the last successful run, so shortening the
        interval can make a job due straight away — which is what an operator asking
        for "daily" instead of "monthly" means.
        """

        if job_name not in self.job_names:
            return False
        self._store.set_cadence_key(job_name, cadence_key)
        logger.info(
            "Pipeline cadence changed",
            extra={
                "event": "pipeline_scheduler.cadence_changed",
                "job": job_name,
                "cadence": cadence_key,
            },
        )
        return True

    async def trigger(self, job_name: str) -> bool:
        """Start ``job_name`` in the background, returning ``False`` if unknown.

        The caller is an HTTP request and the job takes minutes, so this returns as
        soon as the work is scheduled; the console shows progress via the run times.
        """

        job = next((item for item in self._jobs if item.name == job_name), None)
        if job is None:
            return False
        if job.name in self._running:
            logger.info("Pipeline %s is already running; ignoring trigger", job.name)
            return True

        self._running.add(job.name)

        async def _runner() -> None:
            try:
                async with self._lock:
                    await self._execute(job, _utc_now())
            finally:
                self._running.discard(job.name)

        asyncio.create_task(_runner())
        return True


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
        max_articles = _positive_int(os.getenv("LIVEON_MAX_ARTICLES"), 1)
        pipeline = run_pipeline._build_pipeline(storage, db_path, feed_limit)
        result = pipeline.run(
            limit_per_feed=feed_limit, max_articles=max_articles, published_at=run_at
        )
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

    # The homepage headlines a "Tip of the Day", so a tip run has to happen daily.
    # Articles were weekly, which left the site looking abandoned between runs.
    article_days = _positive_int(os.getenv("LIVEON_ARTICLE_INTERVAL_DAYS"), 1)
    tip_days = _positive_int(os.getenv("LIVEON_TIP_INTERVAL_DAYS"), 1)
    tip_months = _positive_int(os.getenv("LIVEON_TIP_INTERVAL_MONTHS"), 0, minimum=0)
    check_interval = _positive_int(os.getenv("LIVEON_PIPELINE_CHECK_INTERVAL_SEC"), 3600)

    try:
        store = PipelineScheduleStore(_resolve_db_path())
    except Exception:
        logger.exception("Pipeline scheduler unavailable: failed to open schedule store")
        return None
    tip_job = (
        JobConfig(name="tips", runner=_run_tip_pipeline, interval_months=tip_months)
        if tip_months
        else JobConfig(name="tips", runner=_run_tip_pipeline, interval_days=tip_days)
    )
    jobs = [
        JobConfig(name="articles", runner=_run_article_pipeline, interval_days=article_days),
        tip_job,
    ]
    return PipelineScheduler(store, jobs, check_interval_sec=check_interval)
