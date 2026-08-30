"""Record what each pipeline run did, and why.

The question this exists to answer is the one that arrives six months late: *why did the
system publish that?* Answering it means being able to reconstruct the run — which sources
were considered, how they ranked, what the reviewer decided, which claims were dropped,
and which model and prompt produced each step.

Timestamps are kept apart deliberately. The old article pipeline used a feed's publication
date as its own, so a write-up of a three-year-old study claimed to have been published in
2023. Three different things are recorded separately:

* ``source_published_at`` — when the study appeared (on the evidence record)
* ``retrieved_at`` — when we fetched it (on the evidence record)
* ``published_date`` — when Live On published (on the article or tip)

and a fourth, ``started_at`` here, for when the run happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable
from uuid import uuid4

LOGGER = logging.getLogger(__name__)

__all__ = ["RunLog", "RunRecord", "retention_days"]

_DEFAULT_RETENTION_DAYS = 365


def retention_days() -> int:
    raw = (os.getenv("LIVEON_RUN_RETENTION_DAYS") or "").strip()
    try:
        return max(1, int(raw)) if raw else _DEFAULT_RETENTION_DAYS
    except ValueError:
        LOGGER.warning("Ignoring invalid LIVEON_RUN_RETENTION_DAYS=%r", raw)
        return _DEFAULT_RETENTION_DAYS


def _iso(value: datetime) -> str:
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


@dataclass(slots=True)
class RunRecord:
    """One pipeline run, as stored."""

    run_id: str
    job: str
    started_at: datetime
    finished_at: datetime | None = None
    outcome: str | None = None
    model_id: str | None = None
    prompt_versions: dict[str, str] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)


class RunLog:
    """Append-only record of pipeline runs and the events inside them."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        from app.services.evidence.store import DEFAULT_DB_PATH

        self._db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode = WAL;")
        self._bootstrap()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "RunLog":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _bootstrap(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    run_id          TEXT PRIMARY KEY,
                    job             TEXT NOT NULL,
                    started_at      TEXT NOT NULL,
                    finished_at     TEXT,
                    outcome         TEXT,
                    model_id        TEXT,
                    prompt_versions TEXT,
                    data            TEXT
                );
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started ON pipeline_runs(started_at DESC);"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL,
                    seq    INTEGER NOT NULL,
                    stage  TEXT NOT NULL,
                    event  TEXT NOT NULL,
                    data   TEXT,
                    at     TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );
                """
            )

    # -- writing -------------------------------------------------------

    def start(self, job: str, *, now: datetime | None = None, run_id: str | None = None) -> str:
        """Open a run and return its id."""

        identifier = run_id or uuid4().hex
        moment = now or datetime.now(timezone.utc)
        with self._conn:
            self._conn.execute(
                "INSERT INTO pipeline_runs (run_id, job, started_at) VALUES (?, ?, ?);",
                (identifier, job, _iso(moment)),
            )
        return identifier

    def event(
        self,
        run_id: str,
        stage: str,
        event: str,
        data: Any = None,
        *,
        now: datetime | None = None,
    ) -> None:
        """Append one event. Failures here never fail the run.

        A pipeline that cannot write its diary should still publish; a pipeline that
        stops publishing because the diary is full is worse than one with a gap in it.
        """

        moment = now or datetime.now(timezone.utc)
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO run_events (run_id, seq, stage, event, data, at)
                    VALUES (
                        ?,
                        COALESCE((SELECT MAX(seq) + 1 FROM run_events WHERE run_id = ?), 1),
                        ?, ?, ?, ?
                    );
                    """,
                    (run_id, run_id, stage, event, _dumps(data) if data is not None else None, _iso(moment)),
                )
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            LOGGER.warning(
                "Could not record run event: %s",
                exc,
                extra={"event": "runlog.write_failed", "run_id": run_id},
            )

    def finish(
        self,
        run_id: str,
        outcome: str,
        *,
        model_id: str | None = None,
        prompt_versions: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        """Close a run with its outcome."""

        moment = now or datetime.now(timezone.utc)
        with self._conn:
            self._conn.execute(
                """
                UPDATE pipeline_runs
                SET finished_at = ?, outcome = ?, model_id = ?, prompt_versions = ?, data = ?
                WHERE run_id = ?;
                """,
                (
                    _iso(moment),
                    outcome,
                    model_id,
                    _dumps(prompt_versions or {}),
                    _dumps(data or {}),
                    run_id,
                ),
            )

    # -- reading -------------------------------------------------------

    def get(self, run_id: str) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?;", (run_id,)
        ).fetchone()
        if row is None:
            return None
        started = _parse(row["started_at"])
        return RunRecord(
            run_id=row["run_id"],
            job=row["job"],
            started_at=started or datetime.now(timezone.utc),
            finished_at=_parse(row["finished_at"]),
            outcome=row["outcome"],
            model_id=row["model_id"],
            prompt_versions=json.loads(row["prompt_versions"] or "{}"),
            data=json.loads(row["data"] or "{}"),
        )

    def events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, stage, event, data, at FROM run_events WHERE run_id = ? ORDER BY seq;",
            (run_id,),
        ).fetchall()
        return [
            {
                "seq": row["seq"],
                "stage": row["stage"],
                "event": row["event"],
                "data": json.loads(row["data"]) if row["data"] else None,
                "at": _parse(row["at"]),
            }
            for row in rows
        ]

    def recent(self, *, job: str | None = None, limit: int = 20) -> list[RunRecord]:
        sql = "SELECT run_id FROM pipeline_runs"
        params: list[Any] = []
        if job:
            sql += " WHERE job = ?"
            params.append(job)
        sql += " ORDER BY started_at DESC LIMIT ?;"
        params.append(max(1, limit))

        rows = self._conn.execute(sql, params).fetchall()
        records = [self.get(row["run_id"]) for row in rows]
        return [record for record in records if record is not None]

    def prune(self, *, now: datetime | None = None, days: int | None = None) -> int:
        """Delete runs older than the retention window. Returns how many went."""

        cutoff = (now or datetime.now(timezone.utc)) - timedelta(
            days=days if days is not None else retention_days()
        )
        with self._conn:
            stale: Iterable[sqlite3.Row] = self._conn.execute(
                "SELECT run_id FROM pipeline_runs WHERE started_at < ?;", (_iso(cutoff),)
            ).fetchall()
            ids = [row["run_id"] for row in stale]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"DELETE FROM run_events WHERE run_id IN ({placeholders});", ids
                )
                self._conn.execute(
                    f"DELETE FROM pipeline_runs WHERE run_id IN ({placeholders});", ids
                )
        return len(ids)
