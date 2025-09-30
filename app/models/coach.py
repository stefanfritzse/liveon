"""Domain models used by the conversational coaching experience."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


@dataclass(slots=True)
class CoachSource:
    """A snippet of supporting material returned to the coach agent."""

    title: str
    url: str
    snippet: str
    article_id: str | None = None
    score: float | None = None
    published_at: datetime | None = None


@dataclass(slots=True)
class CoachQuestion:
    """The user's question presented to the coach agent."""

    text: str
    metadata: Mapping[str, str] | None = None
    include_history: bool = False

    def stripped(self) -> str:
        """Return a trimmed representation of the question text."""

        return self.text.strip()


@dataclass(slots=True)
class CoachAnswer:
    """The coach agent's response along with any supporting sources."""

    message: str
    disclaimer: str
    sources: list[CoachSource] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Serialise the answer for JSON responses or templating."""

        return {
            "message": self.message,
            "disclaimer": self.disclaimer,
            "sources": [
                {
                    "title": source.title,
                    "url": source.url,
                    "snippet": source.snippet,
                    "article_id": source.article_id,
                    "score": source.score,
                    "published_at": source.published_at.isoformat()
                    if source.published_at
                    else None,
                }
                for source in self.sources
            ],
        }
