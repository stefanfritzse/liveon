"""Domain models for content stored in the database."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Sequence


def _default_datetime() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    """Coerce a string/date/datetime value into a timezone-aware UTC datetime."""

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _listify_strings(value: Any) -> list[str]:
    """Normalise a value into a list of non-empty strings."""

    if isinstance(value, str):
        trimmed = value.strip()
        return [trimmed] if trimmed else []

    if isinstance(value, Sequence):
        result: list[str] = []
        for item in value:
            if isinstance(item, str):
                trimmed = item.strip()
                if trimmed:
                    result.append(trimmed)
        return result

    return []


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _text_value(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _snapshot_data(snapshot: Any) -> dict[str, Any]:
    if snapshot is None:
        return {}

    to_dict = getattr(snapshot, "to_dict", None)
    if callable(to_dict):
        return to_dict() or {}

    if isinstance(snapshot, dict):
        return dict(snapshot)

    return {}


def _snapshot_id(snapshot: Any) -> str | None:
    if snapshot is None:
        return None

    identifier = getattr(snapshot, "id", None)
    if identifier is not None:
        return str(identifier)

    if isinstance(snapshot, dict):
        candidate = snapshot.get("id")
        if candidate is not None:
            return str(candidate)

    return None


@dataclass(slots=True)
class Article:
    """Representation of an article stored in the Firestore ``articles`` collection."""

    title: str
    content_body: str
    summary: str | None = None
    published_date: datetime = field(default_factory=_default_datetime)
    source_urls: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    id: str | None = None

    def to_document(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": self.title,
            "content_body": self.content_body,
            "summary": self.summary,
            "published_date": self.published_date,
            "source_urls": list(self.source_urls),
            "tags": list(self.tags),
        }
        if self.id:
            payload["id"] = self.id
        return payload

    @classmethod
    def from_document(cls, snapshot: Any) -> "Article":
        data = _snapshot_data(snapshot)
        doc_id = _snapshot_id(snapshot) or _optional_str(data.get("id"))

        title = _text_value(data.get("title"))
        summary = _optional_str(data.get("summary"))
        content_body = _text_value(data.get("content_body") or data.get("body") or data.get("content"))
        published = _parse_datetime(data.get("published_date") or data.get("published_at")) or _default_datetime()
        source_urls = _listify_strings(data.get("source_urls") or data.get("sources"))
        tags = _listify_strings(data.get("tags"))

        return cls(
            title=title or "",
            summary=summary,
            content_body=content_body,
            published_date=published,
            source_urls=source_urls,
            tags=tags,
            id=doc_id,
        )


@dataclass(slots=True)
class Tip:
    """Representation of a short coaching tip stored in the Firestore ``tips`` collection."""

    title: str
    content_body: str
    published_date: datetime = field(default_factory=_default_datetime)
    tags: list[str] = field(default_factory=list)
    id: str | None = None

    def to_document(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": self.title,
            "content_body": self.content_body,
            "published_date": self.published_date,
            "tags": list(self.tags),
        }
        if self.id:
            payload["id"] = self.id
        return payload

    @classmethod
    def from_document(cls, snapshot: Any) -> "Tip":
        data = _snapshot_data(snapshot)
        doc_id = _snapshot_id(snapshot) or _optional_str(data.get("id"))

        title = _text_value(data.get("title"))
        content_body = _text_value(data.get("content_body") or data.get("body") or data.get("content"))
        published = _parse_datetime(data.get("published_date") or data.get("published_at")) or _default_datetime()
        tags = _listify_strings(data.get("tags"))

        return cls(
            title=title or "",
            content_body=content_body,
            published_date=published,
            tags=tags,
            id=doc_id,
        )


ContentItem = Article | Tip
