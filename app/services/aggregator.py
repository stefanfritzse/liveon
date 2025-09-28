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

        index_by_url: dict[str, int] = {}
        index_by_guid: dict[str, int] = {}
        index_by_signature: dict[tuple[str, str], int] = {}
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
                signature = _signature_key(aggregated)
                guid = _guid_key(aggregated)

                if normalized_url:
                    existing_index = index_by_url.get(normalized_url)
                    if existing_index is not None:
                        existing = collected[existing_index]
                        if _should_replace(existing, aggregated):
                            _remove_indexes(index_by_url, index_by_guid, index_by_signature, existing)
                            collected[existing_index] = aggregated
                            _record_indexes(
                                index_by_url,
                                index_by_guid,
                                index_by_signature,
                                normalized_url,
                                guid,
                                signature,
                                existing_index,
                            )
                        continue

                if guid:
                    existing_index = index_by_guid.get(guid)
                    if existing_index is not None:
                        existing = collected[existing_index]
                        if _should_replace(existing, aggregated):
                            _remove_indexes(index_by_url, index_by_guid, index_by_signature, existing)
                            collected[existing_index] = aggregated
                            _record_indexes(
                                index_by_url,
                                index_by_guid,
                                index_by_signature,
                                normalized_url,
                                guid,
                                signature,
                                existing_index,
                            )
                        continue

                existing_index = index_by_signature.get(signature)
                if existing_index is not None:
                    existing = collected[existing_index]
                    if _should_replace(existing, aggregated):
                        _remove_indexes(index_by_url, index_by_guid, index_by_signature, existing)
                        collected[existing_index] = aggregated
                        _record_indexes(
                            index_by_url,
                            index_by_guid,
                            index_by_signature,
                            normalized_url,
                            guid,
                            signature,
                            existing_index,
                        )
                    continue

                collected.append(aggregated)
                index = len(collected) - 1
                _record_indexes(
                    index_by_url,
                    index_by_guid,
                    index_by_signature,
                    normalized_url,
                    guid,
                    signature,
                    index,
                )

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

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    query_pairs.sort()
    query = urlencode(query_pairs, doseq=True)

    return urlunparse((scheme, netloc, parsed.path, parsed.params, query, ""))


def _signature_key(item: AggregatedContent) -> tuple[str, str]:
    """Fallback deduplication key derived from the article title and timestamp."""

    title = item.title.strip().lower()
    timestamp = item.published_at.replace(microsecond=0, tzinfo=item.published_at.tzinfo)
    return (title, timestamp.isoformat())


def _guid_key(item: AggregatedContent) -> str:
    """Return a normalised identifier derived from feed-specific GUID fields."""

    raw_value = item.raw.get("id") or item.raw.get("guid") or ""
    if not isinstance(raw_value, str):
        raw_value = str(raw_value)
    return raw_value.strip().lower()


def _should_replace(existing: AggregatedContent, candidate: AggregatedContent) -> bool:
    """Determine whether the candidate item should replace the existing one."""

    if not existing.url and candidate.url:
        return True

    if not candidate.url:
        return False

    existing_normalized = _normalise_url(existing.url)
    candidate_normalized = _normalise_url(candidate.url)

    if not existing_normalized:
        return True

    if existing_normalized != candidate_normalized:
        return False

    if _has_tracking(existing.url) and not _has_tracking(candidate.url):
        return True

    return len(candidate.url) < len(existing.url)


def _has_tracking(url: str) -> bool:
    """Return ``True`` when the URL contains common analytics query parameters."""

    if not url:
        return False

    parsed = urlparse(url)
    return any(key.lower().startswith("utm_") for key, _ in parse_qsl(parsed.query, keep_blank_values=True))


def _remove_indexes(
    index_by_url: dict[str, int],
    index_by_guid: dict[str, int],
    index_by_signature: dict[tuple[str, str], int],
    item: AggregatedContent,
) -> None:
    """Remove stale index entries for a replaced aggregated item."""

    normalized_url = _normalise_url(item.url)
    if normalized_url:
        index_by_url.pop(normalized_url, None)

    guid = _guid_key(item)
    if guid:
        index_by_guid.pop(guid, None)

    index_by_signature.pop(_signature_key(item), None)


def _record_indexes(
    index_by_url: dict[str, int],
    index_by_guid: dict[str, int],
    index_by_signature: dict[tuple[str, str], int],
    normalized_url: str,
    guid: str,
    signature: tuple[str, str],
    index: int,
) -> None:
    """Store index mappings for quick duplicate lookups."""

    if normalized_url:
        index_by_url[normalized_url] = index

    if guid:
        index_by_guid[guid] = index

    index_by_signature[signature] = index
