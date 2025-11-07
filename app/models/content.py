"""Domain models for content stored in the database."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _default_datetime() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Article:
    """Representation of an article."""

    title: str
    content_body: str
    summary: str | None = None
    published_date: datetime = field(default_factory=_default_datetime)
    source_urls: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    id: str | None = None


@dataclass(slots=True)
class Tip:
    """Representation of a short coaching tip."""

    title: str
    content_body: str
    published_date: datetime = field(default_factory=_default_datetime)
    tags: list[str] = field(default_factory=list)
    id: str | None = None


ContentItem = Article | Tip
