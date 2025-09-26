"""Agent responsible for aggregating longevity research updates from external feeds."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import logging
from typing import Iterable

import feedparser
import httpx

from app.models.aggregator import AggregatedContent, FeedSource

LOGGER = logging.getLogger(__name__)


Fetcher = Callable[[str], str]


@dataclass(slots=True)
class AggregationResult:
    """Outcome of running the aggregator across configured feeds."""

    items: list[AggregatedContent]
    errors: list[str]


class LongevityNewsAggregator:
    """Fetch longevity-focused articles from configured RSS/Atom feeds."""

    def __init__(self, feeds: Sequence[FeedSource], *, fetcher: Fetcher | None = None) -> None:
        if not feeds:
            raise ValueError("At least one feed must be provided to the aggregator")
        self._feeds = list(feeds)
        self._fetcher = fetcher or self._default_fetcher

    def gather(self, *, limit_per_feed: int = 5) -> AggregationResult:
        """Collect recent updates from each feed, returning a combined result set."""

        collected: list[AggregatedContent] = []
        errors: list[str] = []
        seen_urls: set[str] = set()
        limit = max(0, limit_per_feed)

        for feed in self._feeds:
            try:
                raw_feed = self._fetcher(feed.url)
            except httpx.HTTPError as exc:
                error_message = f"Failed to fetch feed '{feed.name}': {exc}"
                LOGGER.warning(error_message)
                errors.append(error_message)
                continue
            except Exception as exc:  # pragma: no cover - defensive guard
                error_message = f"Unexpected error fetching feed '{feed.name}': {exc}"
                LOGGER.warning(error_message)
                errors.append(error_message)
                continue

            parsed = feedparser.parse(raw_feed)
            if parsed.bozo and parsed.bozo_exception is not None:  # type: ignore[attr-defined]
                error_message = f"Feed '{feed.name}' could not be parsed: {parsed.bozo_exception}"
                LOGGER.warning(error_message)
                errors.append(error_message)
                continue

            entries: Iterable[feedparser.FeedParserDict] = parsed.entries[:limit]
            for entry in entries:
                aggregated = AggregatedContent.from_feed_entry(entry, source=feed)
                if aggregated.url and aggregated.url in seen_urls:
                    continue
                seen_urls.add(aggregated.url)
                collected.append(aggregated)

        collected.sort(key=lambda item: item.published_at, reverse=True)
        return AggregationResult(items=collected, errors=errors)

    @staticmethod
    def _default_fetcher(url: str) -> str:
        """Fetch raw feed content using ``httpx``."""

        response = httpx.get(url, timeout=10.0)
        response.raise_for_status()
        return response.text
