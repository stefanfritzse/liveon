"""Data structures supporting the publisher agent that commits content updates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(slots=True)
class PublicationResult:
    """Outcome returned by the publisher agent after committing an article."""

    slug: str
    path: Path
    commit_hash: str | None
    published_at: datetime

