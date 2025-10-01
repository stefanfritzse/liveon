from __future__ import annotations

from collections.abc import Sequence
import sys
from types import ModuleType
from typing import Any

import pytest

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


class _EchoResponder:
    def invoke(self, messages):  # type: ignore[override]
        return "Here is support.\n\nDisclaimer: Custom safety notice"


@pytest.fixture(autouse=True)
def _stub_prompt_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure CoachAgent can be constructed without the langchain-core dependency."""

    monkeypatch.setattr(coach_module, "ChatPromptTemplate", _DummyChatPromptTemplate)


def test_ask_returns_answer_with_default_disclaimer() -> None:
    responder = _RecordingResponder()
    agent = CoachAgent(llm=responder)

    answer = agent.ask("   How can I improve my longevity?   ")

    assert answer.message.startswith("Offline coach response"), "Expected deterministic local response"
    assert answer.disclaimer == responder.disclaimer

    assert responder.messages is not None
    human_message = next(item for item in responder.messages if item["role"] == "human")
    assert "How can I improve my longevity?" in human_message["content"]
    assert "Disclaimer" not in answer.message


def test_ask_uses_llm_disclaimer_when_provided() -> None:
    responder = _EchoResponder()
    agent = CoachAgent(llm=responder)

    answer = agent.ask("What recovery strategies help?")

    assert answer.message == "Here is support."
    assert answer.disclaimer == "Custom safety notice"


def test_create_coach_llm_configures_gemini_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, Any] = {}

    class _StubGeminiClient:
        def __init__(self, **kwargs: Any) -> None:
            captured_kwargs.update(kwargs)
            self.kwargs = kwargs

        def invoke(self, messages: Any) -> str:  # pragma: no cover - helper behaviour
            return "Gemini reply"

    fake_module = ModuleType("langchain_google_vertexai")
    fake_module.ChatVertexAI = _StubGeminiClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_google_vertexai", fake_module)
    monkeypatch.setattr(coach_module.google.auth, "default", lambda: (object(), "proj"), raising=False)
    monkeypatch.setenv("LIVEON_COACH_VERTEX_MODEL", "gemini-1.5-pro")
    monkeypatch.setenv("LIVEON_VERTEX_LOCATION", "europe-west1")
    monkeypatch.setenv("GCP_PROJECT", "unit-test-project")
    monkeypatch.setenv("LIVEON_MODEL_TEMPERATURE", "0.5")
    monkeypatch.setenv("LIVEON_MODEL_MAX_OUTPUT_TOKENS", "256")

    llm = coach_module.create_coach_llm()

    assert isinstance(llm, _StubGeminiClient)
    assert captured_kwargs["model_name"] == "gemini-1.5-pro"
    assert captured_kwargs["temperature"] == 0.5
    assert captured_kwargs["max_output_tokens"] == 256
    assert captured_kwargs["location"] == "europe-west1"
    assert captured_kwargs["project"] == "unit-test-project"
