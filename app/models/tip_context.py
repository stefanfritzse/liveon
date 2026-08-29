"""Context helpers for standalone tip generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Sequence


def _default_date() -> date:
    return datetime.now(timezone.utc).date()


@dataclass(slots=True)
class TipGenerationContext:
    """Structured inputs passed to the tip generator prompt."""

    notes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    theme: str | None = None
    guidance: str | None = None
    current_date: date = field(default_factory=_default_date)

    def notes_block(self) -> str:
        """Return newline-delimited notes suitable for prompt rendering."""

        return self._join_lines(self.notes, fallback="No curated notes available.")

    def sources_block(self) -> str:
        """Return newline-delimited sources suitable for the prompt."""

        return self._join_lines(self.sources, fallback="Not provided")

    @staticmethod
    def _join_lines(lines: Sequence[str], *, fallback: str) -> str:
        cleaned = [line.strip() for line in lines if isinstance(line, str) and line.strip()]
        if not cleaned:
            return fallback
        return "\n".join(cleaned)
