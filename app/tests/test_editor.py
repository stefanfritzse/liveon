import json
from typing import Any, Sequence

from langchain_core.messages import AIMessage

from app.models.summarizer import ArticleDraft
from app.scripts.run_pipeline import LocalJSONResponder
from app.services.editor import EditorAgent


class DummyLLM:
    """Simple fake LLM that returns a fixed AIMessage payload."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[Sequence[Any]] = []

    def invoke(self, input: Any, **_: Any) -> AIMessage:
        self.calls.append(input if isinstance(input, list) else [input])
        return AIMessage(content=self._response)


def sample_draft() -> ArticleDraft:
    return ArticleDraft(
        title="Two Lifestyle Interventions Support Healthy Aging",
        summary="A fasting protocol and strength training routine demonstrate biomarker and metabolic gains.",
        body=(
            "## Longevity Highlights\n"
            "Intermittent fasting and strength training support healthy aging.\n"
            "Sources currently include observational research only."
        ),
        takeaways=[
            "Intermittent fasting improved cellular biomarkers",
            "Consistent strength training bolsters metabolic resilience",
        ],
        sources=[
            "https://example.com/articles/intermittent-fasting",
            "https://example.com/articles/strength-training",
        ],
        tags=["nutrition", "exercise"],
    )


def editor_payload() -> dict[str, Any]:
    return {
        "title": "Two Evidence-Backed Habits to Support Healthy Aging",
        "summary": "Research-backed fasting and strength routines can improve biomarkers and metabolic health.",
        "body": "## Refined Highlights\nClinical and longitudinal studies suggest fasting and strength work aid healthy aging.",
        "takeaways": [
            "Prioritise protein intake alongside strength training",
            "Monitor biomarkers when attempting fasting protocols",
        ],
        "sources": [
            "https://journal.example.com/intermittent-fasting",
            "https://journal.example.com/strength-training",
        ],
        "tags": ["exercise", "nutrition", "longevity"],
        "disclaimer": "Always consult a healthcare professional before changing your routine.",
    }


def test_revise_returns_polished_article() -> None:
    fake_response = json.dumps(editor_payload())
    agent = EditorAgent(llm=DummyLLM(fake_response))

    edited = agent.revise(sample_draft())

    assert edited.title == "Two Evidence-Backed Habits to Support Healthy Aging"
    assert edited.summary.startswith("Research-backed")
    assert "Refined Highlights" in edited.body
    assert edited.disclaimer == "Always consult a healthcare professional before changing your routine."
    assert "https://example.com/articles/strength-training" in edited.sources
    assert "https://journal.example.com/strength-training" in edited.sources
    assert edited.tags == ["nutrition", "exercise", "longevity"]

    article = edited.to_article()
    assert article.title == edited.title
    assert "**Key Takeaways**" in article.content_body
    assert "> Always consult" in article.content_body


def test_revise_handles_json_wrapped_in_code_fence() -> None:
    fake_response = "```json\n" + json.dumps(editor_payload(), indent=2) + "\n```\nThanks!"
    agent = EditorAgent(llm=DummyLLM(fake_response))

    edited = agent.revise(sample_draft())

    assert edited.title == "Two Evidence-Backed Habits to Support Healthy Aging"
    assert edited.disclaimer == "Always consult a healthcare professional before changing your routine."


def test_revise_extracts_json_from_surrounding_text() -> None:
    fake_response = (
        "Here is the polished article you requested:\n\n"
        + json.dumps(editor_payload())
        + "\n\nLet me know if you need anything else!"
    )
    agent = EditorAgent(llm=DummyLLM(fake_response))

    edited = agent.revise(sample_draft())

    assert edited.tags == ["nutrition", "exercise", "longevity"]


def test_revise_requires_valid_json() -> None:
    agent = EditorAgent(llm=DummyLLM("not-json"))

    try:
        agent.revise(sample_draft())
    except ValueError as exc:
        assert "valid JSON" in str(exc)
    else:  # pragma: no cover - ensure failure visible
        raise AssertionError("Expected ValueError for invalid JSON response")


def test_local_json_responder_parses_editor_prompt() -> None:
    draft = sample_draft()
    agent = EditorAgent(llm=DummyLLM(""))
    messages = agent.prompt.format_messages(
        draft=json.dumps(
            {
                "title": draft.title,
                "summary": draft.summary,
                "body": draft.body,
                "takeaways": draft.takeaways,
                "sources": draft.sources,
                "tags": draft.tags,
            },
            ensure_ascii=False,
            indent=2,
        ),
        current_date="2024-01-01",
    )

    prompt_text = messages[-1].content
    responder = LocalJSONResponder("editor")

    payload = responder._editor_payload(prompt_text)

    assert payload["title"] == draft.title
    assert payload["summary"] == draft.summary
    assert payload["body"].startswith("## Longevity Highlights")
    assert "disclaimer" in payload
    assert "healthy-aging" in payload["tags"]
