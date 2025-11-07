"""Domain models for application content."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


class DocumentSnapshot(Protocol):
    """Duck-typed version of a Firestore DocumentSnapshot."""

    id: str

    def to_dict(self) -> dict[str, Any] | None:
        ...


def _default_datetime() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Article:
    """Domain model for a promotable article."""

    title: str
    content_body: str
    summary: str | None = None
    published_date: datetime = field(default_factory=_default_datetime)
    source_urls: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    id: str | None = None

    @classmethod
    def from_document(cls, document: DocumentSnapshot) -> "Article":
        """Create an :class:`Article` from a document snapshot."""
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
        """Serialise the article to a document payload."""
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
    """Domain model for a short coaching tip."""

    title: str
    content_body: str
    published_date: datetime = field(default_factory=_default_datetime)
    tags: list[str] = field(default_factory=list)
    id: str | None = None

    @classmethod
    def from_document(cls, document: DocumentSnapshot) -> "Tip":
        """Create a :class:`Tip` from a document snapshot."""
        data = document.to_dict() or {}
        return cls(
            id=document.id,
            title=data.get("title", ""),
            content_body=data.get("content_body", ""),
            published_date=data.get("published_date", _default_datetime()),
            tags=list(data.get("tags", []) or []),
        )

    def to_document(self) -> dict[str, Any]:
        """Serialise the tip to a document payload."""
        return {
            "title": self.title,
            "content_body": self.content_body,
            "published_date": self.published_date,
            "tags": list(self.tags),
        }


ContentItem = Article | Tip
