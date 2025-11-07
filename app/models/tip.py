"""Models supporting tip generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TipDraft:
    """Structured representation of a longevity tip draft."""

    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def with_defaults(self) -> "TipDraft":
        """Return a copy with trimmed fields and fallback defaults."""

        cleaned_tags = [
            tag.strip()
            for tag in self.tags
            if isinstance(tag, str) and tag.strip()
        ]
        cleaned_metadata = {
            str(key): value for key, value in self.metadata.items() if isinstance(key, str)
        }

        return TipDraft(
            title=self.title.strip() or "Longevity Tip",
            body=self.body.strip(),
            tags=cleaned_tags,
            metadata=cleaned_metadata,
        )
