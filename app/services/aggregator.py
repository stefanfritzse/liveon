"""Agent responsible for aggregating longevity research updates from external feeds."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import logging
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
        seen_signatures: set[str] = set()
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
                normalized_url = _normalise_url(aggregated.url)
                if normalized_url:
                    aggregated.url = normalized_url
                    signature = f"url::{normalized_url}"
                else:
                    signature = _text_signature(aggregated)
                if signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                collected.append(aggregated)

        collected.sort(key=lambda item: item.published_at, reverse=True)
        return AggregationResult(items=collected, errors=errors)

    @staticmethod
    def _default_fetcher(url: str) -> str:
        """Fetch raw feed content using ``httpx``."""

        response = httpx.get(url, timeout=10.0)
        response.raise_for_status()
        return response.text


def _normalise_url(url: str | None) -> str:
    """Normalise feed URLs to improve duplicate detection.

    The normalisation is intentionally conservative: it lowercases the scheme
    and host, removes default ports, strips fragments, collapses empty paths to
    ``/`` and orders query parameters. Empty or malformed URLs are returned
    unchanged so that they can be handled by the textual fallback logic.
    """

    if not url:
        return ""

    stripped = url.strip()
    if not stripped:
        return ""

    parsed = urlsplit(stripped)
    if not parsed.scheme or not parsed.netloc:
        return stripped

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    if ":" in netloc:
        host, _, port = netloc.rpartition(":")
        if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
            netloc = host

    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")

    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    query = urlencode(sorted(query_items))

    return urlunsplit((scheme, netloc, path, query, ""))


def _text_signature(content: AggregatedContent) -> str:
    """Return a textual signature for feed entries lacking canonical URLs."""

    title = (content.title or "").strip().lower()
    summary = (content.summary or "").strip().lower()
    return f"text::{title}::{summary}"
