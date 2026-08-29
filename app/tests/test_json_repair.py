"""Tests for the shared model-output JSON parser and its re-ask retry.

This logic lived in four near-identical copies across the agents, and a single
malformed reply discarded a whole pipeline run along with the feed fetching and
summarisation that preceded it.
"""

from __future__ import annotations

import pytest

from app.utils.json_repair import (
    JsonParseError,
    extract_message_text,
    invoke_json_object,
    parse_json_object,
)
from app.utils.langchain_compat import AIMessage


class _ScriptedLLM:
    """Returns each scripted reply in turn, recording what it was asked."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[object] = []

    def invoke(self, messages: object) -> AIMessage:
        self.calls.append(messages)
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return AIMessage(content=reply)


# ----------------------------------------------------------------------
# Parsing ladder
# ----------------------------------------------------------------------


def test_plain_json_parses() -> None:
    assert parse_json_object('{"title": "Sleep"}') == {"title": "Sleep"}


def test_fenced_json_parses() -> None:
    text = '```json\n{"title": "Sleep"}\n```'

    assert parse_json_object(text) == {"title": "Sleep"}


def test_unlabelled_fence_parses() -> None:
    assert parse_json_object('```\n{"a": 1}\n```') == {"a": 1}


def test_json_with_preamble_parses() -> None:
    text = 'Sure! Here is the article you asked for:\n{"title": "Sleep"}\nHope that helps.'

    assert parse_json_object(text) == {"title": "Sleep"}


def test_python_dict_literal_parses() -> None:
    """Small models often answer with Python's True/None instead of JSON's."""

    assert parse_json_object("{'ok': True, 'note': None}") == {"ok": True, "note": None}


def test_nested_objects_survive() -> None:
    payload = parse_json_object('{"metadata": {"sources": ["https://a"], "confidence": "high"}}')

    assert payload["metadata"]["sources"] == ["https://a"]


def test_empty_response_is_rejected() -> None:
    with pytest.raises(JsonParseError, match="empty"):
        parse_json_object("   ", label="Summarizer")


def test_prose_only_response_is_rejected() -> None:
    with pytest.raises(JsonParseError, match="valid JSON"):
        parse_json_object("I cannot help with that.", label="Editor")


def test_a_json_array_is_not_an_object() -> None:
    with pytest.raises(JsonParseError):
        parse_json_object('["not", "an", "object"]')


def test_the_label_appears_in_the_error() -> None:
    with pytest.raises(JsonParseError, match="Tip generator"):
        parse_json_object("nope", label="Tip generator")


def test_parse_error_is_a_value_error() -> None:
    """Callers already guard on ValueError."""

    assert issubclass(JsonParseError, ValueError)


# ----------------------------------------------------------------------
# Message extraction
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("plain", "plain"),
        (AIMessage(content="from message"), "from message"),
        ({"content": "from dict"}, "from dict"),
        (None, ""),
    ],
)
def test_message_text_is_extracted(response: object, expected: str) -> None:
    assert extract_message_text(response) == expected


def test_list_content_is_joined() -> None:
    assert extract_message_text(AIMessage(content=["a", "b"])) == "ab"


# ----------------------------------------------------------------------
# Re-ask retry
# ----------------------------------------------------------------------


def test_a_good_first_reply_is_used_directly() -> None:
    llm = _ScriptedLLM('{"ok": true}')

    assert invoke_json_object(llm, ["prompt"], label="Editor") == {"ok": True}
    assert len(llm.calls) == 1


def test_a_malformed_reply_triggers_one_retry() -> None:
    """One bad reply used to discard the whole run."""

    llm = _ScriptedLLM("Sorry, I cannot do that.", '{"recovered": true}')

    payload = invoke_json_object(llm, ["prompt"], label="Editor")

    assert payload == {"recovered": True}
    assert len(llm.calls) == 2


def test_the_retry_explains_the_problem_to_the_model() -> None:
    llm = _ScriptedLLM("prose only", '{"ok": true}')

    invoke_json_object(llm, ["prompt"], label="Editor")

    correction = llm.calls[1][-1]
    text = getattr(correction, "content", "")
    assert "could not be parsed as JSON" in text
    assert "ONLY the JSON object" in text
    # The original conversation is preserved ahead of the correction.
    assert llm.calls[1][0] == "prompt"


def test_the_retry_quotes_the_bad_reply_back() -> None:
    llm = _ScriptedLLM("I refuse to answer", '{"ok": true}')

    invoke_json_object(llm, ["prompt"], label="Editor")

    assert "I refuse to answer" in getattr(llm.calls[1][-1], "content", "")


def test_persistent_failure_raises_after_the_retry() -> None:
    llm = _ScriptedLLM("nope", "still nope")

    with pytest.raises(JsonParseError, match="valid JSON"):
        invoke_json_object(llm, ["prompt"], label="Editor")

    assert len(llm.calls) == 2


def test_retries_can_be_disabled() -> None:
    llm = _ScriptedLLM("nope")

    with pytest.raises(JsonParseError):
        invoke_json_object(llm, ["prompt"], label="Editor", retries=0)

    assert len(llm.calls) == 1


def test_extra_retries_are_honoured() -> None:
    llm = _ScriptedLLM("bad", "bad", '{"ok": true}')

    assert invoke_json_object(llm, ["prompt"], label="Editor", retries=2) == {"ok": True}
    assert len(llm.calls) == 3


# ----------------------------------------------------------------------
# The agents use the shared implementation
# ----------------------------------------------------------------------


def test_agents_share_one_parser() -> None:
    """The ladder used to be copy-pasted into all four agents."""

    from app.services import editor, summarizer, tip_editor, tip_generator

    for module in (summarizer, editor, tip_generator, tip_editor):
        source = module.__file__
        assert source
        text = open(source, encoding="utf-8").read()
        assert "invoke_json_object" in text, f"{module.__name__} bypasses the shared parser"
        assert "_scan_for_object" not in text, f"{module.__name__} still has its own copy"


def test_summarizer_recovers_from_one_bad_reply() -> None:
    from app.models.aggregator import AggregatedContent
    from app.services.summarizer import SummarizerAgent
    from datetime import datetime, timezone

    llm = _ScriptedLLM(
        "Here you go!",
        '{"title": "T", "summary": "S", "body": "B", "takeaways": [], "sources": [], "tags": []}',
    )
    agent = SummarizerAgent(llm=llm)

    draft = agent.summarize(
        [
            AggregatedContent(
                title="Study",
                url="https://example.test/study",
                summary="Summary",
                published_at=datetime.now(timezone.utc),
                source="Feed",
            )
        ]
    )

    assert draft.title == "T"
    assert len(llm.calls) == 2
