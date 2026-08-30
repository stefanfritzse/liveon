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
    # Provenance travels with the draft rather than in ``metadata``, because metadata was
    # dropped at persistence and a tip that cannot name its source is unpublishable.
    source_urls: list[str] = field(default_factory=list)
    evidence_bundle_id: str | None = None
    evidence_keys: list[str] = field(default_factory=list)
    evidence_grade: str | None = None
    evidence_summary: str | None = None
    evidence_limitations: list[str] = field(default_factory=list)

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
            source_urls=[
                url.strip()
                for url in self.source_urls
                if isinstance(url, str) and url.strip()
            ],
            evidence_bundle_id=(self.evidence_bundle_id or "").strip() or None,
            evidence_keys=[
                key.strip()
                for key in self.evidence_keys
                if isinstance(key, str) and key.strip()
            ],
            evidence_grade=(self.evidence_grade or "").strip() or None,
            evidence_summary=(self.evidence_summary or "").strip() or None,
            evidence_limitations=[
                limitation.strip()
                for limitation in self.evidence_limitations
                if isinstance(limitation, str) and limitation.strip()
            ],
        )
