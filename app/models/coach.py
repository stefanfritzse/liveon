"""Domain models used by the conversational coaching experience."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


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
    """The coach agent's response payload."""

    message: str
    disclaimer: str

    def as_dict(self) -> dict[str, object]:
        """Serialise the answer for JSON responses or templating."""

        return {
            "message": self.message,
            "disclaimer": self.disclaimer,
        }
