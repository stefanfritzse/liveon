"""Build the research context that guides daily tip generation.

This provider has been through two failure modes worth remembering.

First it drew on three literal presets selected by ``ordinal % 3``, so the generator saw
identical notes every third day while the editor rejected anything repetitive — the review
loop was fighting its own input. Aggregated news replaced that.

Then the presets remained as an *offline fallback*, which is the failure mode this version
removes. When every feed failed, the run fell back to hard-coded notes and continued
toward publication, so a network outage produced a confident, specific health claim
presented as that day's research. For an autonomous publication with no human between the
pipeline and the reader, that is the wrong direction to fail in.

Now the provider raises. Inability to see today's research produces no tip
(improvements.md item 9, invariant I5), and the caller distinguishes "nothing new" from
"could not look" so the scheduler can back off from one and not the other. The presets
survive in :mod:`app.tests.fixtures.tip_presets` as test data.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, Sequence

from app.models.aggregator import AggregatedContent
from app.models.run_outcome import RunOutcome
from app.models.tip_context import TipGenerationContext

LOGGER = logging.getLogger(__name__)

#: How many aggregated stories to distil into the notes block.
DEFAULT_NOTE_COUNT = 4

#: Trim each note so a handful of stories cannot crowd out the instructions.
_MAX_NOTE_CHARS = 320

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class TipContextUnavailable(RuntimeError):
    """Raised when no research context can be built for this run.

    Carries the :class:`RunOutcome` that describes *why*, because the scheduler treats a
    quiet day and an unreachable source very differently: one satisfies the cadence, the
    other backs off and tries again.
    """

    def __init__(self, message: str, *, outcome: RunOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome


class SupportsAggregation(Protocol):
    """The slice of :class:`LongevityNewsAggregator` this provider needs."""

    def gather(self, *, limit_per_feed: int = 5) -> Any:
        """Return aggregated longevity updates."""


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
    """Assemble the research context for one tip run, or refuse to."""

    def __init__(
        self,
        *,
        aggregator: SupportsAggregation | None = None,
        now_provider: Callable[[], datetime] | None = None,
        note_count: int = DEFAULT_NOTE_COUNT,
        feed_limit: int = 5,
    ) -> None:
        self._aggregator = aggregator
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._note_count = max(1, note_count)
        self._feed_limit = max(1, feed_limit)

    def build(self) -> TipGenerationContext:
        """Return today's context.

        :raises TipContextUnavailable: when there is no research to build one from.
        """

        today = self._now_provider()

        if self._aggregator is None:
            raise TipContextUnavailable(
                "No research source is configured for tip generation.",
                outcome=RunOutcome.SOURCE_UNAVAILABLE,
            )

        try:
            result = self._aggregator.gather(limit_per_feed=self._feed_limit)
        except Exception as exc:  # noqa: BLE001 - reported as a retryable outcome
            LOGGER.warning(
                "Tip aggregation failed; publishing nothing this run: %s",
                exc,
                extra={"event": "tip_context.aggregation_failed"},
            )
            raise TipContextUnavailable(
                f"Could not retrieve research for tip generation: {exc}",
                outcome=RunOutcome.RETRIEVAL_FAILED,
            ) from exc

        items = list(getattr(result, "items", []) or [])
        errors = list(getattr(result, "errors", []) or [])
        for error in errors:
            LOGGER.info("Tip aggregation warning: %s", error)

        if not items:
            # Every feed erroring is an outage; feeds answering with nothing is a quiet
            # day. The scheduler backs off from the first and not the second.
            outcome = RunOutcome.RETRIEVAL_FAILED if errors else RunOutcome.NO_NEW_EVIDENCE
            LOGGER.warning(
                "Tip aggregation returned no usable items (%s)",
                outcome.value,
                extra={"event": "tip_context.empty", "outcome": outcome.value},
            )
            raise TipContextUnavailable(
                "No research was available for tip generation.", outcome=outcome
            )

        notes, sources = summarise_for_notes(items, limit=self._note_count)
        if not notes:
            raise TipContextUnavailable(
                "Aggregated items carried no usable notes.",
                outcome=RunOutcome.NO_NEW_EVIDENCE,
            )

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
