from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from langchain_core.messages import AIMessage

from app.models.aggregator import AggregatedContent, FeedSource
from app.services.summarizer import SummarizerAgent


class DummyLLM:
    """Simple fake LLM that returns a fixed AIMessage payload."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[Sequence[Any]] = []

    def invoke(self, input: Any, **_: Any) -> AIMessage:
        self.calls.append(input if isinstance(input, list) else [input])
        return AIMessage(content=self._response)


def sample_aggregated() -> list[AggregatedContent]:
    feed = FeedSource(name="Longevity Digest", url="https://example.com/rss", topic="research")
    return [
        AggregatedContent(
            title="Intermittent fasting study shows promising biomarkers",
            url="https://example.com/articles/intermittent-fasting",
            summary="Longitudinal study tracks biomarker improvements in fasting cohorts.",
            published_at=datetime(2024, 1, 2, 9, tzinfo=timezone.utc),
            source="Example News",
            topic=feed.topic,
            raw={},
        ),
        AggregatedContent(
            title="Strength training linked to increased healthspan",
            url="https://example.com/articles/strength-training",
            summary="Meta-analysis finds resistance training supports metabolic resilience.",
            published_at=datetime(2024, 1, 3, 8, tzinfo=timezone.utc),
            source="Example News",
            topic=feed.topic,
            raw={},
        ),
    ]


def test_summarize_returns_structured_draft() -> None:
    fake_response = """
    {
      "title": "Two Lifestyle Interventions Support Healthy Aging",
      "summary": "A fasting protocol and strength training routine demonstrate biomarker and metabolic gains.",
      "body": "## Longevity Highlights - Intermittent fasting and strength training support healthy aging.",
      "takeaways": [
        "Intermittent fasting improved cellular biomarkers",
        "Consistent strength training bolsters metabolic resilience"
      ],
      "sources": ["https://journal.example.com/study"],
      "tags": ["nutrition", "exercise"]
    }
    """.strip()
    agent = SummarizerAgent(llm=DummyLLM(fake_response))

    draft = agent.summarize(sample_aggregated())

    assert draft.title == "Two Lifestyle Interventions Support Healthy Aging"
    assert draft.summary.startswith("A fasting protocol")
    assert draft.body.startswith("## Longevity Highlights")
    assert draft.takeaways == [
        "Intermittent fasting improved cellular biomarkers",
        "Consistent strength training bolsters metabolic resilience",
    ]
    assert "https://example.com/articles/strength-training" in draft.sources
    assert "https://journal.example.com/study" in draft.sources
    assert draft.tags == ["nutrition", "exercise"]


def test_summarize_raises_when_llm_returns_invalid_json() -> None:
    agent = SummarizerAgent(llm=DummyLLM("not-json"))

    try:
        agent.summarize(sample_aggregated())
    except ValueError as exc:
        assert "valid JSON" in str(exc)
    else:  # pragma: no cover - ensure failure visible
        raise AssertionError("Expected ValueError for invalid JSON response")


def test_summarize_requires_content_items() -> None:
    agent = SummarizerAgent(llm=DummyLLM("{}"))

    try:
        agent.summarize([])
    except ValueError as exc:
        assert "At least one" in str(exc)
    else:  # pragma: no cover - ensure failure visible
        raise AssertionError("Expected ValueError when no items provided")
