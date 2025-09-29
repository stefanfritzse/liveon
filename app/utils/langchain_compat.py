"""Lightweight compatibility helpers for optional LangChain dependencies."""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

__all__ = [
    "AIMessage",
    "BaseMessage",
    "ChatPromptTemplate",
]


def _module_available(module: str) -> bool:
    """Return ``True`` when the given module can be imported."""

    return importlib.util.find_spec(module) is not None


if _module_available("langchain_core.messages"):
    from langchain_core.messages import AIMessage, BaseMessage  # type: ignore
else:
    @dataclass(slots=True)
    class BaseMessage:  # pragma: no cover - trivial container
        """Minimal stand-in for :class:`langchain_core.messages.BaseMessage`."""

        content: str | None = None

    @dataclass(slots=True)
    class AIMessage(BaseMessage):  # pragma: no cover - trivial container
        """Fallback :class:`langchain_core.messages.AIMessage` implementation."""

        pass


if _module_available("langchain_core.prompts"):
    from langchain_core.prompts import ChatPromptTemplate  # type: ignore
else:
    @dataclass(slots=True)
    class _PromptMessage(BaseMessage):  # pragma: no cover - trivial container
        role: str = "human"

    class ChatPromptTemplate:  # pragma: no cover - minimal behaviour
        """Simplified drop-in replacement for LangChain chat prompts."""

        def __init__(self, messages: Sequence[tuple[str, str]]) -> None:
            self._messages = tuple(messages)

        @classmethod
        def from_messages(
            cls, messages: Iterable[tuple[str, str]]
        ) -> "ChatPromptTemplate":
            return cls(tuple(messages))

        def format_messages(self, **kwargs: Any) -> list[_PromptMessage]:
            formatted: list[_PromptMessage] = []
            for role, template in self._messages:
                if hasattr(template, "format"):
                    content = template.format(**kwargs)
                else:
                    content = str(template)
                formatted.append(_PromptMessage(role=role, content=str(content)))
            return formatted
