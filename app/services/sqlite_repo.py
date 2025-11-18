"""SQLite-backed content repository for the Live On app.

This repository provides the following services that the pipeline/publisher
rely on:
  - get_article, save_article, find_article_by_source_url
  - get_latest_articles
  - (tips) get_tip, save_tip, get_latest_tips, get_latest_tip, find_tip_by_title, find_tip_by_tags
  - seed_if_empty, create_repository
It also exposes .article_collection/.tip_collection with an `.id` attribute for callers
that format paths.

Design notes
------------
- Data is stored verbatim as the model's `to_document()` JSON in a `data` column.
- We construct a tiny “snapshot” shim to call `Article.from_document(...)` / `Tip.from_document(...)` so no model changes are needed.
- Minimal secondary columns (title, published_date) are denormalized for ordering and lookups.
- Source URL lookups use a small side table `article_sources(normalized_url -> article_id)`.
- WAL mode and foreign_keys are enabled. Suitable for single-writer, multi-reader local use.

Default location (if not provided):  ~/liveon/data/content.db
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Final
from datetime import datetime, date, timezone
from pathlib import Path

# Model types
from app.models.content import Article, Tip

# Try to reuse the aggregator’s normalization if available
try:
    # local project path: app/services/aggregator.py
    from app.services.aggregator import _normalise_url as _normalise_url  # type: ignore
except Exception:  # pragma: no cover - defensive fallback
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

    def _normalise_url(url: str | None) -> str:
        """Conservative URL normalizer (lowercase host, strip tracking params)."""
        if not url:
            return ""
        parsed = urlparse(url)
        # Drop common tracking params
        query = [(k, v) for (k, v) in parse_qsl(parsed.query, keep_blank_values=True)
                 if not k.lower().startswith(("utm_", "gclid", "fbclid"))]
        normalized = parsed._replace(
            netloc=parsed.netloc.lower(),
            fragment="",
            query=urlencode(query)
        )
        return urlunparse(normalized).strip()


DEFAULT_DB_PATH: Final[Path] = Path.home() / "liveon" / "data" / "content.db"
DEFAULT_ARTICLES_TABLE: Final[str] = "articles"
DEFAULT_TIPS_TABLE: Final[str] = "tips"
DEFAULT_ARTICLES_COLLECTION: Final[str] = "articles"
DEFAULT_TIPS_COLLECTION: Final[str] = "tips"


# ---------------------------------------------------------------------------
# Small helpers that emulate document snapshots and collections
# ---------------------------------------------------------------------------

class _DictSnapshot:
    """Duck-type of DocumentSnapshot for model.from_document(...)"""

    __slots__ = ("id", "_data", "exists")

    def __init__(self, doc_id: str, data: dict[str, Any]) -> None:
        self.id = doc_id
        self._data = data
        self.exists = True

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


@dataclass(slots=True)
class _CollectionRef:
    """Minimal collection reference with `.id` used by publishers for path formatting."""
    id: str

def _to_iso8601(value: datetime | date) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    # date -> midnight UTC ISO
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc).isoformat()

def _json_default(o: object):
    if isinstance(o, (datetime, date)):
        return _to_iso8601(o)
    if isinstance(o, set):
        return list(o)
    if isinstance(o, Path):
        return str(o)
    # last-resort fallback so debug structs never crash
    return str(o)

def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=_json_default)

def _json_or_none(txt: str | None) -> dict[str, Any] | None:
    if not txt:
        return None
    return json.loads(txt)


def _iso_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class LocalSQLiteContentRepository:
    """SQLite repository that mirrors the FirestoreContentRepository surface."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        article_table: str = DEFAULT_ARTICLES_TABLE,
        tip_table: str = DEFAULT_TIPS_TABLE,
        article_collection: str = DEFAULT_ARTICLES_COLLECTION,
        tip_collection: str = DEFAULT_TIPS_COLLECTION,
    ) -> None:
        self._db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._article_table = article_table
        self._tip_table = tip_table
        self._article_collection = _CollectionRef(article_collection)
        self._tip_collection = _CollectionRef(tip_collection)

        # Ensure directory exists
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.execute("PRAGMA journal_mode = WAL;")
        self._bootstrap()

    # --- schema ----------------------------------------------------------------

    def _bootstrap(self) -> None:
        """Create required tables and indexes if they don't exist."""
        with self._conn:
            # Articles
            self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._article_table} (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    published_date TEXT,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Index for newest-first queries
            self._conn.execute(
                f"""CREATE INDEX IF NOT EXISTS idx_{self._article_table}_published
                    ON {self._article_table}(published_date DESC);"""
            )
            # URLs index table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS article_sources (
                    normalized_url TEXT PRIMARY KEY,
                    article_id TEXT NOT NULL,
                    FOREIGN KEY(article_id) REFERENCES articles(id) ON DELETE CASCADE
                );
                """
            )

            # Tips
            self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._tip_table} (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    published_date TEXT,
                    tags_json TEXT,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._conn.execute(
                f"""CREATE INDEX IF NOT EXISTS idx_{self._tip_table}_published
                    ON {self._tip_table}(published_date DESC);"""
            )
            self._conn.execute(
                f"""CREATE INDEX IF NOT EXISTS idx_{self._tip_table}_title
                    ON {self._tip_table}(title);"""
            )

    # --- properties -------------------------------------------------------------

    @property
    def article_collection(self) -> _CollectionRef:
        return self._article_collection

    @property
    def tip_collection(self) -> _CollectionRef:
        return self._tip_collection

    # --- Article helpers --------------------------------------------------------

    def get_latest_articles(self, *, limit: int = 5) -> list[Article]:
        """Return newest articles ordered by published_date DESC."""
        rows = self._conn.execute(
            f"""
            SELECT id, data FROM {self._article_table}
            ORDER BY
              CASE WHEN published_date IS NULL THEN 1 ELSE 0 END,
              published_date DESC,
              created_at DESC
            LIMIT ?;
            """,
            (int(limit),),
        ).fetchall()

        return [self._row_to_article(r) for r in rows]

    def get_article(self, article_id: str) -> Article | None:
        row = self._conn.execute(
            f"SELECT id, data FROM {self._article_table} WHERE id = ?;",
            (article_id,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_article(row)

    def _extract_article_index_fields(self, doc: dict[str, Any]) -> tuple[str | None, str | None]:
        title = doc.get("title")
        published_date = doc.get("published_date") or doc.get("published_at")
        if isinstance(published_date, (datetime, date)):
            published_date = _to_iso8601(published_date)
        if isinstance(published_date, (dict, list)):
            published_date = None
        return (title, published_date)

    def save_article(self, article: Article) -> Article:
        """Persist an article and return the stored representation."""
        # Keep parity with Firestore path by using model's dict
        doc = article.to_document()
        # Ensure an id
        art_id = getattr(article, "id", None) or doc.get("id") or self._generate_id()
        title, published_date = self._extract_article_index_fields(doc)

        now = _iso_now()
        payload = _json(doc)

        with self._conn:
            # Upsert
            self._conn.execute(
                f"""
                INSERT INTO {self._article_table}(id, title, published_date, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    published_date = excluded.published_date,
                    data = excluded.data,
                    updated_at = excluded.updated_at;
                """,
                (art_id, title, published_date, payload, now, now),
            )
            # Rebuild URL index for this article
            self._conn.execute(
                "DELETE FROM article_sources WHERE article_id = ?;",
                (art_id,),
            )
            for url in (doc.get("source_urls") or doc.get("sources") or []):
                if not isinstance(url, str):
                    continue
                norm = _normalise_url(url)
                if not norm:
                    continue
                self._conn.execute(
                    "INSERT OR IGNORE INTO article_sources(normalized_url, article_id) VALUES (?, ?);",
                    (norm, art_id),
                )

        # Return stored version
        return self.get_article(art_id) or self._row_to_article({"id": art_id, "data": payload})

    def find_article_by_source_url(self, url: str) -> Article | None:
        """Return an article that references the (normalized) URL, if any."""
        norm = _normalise_url(url)
        if not norm:
            return None
        row = self._conn.execute(
            """
            SELECT a.id, a.data
            FROM article_sources s
            JOIN articles a ON a.id = s.article_id
            WHERE s.normalized_url = ?;
            """,
            (norm,),
        ).fetchone()
        return self._row_to_article(row) if row else None

    # --- Tip helpers ------------------------------------------------------------

    def get_latest_tips(self, *, limit: int = 5) -> list[Tip]:
        rows = self._conn.execute(
            f"""
            SELECT id, data FROM {self._tip_table}
            ORDER BY
              CASE WHEN published_date IS NULL THEN 1 ELSE 0 END,
              published_date DESC,
              created_at DESC
            LIMIT ?;
            """,
            (int(limit),),
        ).fetchall()
        return [self._row_to_tip(r) for r in rows]

    def get_latest_tip(self) -> Tip | None:
        row = self._conn.execute(
            f"""
            SELECT id, data FROM {self._tip_table}
            ORDER BY
              CASE WHEN published_date IS NULL THEN 1 ELSE 0 END,
              published_date DESC,
              created_at DESC
            LIMIT 1;
            """
        ).fetchone()
        return self._row_to_tip(row) if row else None

    def get_tip(self, tip_id: str) -> Tip | None:
        row = self._conn.execute(
            f"SELECT id, data FROM {self._tip_table} WHERE id = ?;",
            (tip_id,),
        ).fetchone()
        return self._row_to_tip(row) if row else None

    def save_tip(self, tip: Tip) -> Tip:
        doc = tip.to_document()
        tip_id = getattr(tip, "id", None) or doc.get("id") or self._generate_id()
        title = doc.get("title")
        published_date = doc.get("published_date") or doc.get("published_at")
        if isinstance(published_date, (datetime, date)):
            published_date = _to_iso8601(published_date)
        tags = [t for t in (doc.get("tags") or []) if isinstance(t, str)]
        tags_json = _json(tags) if tags else None
        payload = _json(doc)
        now = _iso_now()

        with self._conn:
            self._conn.execute(
                f"""
                INSERT INTO {self._tip_table}(id, title, published_date, tags_json, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    published_date = excluded.published_date,
                    tags_json = excluded.tags_json,
                    data = excluded.data,
                    updated_at = excluded.updated_at;
                """,
                (tip_id, title, published_date, tags_json, payload, now, now),
            )

        return self.get_tip(tip_id) or self._row_to_tip({"id": tip_id, "data": payload})

    def find_tip_by_title(self, title: str) -> Tip | None:
        row = self._conn.execute(
            f"""
            SELECT id, data FROM {self._tip_table}
            WHERE title = ? COLLATE NOCASE
            LIMIT 1;
            """,
            (title,),
        ).fetchone()
        return self._row_to_tip(row) if row else None

    def find_tip_by_tags(self, tags: Sequence[str] | Iterable[str]) -> Tip | None:
        """Return a tip whose tag set matches exactly."""
        if isinstance(tags, str):
            tag_list = [tags.strip()] if tags.strip() else []
        else:
            # normalize + filter empties
            tag_list = [t.strip() for t in list(tags) if isinstance(t, str) and t.strip()]

        if not tag_list:
            return None

        first = tag_list[0]
        like = f'%"{first}"%'
        rows = self._conn.execute(
            f"""
            SELECT id, data FROM {self._tip_table}
            WHERE tags_json LIKE ?
            LIMIT 20;
            """,
            (like,),
        ).fetchall()

        sought = set(tag_list)
        for r in rows:
            data = _json_or_none(r["data"]) or {}
            candidate = set([t for t in (data.get("tags") or []) if isinstance(t, str)])
            if candidate == sought:
                return self._row_to_tip(r)
        return None

    # --- Seed utility -----------------------------------------------------------

    def seed_if_empty(
        self,
        *,
        articles: Iterable[Article] = (),
        tips: Iterable[Tip] = (),
    ) -> dict[str, list[str]]:
        """Seed the database if empty, returning created IDs by collection."""
        created_articles: list[str] = []
        created_tips: list[str] = []

        if self._is_table_empty(self._article_table):
            for a in articles:
                stored = self.save_article(a)
                created_articles.append(stored.id or "")

        if self._is_table_empty(self._tip_table):
            for t in tips:
                stored = self.save_tip(t)
                created_tips.append(stored.id or "")

        return {"articles": created_articles, "tips": created_tips}

    def _is_table_empty(self, table: str) -> bool:
        row = self._conn.execute(f"SELECT 1 FROM {table} LIMIT 1;").fetchone()
        return row is None

    # --- Factories --------------------------------------------------------------

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # --- Conversions ------------------------------------------------------------

    def _row_to_article(self, row: sqlite3.Row | dict | None) -> Article:
        if not row:
            raise KeyError("Article row not found")
        row_id = row["id"] if isinstance(row, sqlite3.Row) else row.get("id")
        data_txt = row["data"] if isinstance(row, sqlite3.Row) else row.get("data")
        data = _json_or_none(data_txt) or {}
        snap = _DictSnapshot(str(row_id), data)
        return Article.from_document(snap)

    def _row_to_tip(self, row: sqlite3.Row | dict | None) -> Tip:
        if not row:
            raise KeyError("Tip row not found")
        row_id = row["id"] if isinstance(row, sqlite3.Row) else row.get("id")
        data_txt = row["data"] if isinstance(row, sqlite3.Row) else row.get("data")
        data = _json_or_none(data_txt) or {}
        snap = _DictSnapshot(str(row_id), data)
        return Tip.from_document(snap)

    # --- IDs --------------------------------------------------------------------

    @staticmethod
    def _generate_id() -> str:
        # Compact, URL-safe-ish 20-char id
        import secrets
        return secrets.token_urlsafe(15).replace("-", "_").replace(".", "_")


def create_repository(**kwargs: Any) -> LocalSQLiteContentRepository:
    """Factory helper to create a repository instance."""
    # Allow env overrides for convenience
    db_path = kwargs.pop("db_path", None) or os.getenv("LIVEON_DB_PATH")
    return LocalSQLiteContentRepository(db_path=db_path, **kwargs)
