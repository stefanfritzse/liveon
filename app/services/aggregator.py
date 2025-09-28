"""Agent responsible for aggregating longevity research updates from external feeds."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import logging
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import httpx

from app.models.aggregator import AggregatedContent, FeedSource

LOGGER = logging.getLogger(__name__)


Fetcher = Callable[[str], str]

_TRACKING_PARAM_PREFIXES = ("utm_", "mc_", "icid", "oly_", "vero_id")
_TRACKING_PARAM_NAMES = {"fbclid", "gclid", "gs_l", "msclkid", "yclid"}


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
                normalized_url = _normalize_url(aggregated.url)
                if normalized_url:
                    aggregated.url = normalized_url
                dedupe_key = normalized_url or aggregated.url
                if dedupe_key and dedupe_key in seen_urls:
                    continue
                if dedupe_key:
                    seen_urls.add(dedupe_key)
                collected.append(aggregated)

        collected.sort(key=lambda item: item.published_at, reverse=True)
        return AggregationResult(items=collected, errors=errors)

    @staticmethod
    def _default_fetcher(url: str) -> str:
        """Fetch raw feed content using ``httpx``."""

        response = httpx.get(url, timeout=10.0)
        response.raise_for_status()
        return response.text


def _normalize_url(url: str) -> str:
    """Return a canonical form of ``url`` by removing tracking noise."""

    if not url:
        return url

    parsed = urlparse(url)
    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_parameter(key)
    ]
    normalized_query = urlencode(filtered_query, doseq=True)
    normalized_path = parsed.path.rstrip("/") or "/"

    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=normalized_path,
        query=normalized_query,
        fragment="",
    )
    return urlunparse(normalized)


def _is_tracking_parameter(name: str) -> bool:
    lowered = name.lower()
    if lowered in _TRACKING_PARAM_NAMES:
        return True
    return any(lowered.startswith(prefix) for prefix in _TRACKING_PARAM_PREFIXES)
