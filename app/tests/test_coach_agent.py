"""Unit tests for the CoachAgent orchestration logic."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.models.coach import CoachSource
from app.services import coach as coach_module
from app.services.coach import CoachAgent, LocalCoachResponder


class _DummyPromptValue:
    """Lightweight stand-in for LangChain prompt values used in tests."""

    def __init__(self, messages: list[dict[str, str]]) -> None:
        self._messages = messages

    def to_messages(self) -> list[dict[str, str]]:
        return self._messages

    def to_string(self) -> str:  # pragma: no cover - debug helper
        return "\n\n".join(f"{item['role']}: {item['content']}" for item in self._messages)


class _DummyChatPromptTemplate:
    """Mimics the subset of LangChain's prompt template API required by CoachAgent."""

    def __init__(self, message_specs: Sequence[tuple[str, ...]]) -> None:
        self._message_specs = message_specs

    @classmethod
    def from_messages(cls, message_specs: Sequence[tuple[str, ...]]) -> "_DummyChatPromptTemplate":
        return cls(message_specs)

    def invoke(self, mapping: dict[str, str]) -> _DummyPromptValue:
        formatted: list[dict[str, str]] = []
        for spec in self._message_specs:
            role, *parts = spec
            template = "".join(parts)
            formatted.append({"role": role, "content": template.format(**mapping)})
        return _DummyPromptValue(formatted)


class _RecordingResponder(LocalCoachResponder):
    """Local responder variant that records the prompt it received."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: Sequence[dict[str, str]] | None = None

    def invoke(self, messages):  # type: ignore[override]
        self.messages = messages
        return super().invoke(messages)


class _StubRepository:
    def __init__(self, sources: Sequence[CoachSource]) -> None:
        self._sources = list(sources)
        self.calls: list[tuple[str, int]] = []

    def search_articles_for_question(self, question: str, *, limit: int) -> list[CoachSource]:
        self.calls.append((question, limit))
        return list(self._sources)


@pytest.fixture(autouse=True)
def _stub_prompt_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure CoachAgent can be constructed without the langchain-core dependency."""

    monkeypatch.setattr(coach_module, "ChatPromptTemplate", _DummyChatPromptTemplate)


def _build_source(title: str, snippet: str, *, url: str = "https://example.com") -> CoachSource:
    return CoachSource(title=title, url=url, snippet=snippet)


def test_ask_returns_answer_with_formatted_citations_and_disclaimer() -> None:
    sources = [
        _build_source("Longevity basics", "Focus on sleep quality."),
        _build_source("Nutrition study", "Include leafy greens daily."),
    ]
    repository = _StubRepository(sources)
    responder = _RecordingResponder()
    agent = CoachAgent(llm=responder, repository=repository, context_limit=5)

    answer = agent.ask("   How can I improve my longevity?   ")

    assert answer.message.startswith("Offline coach response"), "Expected deterministic local response"
    assert answer.disclaimer == responder.disclaimer
    assert answer.sources == sources
    assert repository.calls == [("How can I improve my longevity?", 5)]

    assert responder.messages is not None
    human_message = next(item for item in responder.messages if item["role"] == "human")
    content = human_message["content"]
    assert "[1] Longevity basics" in content
    assert "Snippet: Focus on sleep quality." in content
    assert "[2] Nutrition study" in content
    assert not answer.message.strip().endswith(responder.disclaimer)


def test_ask_handles_empty_search_results() -> None:
    repository = _StubRepository([])
    responder = _RecordingResponder()
    agent = CoachAgent(llm=responder, repository=repository, context_limit=2)

    answer = agent.ask("What should I do when no studies are available?")

    assert answer.sources == []
    assert answer.disclaimer == responder.disclaimer
    assert answer.message.startswith("Offline coach response")

    assert responder.messages is not None
    human_message = next(item for item in responder.messages if item["role"] == "human")
    assert "No articles matched the request" in human_message["content"]
