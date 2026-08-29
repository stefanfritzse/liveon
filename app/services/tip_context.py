"""Helpers for constructing daily tip generation context without RSS feeds."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from app.models.tip_context import TipGenerationContext

LOGGER = logging.getLogger(__name__)


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


class DailyTipContextProvider:
    """Select a deterministic preset to keep the tip pipeline self-contained."""

    def __init__(
        self,
        *,
        presets: Sequence[dict[str, Any]] | None = None,
        now_provider: Callable[[], datetime] | None = None,
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
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def build(self) -> TipGenerationContext:
        today = self._now_provider()
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
