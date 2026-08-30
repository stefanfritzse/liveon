"""Offline tip presets, retained as test fixtures only.

These used to be a runtime fallback: when every feed failed, the tip pipeline drew on
them and published anyway. Several carry specific quantitative health claims behind
links that do not support them ("20-30g of protein ... curb cravings for the next 6
hours"), which is the wrong failure mode for an autonomous health publication — a feed
outage would produce a confident, mis-sourced claim presented as current research.

The runtime now fails closed instead (improvements.md item 9). They survive here so
tests can exercise context-shaped inputs without reaching the network.
"""

from __future__ import annotations

from typing import Any

TIP_PRESETS: list[dict[str, Any]] = [
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

