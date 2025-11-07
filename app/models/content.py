"""Domain models for content stored in Firestore."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from google.cloud.firestore_v1 import DocumentSnapshot


def _default_datetime() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


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

    @classmethod
    def from_document(cls, document: DocumentSnapshot) -> "Article":
        """Create an :class:`Article` from a Firestore document snapshot."""
        data = document.to_dict() or {}
        return cls(
            id=document.id,
            title=data.get("title", ""),
            content_body=data.get("content_body", ""),
            summary=data.get("summary"),
            published_date=data.get("published_date", _default_datetime()),
            source_urls=list(data.get("source_urls", []) or []),
            tags=list(data.get("tags", []) or []),
        )

    def to_document(self) -> dict[str, Any]:
        """Serialise the article to a Firestore document payload."""
        return {
            "title": self.title,
            "content_body": self.content_body,
            "summary": self.summary,
            "published_date": self.published_date,
            "source_urls": list(self.source_urls),
            "tags": list(self.tags),
        }


@dataclass(slots=True)
class Tip:
    """Representation of a short coaching tip stored in the Firestore ``tips`` collection."""

    title: str
    content_body: str
    published_date: datetime = field(default_factory=_default_datetime)
    tags: list[str] = field(default_factory=list)
    id: str | None = None

    @classmethod
    def from_document(cls, document: DocumentSnapshot) -> "Tip":
        """Create a :class:`Tip` from a Firestore document snapshot."""
        data = document.to_dict() or {}
        return cls(
            id=document.id,
            title=data.get("title", ""),
            content_body=data.get("content_body", ""),
            published_date=data.get("published_date", _default_datetime()),
            tags=list(data.get("tags", []) or []),
        )

    def to_document(self) -> dict[str, Any]:
        """Serialise the tip to a Firestore document payload."""
        return {
            "title": self.title,
            "content_body": self.content_body,
            "published_date": self.published_date,
            "tags": list(self.tags),
        }


ContentItem = Article | Tip
