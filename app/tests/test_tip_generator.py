from __future__ import annotations

from typing import Any, Sequence

from langchain_core.messages import AIMessage

from app.models.tip_context import TipGenerationContext
from app.services.tip_generator import TipGenerator


class DummyLLM:
    """Simple fake LLM that returns a fixed AIMessage payload."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[Sequence[Any]] = []

    def invoke(self, input: Any, **_: Any) -> AIMessage:
        self.calls.append(input if isinstance(input, list) else [input])
        return AIMessage(content=self._response)


def sample_context(note_suffix: str = "") -> TipGenerationContext:
    base_note = "Longitudinal study tracks biomarker improvements in fasting cohorts."
    if note_suffix:
        base_note = f"{base_note} {note_suffix}".strip()
    return TipGenerationContext(
        notes=[base_note],
        sources=["https://example.com/articles/intermittent-fasting"],
        guidance="Coach readers to structure their fasting window and recovery meals.",
    )


def test_generate_returns_tip_draft() -> None:
    fake_response = """
    {
      "title": "Intermittent Fasting for Metabolic Health",
      "body": "Stay hydrated and track your fasting window to reinforce metabolic gains.",
      "tags": ["nutrition", "fasting"],
      "metadata": {
        "sources": ["https://journal.example.com/study"],
        "confidence": "high"
      }
    }
    """.strip()
    agent = TipGenerator(llm=DummyLLM(fake_response))

    draft = agent.generate(context=sample_context())

    assert draft.title == "Intermittent Fasting for Metabolic Health"
    assert draft.body.startswith("Stay hydrated")
    assert draft.tags == ["nutrition", "fasting"]
    assert "https://journal.example.com/study" in draft.metadata.get("sources", [])
    assert "https://example.com/articles/intermittent-fasting" in draft.metadata.get("sources", [])
    assert draft.metadata.get("confidence") == "high"


def test_generate_handles_malformed_json() -> None:
    agent = TipGenerator(llm=DummyLLM("not-json"))

    try:
        agent.generate(context=sample_context())
    except ValueError as exc:
        assert "valid JSON" in str(exc)
    else:  # pragma: no cover - ensure failure visible
        raise AssertionError("Expected ValueError for invalid JSON response")


def test_generate_includes_guidance_in_prompt() -> None:
    agent = TipGenerator(llm=DummyLLM("{}"))

    agent.generate(context=sample_context())

    _, human_message = agent.llm.calls[-1]
    assert "Today's focus: Coach readers to structure their fasting window" in human_message.content
