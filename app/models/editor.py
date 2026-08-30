"""Data structures supporting the editorial agent that polishes article drafts."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from app.models.content import Article
from app.models.summarizer import ArticleDraft
from app.services.evidence.citations import allowlisted_evidence, rejected_evidence


@dataclass(slots=True)
class EditedArticle:
    """Finalised article produced by the editor agent."""

    title: str
    summary: str
    body: str
    sources: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    takeaways: list[str] = field(default_factory=list)
    disclaimer: str | None = None

    def to_article(
        self,
        *,
        include_takeaways: bool = True,
        include_disclaimer: bool = True,
    ) -> Article:
        """Convert the edited payload into the Firestore-aware :class:`Article`."""

        body_text = self.body.strip()
        sections: list[str] = [body_text]

        if include_takeaways and self.takeaways:
            bullet_lines: list[str] = []
            for raw in self.takeaways:
                cleaned = _clean_takeaway(raw)
                if cleaned:
                    bullet_lines.append(f"- {cleaned}")

            body_has_takeaways_heading = "key takeaways" in body_text.lower()
            if bullet_lines and not body_has_takeaways_heading:
                sections.append("**Key Takeaways**\n" + "\n".join(bullet_lines))

        if include_disclaimer and self.disclaimer:
            disclaimer_text = self.disclaimer.strip()
            if disclaimer_text:
                sections.append(f"> {disclaimer_text}")

        content_body = "\n\n".join(section for section in sections if section)

        return Article(
            title=self.title.strip(),
            summary=self.summary.strip(),
            content_body=content_body,
            source_urls=list(self.sources),
            tags=list(self.tags),
        )

    @classmethod
    def from_draft(cls, draft: ArticleDraft) -> "EditedArticle":
        """Create an :class:`EditedArticle` seeded from a summariser draft."""

        return cls(
            title=draft.title,
            summary=draft.summary,
            body=draft.body,
            sources=list(draft.sources),
            tags=list(draft.tags),
            takeaways=list(draft.takeaways),
        )

    def normalised(self, draft: ArticleDraft) -> "EditedArticle":
        """Return a cleaned version using draft values as fallbacks."""

        return EditedArticle(
            title=(self.title or draft.title).strip(),
            summary=(self.summary or draft.summary).strip(),
            body=(self.body or draft.body).strip(),
            sources=allowlisted_sources(draft.sources, self.sources),
            tags=_merge_unique(draft.tags, self.tags),
            takeaways=_merge_unique(draft.takeaways, self.takeaways),
            disclaimer=(self.disclaimer or "").strip() or None,
        )


def _clean_takeaway(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"^[\-\*\u2022]+", "", cleaned).strip()
    return cleaned


def _merge_unique(primary: Sequence[str], secondary: Sequence[str]) -> list[str]:
    """Return a list containing unique, trimmed values preserving order."""

    seen: set[str] = set()
    merged: list[str] = []
    for value in list(primary) + list(secondary):
        normalised = value.strip()
        if normalised and normalised not in seen:
            seen.add(normalised)
            merged.append(normalised)
    return merged


def _source_key(value: str) -> str:
    """Normalise a URL for allowlist comparison (whitespace, trailing slash)."""

    return value.strip().rstrip("/")


def allowlisted_sources(
    feed_sources: Sequence[str],
    model_sources: Sequence[str],
) -> list[str]:
    """Return only sources that actually came from the aggregated feed.

    The editor agent routinely returns plausible-looking URLs that do not
    exist (invented publisher pages, mangled copies of the real link). Source
    URLs are provenance, not prose, so the model gets no say in them: anything
    it supplies is kept only when it matches a feed URL, and the feed URLs are
    always preserved.

    This is the URL specialisation of
    :func:`app.services.evidence.citations.allowlisted_evidence`; the evidence layer
    applies the same rule to canonical source keys, which a model cannot spell at all.
    """

    return allowlisted_evidence(feed_sources, model_sources, key=_source_key)


def rejected_sources(
    feed_sources: Sequence[str],
    model_sources: Sequence[str],
) -> list[str]:
    """Return the model-supplied sources dropped by :func:`allowlisted_sources`."""

    return rejected_evidence(feed_sources, model_sources, key=_source_key)
