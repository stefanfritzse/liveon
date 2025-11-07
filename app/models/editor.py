"""Data structures supporting the editorial agent that polishes article drafts."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.models.content import Article
from app.models.summarizer import ArticleDraft


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
        """Convert the edited payload into the app :class:`Article`."""

        sections: list[str] = [self.body.strip()]

        if include_takeaways and self.takeaways:
            bullets = "\n".join(f"- {item.strip()}" for item in self.takeaways if item.strip())
            if bullets:
                sections.append("**Key Takeaways**\n" + bullets)

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
            sources=_merge_unique(draft.sources, self.sources),
            tags=_merge_unique(draft.tags, self.tags),
            takeaways=_merge_unique(draft.takeaways, self.takeaways),
            disclaimer=(self.disclaimer or "").strip() or None,
        )


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
