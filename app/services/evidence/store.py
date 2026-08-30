"""The research knowledge store.

Articles and tips used to acquire research independently, which is why they could
disagree about the same finding: each interpreted a different headline snippet. Research
is now ingested once into this store and both products consume the reviewed layer.

Three structural choices are worth keeping in mind when extending this module:

* **Deduplication is a primary key, not a heuristic.** A DOI, its PubMed ID, its PMC ID
  and the publisher URL all resolve through ``evidence_aliases`` to one ``source_key``.
* **Usage is not a lifecycle state.** A record is cited many times over its life, so
  ``state`` stays a pure acquisition/review lifecycle and usage lives in its own table.
  Collapsing the two makes deduplication and the maintenance sweep fight each other.
* **``document_text`` is written once.** Spans index into it. Re-normalising stored text
  later would silently invalidate every span pointing at it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any, Final

from app.models.evidence import SCHEMA_VERSION, EvidenceBundle, EvidenceRecord

LOGGER = logging.getLogger(__name__)

DEFAULT_DB_PATH: Final[Path] = Path.home() / "liveon" / "data" / "content.db"


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Return the database to use, honouring ``LIVEON_DB_PATH``.

    Every other component resolves the path this way. Defaulting to ``DEFAULT_DB_PATH``
    while the application runs against a configured one would silently open — and create —
    a second, empty database, which is exactly the kind of quiet divergence that makes an
    operator conclude the pipeline never ran.
    """

    if db_path is not None:
        return Path(db_path)
    configured = (os.getenv("LIVEON_DB_PATH") or "").strip()
    return Path(configured) if configured else DEFAULT_DB_PATH


#: Roles a source can play in a bundle. "contradicting" is a first-class role because the
#: synthesizer must surface disagreement rather than average it away.
BUNDLE_ROLES: Final[tuple[str, ...]] = ("primary", "supporting", "contradicting")


def _iso(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc).isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: object) -> Any:
    if isinstance(value, (datetime, date)):
        return _iso(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default)


