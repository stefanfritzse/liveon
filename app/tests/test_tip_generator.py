from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from langchain_core.messages import AIMessage

from app.models.aggregator import AggregatedContent, FeedSource
from app.services.tip_generator import TipGenerator


class DummyLLM:
    """Simple fake LLM that returns a fixed AIMessage payload."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[Sequence[Any]] = []

    def invoke(self, input: Any, **_: Any) -> AIMessage:
        self.calls.append(input if isinstance(input, list) else [input])
        return AIMessage(content=self._response)


def sample_aggregated(summary_suffix: str = "") -> list[AggregatedContent]:
    feed = FeedSource(name="Longevity Digest", url="https://example.com/rss", topic="research")
    return [
        AggregatedContent(
            title="Intermittent fasting study shows promising biomarkers",
            url="https://example.com/articles/intermittent-fasting",
            summary=(
                "Longitudinal study tracks biomarker improvements in fasting cohorts." + summary_suffix
            ),
            published_at=datetime(2024, 1, 2, 9, tzinfo=timezone.utc),
            source="Example News",
            topic=feed.topic,
            raw={},
        ),
    ]


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

    draft = agent.generate(sample_aggregated())

    assert draft.title == "Intermittent Fasting for Metabolic Health"
    assert draft.body.startswith("Stay hydrated")
    assert draft.tags == ["nutrition", "fasting"]
    assert "https://journal.example.com/study" in draft.metadata.get("sources", [])
    assert "https://example.com/articles/intermittent-fasting" in draft.metadata.get("sources", [])
    assert draft.metadata.get("confidence") == "high"


def test_generate_handles_malformed_json() -> None:
    agent = TipGenerator(llm=DummyLLM("not-json"))

    try:
        agent.generate(sample_aggregated())
    except ValueError as exc:
        assert "valid JSON" in str(exc)
    else:  # pragma: no cover - ensure failure visible
        raise AssertionError("Expected ValueError for invalid JSON response")


def test_generate_truncates_notes_for_prompt() -> None:
    long_suffix = " " + "impactful insights " * 30
    agent = TipGenerator(llm=DummyLLM("{}"))

    agent.generate(sample_aggregated(summary_suffix=long_suffix))

    # Last call contains the formatted messages (system + human)
    _, human_message = agent.llm.calls[-1]
    assert "…" in human_message.content

    notes_section = human_message.content.split("Notes:\n", 1)[1].split("\n\nKey sources:", 1)[0]
    for line in notes_section.splitlines():
        assert len(line) <= 221  # truncated with ellipsis
