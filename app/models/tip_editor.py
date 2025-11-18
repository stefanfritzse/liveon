"""Data structures supporting the tip editor agent."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.tip import TipDraft


@dataclass(slots=True)
class TipReviewResult:
    """Structured result returned by the tip editor after reviewing a draft."""

    is_approved: bool
    feedback: str | None = None
    revised_draft: TipDraft | None = None

    def resolved_draft(self, fallback: TipDraft) -> TipDraft:
        """Return the revised draft when present, otherwise fall back to the input."""

        return self.revised_draft or fallback