class EvidenceStore:
    """SQLite-backed store for evidence records, bundles, and their usage."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = resolve_db_path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.execute("PRAGMA journal_mode = WAL;")
        self._bootstrap()

    # -- lifecycle -----------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "EvidenceStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _bootstrap(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_sources (
                    source_key       TEXT PRIMARY KEY,
                    source_type      TEXT NOT NULL,
                    state            TEXT NOT NULL,
                    retraction_state TEXT NOT NULL DEFAULT 'none',
                    superseded_by    TEXT,
                    title            TEXT,
                    document_text    TEXT,
                    data             TEXT NOT NULL,
                    first_seen_at    TEXT NOT NULL,
                    retrieved_at     TEXT,
                    updated_at       TEXT NOT NULL,
                    schema_version   INTEGER NOT NULL
                );
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_evidence_sources_state ON evidence_sources(state);"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_aliases (
                    alias      TEXT PRIMARY KEY,
                    source_key TEXT NOT NULL
                        REFERENCES evidence_sources(source_key) ON DELETE CASCADE
                );
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_bundles (
                    bundle_id     TEXT PRIMARY KEY,
                    topic_key     TEXT NOT NULL,
                    grade         TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    data          TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL,
                    run_id        TEXT
                );
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_evidence_bundles_topic ON evidence_bundles(topic_key);"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bundle_sources (
                    bundle_id  TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    role       TEXT NOT NULL,
                    PRIMARY KEY (bundle_id, source_key)
                );
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_usage (
                    source_key   TEXT NOT NULL,
                    bundle_id    TEXT,
                    topic_key    TEXT,
                    content_type TEXT NOT NULL,
                    content_id   TEXT NOT NULL,
                    used_at      TEXT NOT NULL,
                    PRIMARY KEY (source_key, content_type, content_id)
                );
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_evidence_usage_topic ON evidence_usage(topic_key, used_at DESC);"
            )

    # -- records -------------------------------------------------------

    def upsert_record(self, record: EvidenceRecord) -> EvidenceRecord:
        """Insert or update a record, keeping ``first_seen_at`` from the first sighting.

        ``document_text`` is preserved once set: a later acquisition that arrives without
        the text (a metadata-only refresh, say) must not blank the document that existing
        spans point into.
        """

        if not record.source_key:
            raise ValueError("An evidence record requires a source_key")

        existing = self._conn.execute(
            "SELECT first_seen_at, document_text FROM evidence_sources WHERE source_key = ?;",
            (record.source_key,),
        ).fetchone()

        if existing is not None and not record.document_text and existing["document_text"]:
            record.document_text = existing["document_text"]

        first_seen = existing["first_seen_at"] if existing else _now()
        payload = _dumps(record.to_document())

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO evidence_sources (
                    source_key, source_type, state, retraction_state, superseded_by,
                    title, document_text, data, first_seen_at, retrieved_at,
                    updated_at, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    source_type      = excluded.source_type,
                    state            = excluded.state,
                    retraction_state = excluded.retraction_state,
                    superseded_by    = excluded.superseded_by,
                    title            = excluded.title,
                    document_text    = excluded.document_text,
                    data             = excluded.data,
                    retrieved_at     = excluded.retrieved_at,
                    updated_at       = excluded.updated_at,
                    schema_version   = excluded.schema_version;
                """,
                (
                    record.source_key,
                    record.source_type,
                    record.state,
                    record.retraction_state,
                    record.superseded_by,
                    record.title,
                    record.document_text,
                    payload,
                    first_seen,
                    _iso(record.retrieved_at),
                    _now(),
                    record.schema_version or SCHEMA_VERSION,
                ),
            )
            aliases = {record.source_key, *record.aliases}
            self._conn.executemany(
                """
                INSERT INTO evidence_aliases (alias, source_key) VALUES (?, ?)
                ON CONFLICT(alias) DO UPDATE SET source_key = excluded.source_key;
                """,
                [(alias, record.source_key) for alias in aliases if alias],
            )

        return record

    def get_record(self, source_key: str) -> EvidenceRecord | None:
        """Return the record for ``source_key``, or ``None``.

        Loading re-verifies every span against the stored document, so a field whose
        anchor no longer holds comes back as ``not_extractable`` rather than as a fact.
        """

        row = self._conn.execute(
            "SELECT data FROM evidence_sources WHERE source_key = ?;", (source_key,)
        ).fetchone()
        if row is None:
            return None
        return EvidenceRecord.from_document(json.loads(row["data"])).verified()

    def get_records(self, source_keys: Iterable[str]) -> dict[str, EvidenceRecord]:
        """Return the records that exist, keyed by ``source_key``. Missing keys are absent."""

        found: dict[str, EvidenceRecord] = {}
        for key in dict.fromkeys(source_keys):
            record = self.get_record(key)
            if record is not None:
                found[key] = record
        return found

    def resolve(self, identifier: str) -> str | None:
        """Return the canonical ``source_key`` for any known alias, or ``None``."""

        cleaned = (identifier or "").strip()
        if not cleaned:
            return None
        row = self._conn.execute(
            "SELECT source_key FROM evidence_aliases WHERE alias = ?;", (cleaned,)
        ).fetchone()
        return row["source_key"] if row else None

    def exists(self, source_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM evidence_sources WHERE source_key = ?;", (source_key,)
        ).fetchone()
        return row is not None

    def records_in_state(self, state: str, *, limit: int = 50) -> list[EvidenceRecord]:
        rows = self._conn.execute(
            """
            SELECT data FROM evidence_sources
            WHERE state = ?
            ORDER BY first_seen_at ASC
            LIMIT ?;
            """,
            (state, max(1, limit)),
        ).fetchall()
        return [EvidenceRecord.from_document(json.loads(row["data"])).verified() for row in rows]

    def set_state(self, source_key: str, state: str) -> None:
        record = self.get_record(source_key)
        if record is None:
            raise KeyError(source_key)
        record.state = state
        self.upsert_record(record)

    def set_retraction(
        self,
        source_key: str,
        retraction_state: str,
        *,
        notes: Sequence[str] = (),
    ) -> None:
        """Flag a record as retracted, corrected, or under concern.

        Kept orthogonal to ``state``: a retracted record stays ``approved`` in lifecycle
        terms, and G6 is what stops it reaching publication. Overwriting the lifecycle
        would lose the fact that it was once reviewed and used.
        """

        record = self.get_record(source_key)
        if record is None:
            raise KeyError(source_key)
        record.retraction_state = retraction_state
        if notes:
            record.retraction_notes = [*record.retraction_notes, *notes]
        self.upsert_record(record)

    # -- bundles -------------------------------------------------------

    def save_bundle(
        self,
        bundle: EvidenceBundle,
        *,
        roles: dict[str, str] | None = None,
    ) -> EvidenceBundle:
        """Persist a bundle and its source roles."""

        if not bundle.bundle_id:
            raise ValueError("An evidence bundle requires a bundle_id")

        assigned = roles or {}
        unknown = {role for role in assigned.values() if role not in BUNDLE_ROLES}
        if unknown:
            # A typo would otherwise become data, and "contradicting" silently
            # degrading to something unrecognised is how disagreement gets lost.
            raise ValueError(
                f"Unknown bundle role(s) {sorted(unknown)}; expected one of {list(BUNDLE_ROLES)}"
            )
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO evidence_bundles (
                    bundle_id, topic_key, grade, review_status, data,
                    created_at, updated_at, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bundle_id) DO UPDATE SET
                    topic_key     = excluded.topic_key,
                    grade         = excluded.grade,
                    review_status = excluded.review_status,
                    data          = excluded.data,
                    updated_at    = excluded.updated_at,
                    run_id        = excluded.run_id;
                """,
                (
                    bundle.bundle_id,
                    bundle.topic_key,
                    bundle.grade,
                    bundle.review_status,
                    _dumps(bundle.to_document()),
                    _iso(bundle.created_at),
                    _now(),
                    bundle.run_id,
                ),
            )
            self._conn.execute(
                "DELETE FROM bundle_sources WHERE bundle_id = ?;", (bundle.bundle_id,)
            )
            self._conn.executemany(
                "INSERT INTO bundle_sources (bundle_id, source_key, role) VALUES (?, ?, ?);",
                [
                    (bundle.bundle_id, key, assigned.get(key, "supporting"))
                    for key in bundle.source_keys()
                ],
            )
        return bundle

    def get_bundle(self, bundle_id: str) -> EvidenceBundle | None:
        row = self._conn.execute(
            "SELECT data FROM evidence_bundles WHERE bundle_id = ?;", (bundle_id,)
        ).fetchone()
        if row is None:
            return None
        return EvidenceBundle.from_document(json.loads(row["data"]))

    def approved_bundles(self, topic_prefix: str, *, limit: int = 3) -> list[EvidenceBundle]:
        """Approved bundles whose topic starts with ``topic_prefix``, newest first.

        Topic keys are ``intervention|outcome``, so a prefix match on the intervention
        returns everything reviewed about it whatever endpoint was measured — which is
        what someone asking "does fasting help?" is actually asking.
        """

        if not (topic_prefix or "").strip() or limit <= 0:
            return []

        rows = self._conn.execute(
            """
            SELECT data FROM evidence_bundles
            WHERE review_status IN ('approved', 'downgraded')
              AND grade != 'insufficient'
              AND (topic_key = ? OR topic_key LIKE ?)
            ORDER BY created_at DESC
            LIMIT ?;
            """,
            (topic_prefix, f"{topic_prefix}|%", limit),
        ).fetchall()
        return [EvidenceBundle.from_document(json.loads(row["data"])) for row in rows]

    def bundle_roles(self, bundle_id: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT source_key, role FROM bundle_sources WHERE bundle_id = ?;", (bundle_id,)
        ).fetchall()
        return {row["source_key"]: row["role"] for row in rows}

    # -- usage ---------------------------------------------------------

    def record_usage(
        self,
        *,
        source_keys: Iterable[str],
        content_type: str,
        content_id: str,
        bundle_id: str | None = None,
        topic_key: str | None = None,
        used_at: datetime | None = None,
    ) -> None:
        """Note that published content cites these sources.

        This is what lets the maintenance sweep find affected content when a paper is
        retracted, and what G9 checks to stop the same finding being republished.
        """

        moment = _iso(used_at or datetime.now(timezone.utc))
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO evidence_usage (
                    source_key, bundle_id, topic_key, content_type, content_id, used_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key, content_type, content_id) DO UPDATE SET
                    bundle_id = excluded.bundle_id,
                    topic_key = excluded.topic_key,
                    used_at   = excluded.used_at;
                """,
                [
                    (key, bundle_id, topic_key, content_type, content_id, moment)
                    for key in dict.fromkeys(source_keys)
                    if key
                ],
            )

    def usage_for_source(self, source_key: str) -> list[dict[str, Any]]:
        """Every piece of published content citing ``source_key``."""

        rows = self._conn.execute(
            """
            SELECT content_type, content_id, bundle_id, topic_key, used_at
            FROM evidence_usage WHERE source_key = ? ORDER BY used_at DESC;
            """,
            (source_key,),
        ).fetchall()
        return [dict(row) for row in rows]

    def last_used_at(self, topic_key: str) -> datetime | None:
        """When this topic was last published, for the G9 repetition window."""

        row = self._conn.execute(
            "SELECT MAX(used_at) AS latest FROM evidence_usage WHERE topic_key = ?;",
            (topic_key,),
        ).fetchone()
        if row is None or not row["latest"]:
            return None
        text = row["latest"]
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def create_store(db_path: str | Path | None = None) -> EvidenceStore:
    """Build a store at ``db_path``, falling back to the shared content database."""

    return EvidenceStore(db_path)
