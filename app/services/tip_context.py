"""Build the research context that guides daily tip generation.

Tips used to come from three literal presets selected by ``ordinal % 3``, so the
generator saw identical notes every third day while the editor's rubric rejected
anything repetitive — the review loop was fighting its own input. The aggregated
news pool is now the primary source, and the presets remain as an offline fallback
for development and for runs where every feed fails.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, Sequence

from app.models.aggregator import AggregatedContent
from app.models.tip_context import TipGenerationContext

LOGGER = logging.getLogger(__name__)

#: How many aggregated stories to distil into the notes block.
DEFAULT_NOTE_COUNT = 4

#: Trim each note so a handful of stories cannot crowd out the instructions.
_MAX_NOTE_CHARS = 320

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


_DEFAULT_PRESETS: list[dict[str, Any]] = [
    {
        "theme": "Strength snacks",
        "guidance": "Encourage short bursts of bodyweight strength throughout the day.",
        "notes": [
            "Mini sets of air squats, lunges, or push-ups keep muscle fibres engaged and preserve strength.",
            "Stack three to four 5-minute bouts between meetings to accumulate the recommended 20 minutes of resistance work.",
        ],
        "sources": ["https://www.cdc.gov/physicalactivity/basics/older_adults/index.htm"],
    },
    {
        "theme": "Circadian-friendly light",
        "guidance": "Coach readers to synchronise daylight exposure with their morning routine.",
        "notes": [
            "A 10-minute outdoor walk within an hour of waking reinforces circadian rhythms.",
            "Pair it with gentle neck/upper-back mobility to counteract screen posture.",
        ],
        "sources": ["https://www.sleepfoundation.org/circadian-rhythm"],
    },
    {
        "theme": "Nutrient timing",
        "guidance": "Offer a concrete, food-based action that stabilises energy.",
        "notes": [
            "Add 20-30g of protein to the first meal to curb cravings for the next 6 hours.",
            "Combine protein with fibrous vegetables to slow glucose spikes.",
        ],
        "sources": ["https://www.health.harvard.edu/blog/8-powerful-food-combos-2019051416639"],
    },
]


class SupportsAggregation(Protocol):
    """The slice of :class:`LongevityNewsAggregator` this provider needs."""

    def gather(self, *, limit_per_feed: int = 5) -> Any:
        """Return aggregated longevity updates."""


def _load_presets_from_env() -> list[dict[str, Any]] | None:
    raw = os.getenv("LIVEON_TIP_CONTEXT_PRESETS")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.warning("Failed to parse LIVEON_TIP_CONTEXT_PRESETS, using defaults.")
        return None
    if isinstance(payload, list):
        return [preset for preset in payload if isinstance(preset, dict)]
    LOGGER.warning("LIVEON_TIP_CONTEXT_PRESETS must be a list of objects; using defaults.")
    return None


def _clean_text(value: str) -> str:
    """Strip feed markup down to a plain sentence fragment."""

    text = html.unescape(value or "")
    text = _TAG_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def summarise_for_notes(items: Sequence[AggregatedContent], *, limit: int) -> tuple[list[str], list[str]]:
    """Turn aggregated stories into prompt notes and their source URLs."""

    notes: list[str] = []
    sources: list[str] = []

    for item in items[:limit]:
        title = _clean_text(item.title)
        if not title:
            continue
        summary = _clean_text(item.summary)
        note = f"{title} - {summary}" if summary else title
        if len(note) > _MAX_NOTE_CHARS:
            note = note[:_MAX_NOTE_CHARS].rstrip() + "…"
        notes.append(note)
        url = (item.url or "").strip()
        if url and url not in sources:
            sources.append(url)

    return notes, sources


class DailyTipContextProvider:
    """Assemble the research context for one tip run.

    Aggregated news is preferred; the deterministic presets are used when no
    aggregator is configured or when a run returns nothing usable.
    """

    def __init__(
        self,
        *,
        aggregator: SupportsAggregation | None = None,
        presets: Sequence[dict[str, Any]] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        note_count: int = DEFAULT_NOTE_COUNT,
        feed_limit: int = 5,
    ) -> None:
        env_presets = _load_presets_from_env()
        if env_presets:
            self._presets = list(env_presets)
        elif presets:
            self._presets = [dict(preset) for preset in presets]
        else:
            self._presets = list(_DEFAULT_PRESETS)
        if not self._presets:
            raise ValueError("At least one preset is required for tip context generation.")
        self._aggregator = aggregator
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._note_count = max(1, note_count)
        self._feed_limit = max(1, feed_limit)

    def build(self) -> TipGenerationContext:
        today = self._now_provider()

        context = self._build_from_feeds(today)
        if context is not None:
            return context

        return self._build_from_presets(today)

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------
    def _build_from_feeds(self, today: datetime) -> TipGenerationContext | None:
        if self._aggregator is None:
            return None

        try:
            result = self._aggregator.gather(limit_per_feed=self._feed_limit)
        except Exception as exc:  # noqa: BLE001 - a feed outage must not stop the run
            LOGGER.warning(
                "Tip aggregation failed; falling back to presets: %s",
                exc,
                extra={"event": "tip_context.aggregation_failed"},
            )
            return None

        items = list(getattr(result, "items", []) or [])
        for error in getattr(result, "errors", []) or []:
            LOGGER.info("Tip aggregation warning: %s", error)

        if not items:
            LOGGER.warning(
                "Tip aggregation returned no items; falling back to presets",
                extra={"event": "tip_context.empty"},
            )
            return None

        notes, sources = summarise_for_notes(items, limit=self._note_count)
        if not notes:
            return None

        return TipGenerationContext(
            notes=notes,
            sources=sources,
            theme=self._theme_for(items),
            guidance=(
                "Draw a single practical habit out of today's research below. Name the"
                " specific behaviour and say why it supports healthy ageing."
            ),
            current_date=today.date(),
        )

    @staticmethod
    def _theme_for(items: Sequence[AggregatedContent]) -> str | None:
        """Use the most common feed topic as a loose theme."""

        topics = [item.topic for item in items if getattr(item, "topic", None)]
        if not topics:
            return None
        return max(set(topics), key=topics.count)

    def _build_from_presets(self, today: datetime) -> TipGenerationContext:
        preset = self._pick_preset(today)
        return TipGenerationContext(
            notes=list(preset.get("notes") or []),
            sources=list(preset.get("sources") or []),
            theme=str(preset.get("theme") or "").strip() or None,
            guidance=str(preset.get("guidance") or "").strip() or None,
            current_date=today.date(),
        )

    def _pick_preset(self, today: datetime) -> dict[str, Any]:
        index = today.date().toordinal() % len(self._presets)
        return self._presets[index]
