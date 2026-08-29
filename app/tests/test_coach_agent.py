from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from app.services import coach as coach_module
from app.services.coach import CoachAgent, LocalCoachResponder


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


def test_create_coach_llm_configures_ollama_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The coach delegates model construction to the shared factory."""

    captured_kwargs: dict[str, Any] = {}
    sentinel = object()

    def _fake_build(**kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return sentinel

    monkeypatch.setattr(coach_module, "build_chat_ollama", _fake_build)

    llm = coach_module.create_coach_llm()

    assert llm is sentinel
    assert captured_kwargs["model"] == "phi3:14b-medium-4k-instruct-q4_K_M"
    # Conversational answers are prose, so JSON mode stays off for the coach.
    assert captured_kwargs["json_mode"] is False
