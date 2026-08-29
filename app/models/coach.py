"""Domain models used by the conversational coaching experience."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

USER_ROLE = "user"
COACH_ROLE = "coach"


@dataclass(slots=True, frozen=True)
class CoachTurn:
    """A single earlier message in the conversation."""

    role: str
    text: str

    @property
    def is_user(self) -> bool:
        return self.role == USER_ROLE

    def stripped(self) -> str:
        return self.text.strip()


@dataclass(slots=True)
class CoachQuestion:
    """The user's question presented to the coach agent.

    ``history`` carries the earlier turns of the same conversation, oldest first, so
    follow-ups like "and after 50?" resolve against what was already discussed.
    """

    text: str
    metadata: Mapping[str, str] | None = None
    history: Sequence[CoachTurn] = field(default_factory=tuple)

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
