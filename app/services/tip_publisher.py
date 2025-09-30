"""Publisher service that persists generated tips via the Firestore repository."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Protocol

from app.models.content import Tip
from app.models.tip import TipDraft


class SupportsTipRepository(Protocol):
    """Protocol describing the repository helpers used by :class:`TipPublisher`."""

    def save_tip(self, tip: Tip) -> Tip:
        """Persist a tip and return the stored representation."""

    def find_tip_by_title(self, title: str) -> Tip | None:
        """Retrieve a tip matching the provided title if one exists."""

    def find_tip_by_tags(self, tags: Iterable[str]) -> Tip | None:
        """Retrieve a tip whose tags match the provided iterable."""


@dataclass(slots=True)
class TipPublicationResult:
    """Outcome returned by :class:`TipPublisher` after attempting to store a tip."""

    tip: Tip
    created: bool


@dataclass(slots=True)
class TipPublisher:
    """Convert :class:`TipDraft` instances into stored :class:`Tip` models."""

    repository: SupportsTipRepository

    def publish(
        self,
        draft: TipDraft,
        *,
        published_at: datetime | None = None,
    ) -> TipPublicationResult:
        """Persist the supplied tip draft, returning the stored tip and creation status."""

        normalised = draft.with_defaults()
        if not normalised.body:
            raise ValueError("Tip draft body cannot be empty")

        existing = self._find_existing(normalised)
        if existing is not None:
            return TipPublicationResult(tip=existing, created=False)

        published = (published_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        tip = Tip(
            title=normalised.title,
            content_body=normalised.body,
            published_date=published,
            tags=list(normalised.tags),
        )
        stored = self.repository.save_tip(tip)
        return TipPublicationResult(tip=stored, created=True)

    def _find_existing(self, draft: TipDraft) -> Tip | None:
        """Return a matching stored tip if one already exists."""

        title = draft.title.strip()
        if title:
            by_title = self.repository.find_tip_by_title(title)
            if by_title is not None and self._matches(by_title, draft):
                return by_title

        if draft.tags:
            by_tags = self.repository.find_tip_by_tags(draft.tags)
            if by_tags is not None and self._matches(by_tags, draft):
                return by_tags

        return None

    @staticmethod
    def _matches(existing: Tip, draft: TipDraft) -> bool:
        """Return ``True`` when ``existing`` is equivalent to ``draft``."""

        existing_tags = {tag.strip() for tag in existing.tags if isinstance(tag, str) and tag.strip()}
        draft_tags = {tag.strip() for tag in draft.tags if isinstance(tag, str) and tag.strip()}

        return (
            existing.title.strip() == draft.title.strip()
            and existing.content_body.strip() == draft.body.strip()
            and existing_tags == draft_tags
        )
