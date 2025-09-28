"""Data models supporting the longevity content aggregation agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import feedparser


@dataclass(slots=True)
class FeedSource:
    """Configuration for a single external content feed."""

    name: str
    url: str
    topic: str | None = None


@dataclass(slots=True)
class AggregatedContent:
    """Structured representation of raw longevity updates gathered by agents."""

    title: str
    url: str
    summary: str
    published_at: datetime
    source: str
    topic: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_feed_entry(cls, entry: feedparser.FeedParserDict, source: FeedSource) -> "AggregatedContent":
        """Build an :class:`AggregatedContent` instance from a feedparser entry."""

        title = (entry.get("title") or "").strip() or "Untitled update"
        link = (entry.get("link") or "").strip()
        summary = _coerce_summary(entry)
        published_at = _coerce_datetime(entry)

        return cls(
            title=title,
            url=link,
            summary=summary,
            published_at=published_at,
            source=source.name,
            topic=source.topic,
            raw=dict(entry),
        )


def _coerce_datetime(entry: Mapping[str, Any]) -> datetime:
    """Extract a timezone-aware timestamp from a feed entry."""

    for candidate_key in ("published_parsed", "updated_parsed"):
        value = entry.get(candidate_key)
        if value is not None:
            return datetime(*value[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _coerce_summary(entry: Mapping[str, Any]) -> str:
    """Return a meaningful summary from varied feed entry payloads."""

    def _clean(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None

    for key in ("summary", "description"):
        cleaned = _clean(entry.get(key))
        if cleaned:
            return cleaned

    content_value = entry.get("content")
    if isinstance(content_value, Sequence):
        for item in content_value:
            if isinstance(item, Mapping):
                cleaned = _clean(item.get("value"))
                if cleaned:
                    return cleaned

    return ""
