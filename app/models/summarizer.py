"""Models supporting the summarizer agent that drafts longevity articles."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.models.aggregator import AggregatedContent


@dataclass(slots=True)
class ArticleDraft:
    """Structured representation of a summarised longevity article."""

    title: str
    summary: str
    body: str
    takeaways: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def with_defaults(self) -> "ArticleDraft":
        """Return a copy that ensures optional fields are populated."""

        return ArticleDraft(
            title=self.title.strip() or "Longevity Insights",
            summary=self.summary.strip() or self.body[:160].strip(),
            body=self.body.strip(),
            takeaways=[point.strip() for point in self.takeaways if point.strip()],
            sources=[source.strip() for source in self.sources if source.strip()],
            tags=[tag.strip() for tag in self.tags if tag.strip()],
        )


@dataclass(slots=True)
class SummarizerContext:
    """Input provided to the summarizer constructed from aggregated content."""

    bullet_points: list[str]
    source_urls: list[str]

    @classmethod
    def from_aggregated(cls, items: Sequence[AggregatedContent]) -> "SummarizerContext":
        bullet_points: list[str] = []
        source_urls: list[str] = []

        for item in items:
            bullet_points.append(
                " - ".join(
                    part
                    for part in (
                        item.title,
                        item.summary,
                        f"Published {item.published_at:%Y-%m-%d}" if item.published_at else None,
                        f"Source: {item.source}" if item.source else None,
                    )
                    if part
                )
            )
            if item.url:
                source_urls.append(item.url)

        return cls(bullet_points=bullet_points, source_urls=source_urls)

    def to_tip_notes(
        self,
        *,
        max_notes: int = 4,
        max_characters: int = 220,
    ) -> list[str]:
        """Return concise notes tailored for prompting the tip generator."""

        notes: list[str] = []
        limit = max(1, max_notes)
        truncate_at = max(32, max_characters)

        for bullet in self.bullet_points:
            cleaned = " ".join(bullet.strip().split())
            if not cleaned:
                continue

            if len(cleaned) > truncate_at:
                cleaned = cleaned[: truncate_at - 1].rstrip()
                cleaned = f"{cleaned}…"

            notes.append(cleaned)
            if len(notes) >= limit:
                break

        return notes
