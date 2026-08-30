"""Tests for span-anchored extraction.

The extractor is where a local model is most likely to invent: asked for a sample size an
abstract never states, it will supply a plausible one. The defence is that a value only
survives if the quote beside it exists in the document, so these tests are mostly about
what happens when it does not.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Sequence

from langchain_core.messages import AIMessage

from app.models.evidence import Classification, EvidenceRecord, Extracted
from app.services.evidence.extractor import ExtractorAgent

DOCUMENT = (
    "Time-restricted eating and cardiometabolic risk\n\n"
    "METHODS: We randomised 412 adults aged 40 to 70 to an eight-hour eating window.\n\n"
    "RESULTS: Fasting glucose fell by 4.2 mg/dL over 12 weeks. The trial was funded by "
    "the National Institute on Aging."
)


class StubLLM:
    """Returns canned payloads, one per invocation."""

    def __init__(self, *responses: Any) -> None:
        self._responses = [
            response if isinstance(response, str) else json.dumps(response)
            for response in responses
        ]
        self.calls: list[Sequence[Any]] = []

    def invoke(self, input: Any, **_: Any) -> AIMessage:
        self.calls.append(input if isinstance(input, list) else [input])
        response = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        return AIMessage(content=response)


def _record(**overrides) -> EvidenceRecord:
    defaults = dict(
        source_key="doi:10.1001/jama.2024.1234",
        document_text=DOCUMENT,
        classification=Classification(design="rct", subject="human"),
        state="acquired",
    )
    defaults.update(overrides)
    return EvidenceRecord(**defaults)


def _agent(*responses: Any, **overrides) -> ExtractorAgent:
    defaults = dict(llm=StubLLM(*responses), model_id="stub-model")
    defaults.update(overrides)
    return ExtractorAgent(**defaults)


# -- anchoring ---------------------------------------------------------


def test_a_quoted_value_is_anchored_to_real_offsets() -> None:
    agent = _agent(
        {
            "sample_size": {"value": 412, "quote": "412 adults"},
            "population": {"value": "adults aged 40 to 70", "quote": "adults aged 40 to 70"},
        }
    )

    record = agent.extract(_record())

    assert record.sample_size.value == 412
    assert record.sample_size.status == "extracted"
    assert record.sample_size.span is not None
    assert record.sample_size.span.verify(DOCUMENT) is True
    assert DOCUMENT[record.sample_size.span.start : record.sample_size.span.end] == "412 adults"


def test_an_invented_quote_loses_its_value() -> None:
    """The signature of a fabricated number is a quote that is not in the document."""

    agent = _agent({"sample_size": {"value": 1200, "quote": "we enrolled 1,200 participants"}})

    record = agent.extract(_record())

    assert record.sample_size.status == "not_extractable"
    assert record.sample_size.value is None


def test_a_real_quote_carrying_a_number_it_does_not_contain_is_refused() -> None:
    """A subtler failure: the quote is genuine, the figure beside it is not."""

    agent = _agent({"sample_size": {"value": 900, "quote": "412 adults"}})

    record = agent.extract(_record())

    assert record.sample_size.status == "not_extractable"


def test_not_reported_is_preserved_as_an_answer() -> None:
    agent = _agent({"funding": "not_reported", "conflicts": {"status": "not_reported"}})

    record = agent.extract(_record())

    assert record.funding.status == "not_reported"
    assert record.conflicts.status == "not_reported"
    assert record.funding.is_known is False


def test_a_missing_field_is_unknown_rather_than_empty() -> None:
    record = _agent({}).extract(_record())

    assert record.population.status == "not_reported"
    assert record.sample_size.status == "not_reported"


def test_whitespace_reflowed_by_the_model_still_matches() -> None:
    """Models routinely re-wrap what they copy; that is not evidence of invention."""

    agent = _agent(
        {"population": {"value": "adults", "quote": "We  randomised 412 adults\naged 40 to 70"}}
    )

    record = agent.extract(_record())

    assert record.population.status == "extracted"
    assert record.population.span is not None
    assert record.population.span.verify(DOCUMENT) is True


def test_a_value_without_any_quote_is_refused() -> None:
    agent = _agent({"population": {"value": "older adults"}})

    assert agent.extract(_record()).population.status == "not_extractable"


# -- outcomes ----------------------------------------------------------


def test_outcomes_are_extracted_with_their_effects() -> None:
    agent = _agent(
        {
            "outcomes": [
                {
                    "name": "fasting glucose",
                    "direction": {"value": "decrease", "quote": "Fasting glucose fell"},
                    "is_surrogate": {"value": True, "quote": "Fasting glucose"},
                    "magnitude": {"value": 4.2, "quote": "4.2 mg/dL"},
                    "unit": {"value": "mg/dL", "quote": "mg/dL"},
                }
            ]
        }
    )

    record = agent.extract(_record())

    assert len(record.outcomes) == 1
    outcome = record.outcomes[0]
    assert outcome.name == "fasting glucose"
    assert outcome.is_surrogate.value is True
    assert outcome.effect.magnitude.value == 4.2
    assert outcome.effect.magnitude.span is not None


def test_an_outcome_with_an_invented_effect_keeps_the_endpoint_and_drops_the_number() -> None:
    agent = _agent(
        {
            "outcomes": [
                {
                    "name": "fasting glucose",
                    "magnitude": {"value": 19.0, "quote": "glucose fell by 19 mg/dL"},
                }
            ]
        }
    )

    record = agent.extract(_record())

    assert record.outcomes[0].name == "fasting glucose"
    assert record.outcomes[0].effect.magnitude.status == "not_extractable"


def test_malformed_outcome_entries_are_skipped() -> None:
    agent = _agent({"outcomes": ["a string", {"no_name": True}, 42]})

    assert _record() and agent.extract(_record()).outcomes == []


# -- bookkeeping -------------------------------------------------------


def test_extraction_stamps_the_model_and_prompt_it_used() -> None:
    moment = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
    agent = _agent({}, model_id="qwen2.5:14b", now=lambda: moment)

    record = agent.extract(_record())

    assert record.extraction_model == "qwen2.5:14b"
    assert record.extraction_prompt_version == agent.prompt_version
    assert record.extracted_at == moment
    assert record.state == "extracted"


def test_a_record_already_extracted_by_this_prompt_and_model_is_not_re_extracted() -> None:
    """Extraction is cached; a re-run must not pay for the same abstract twice."""

    stub = StubLLM({})
    agent = ExtractorAgent(llm=stub, model_id="stub-model")
    record = agent.extract(_record())

    agent.extract(record)

    assert len(stub.calls) == 1


def test_a_prompt_version_change_forces_re_extraction() -> None:
    stub = StubLLM({})
    first = ExtractorAgent(llm=stub, model_id="stub-model")
    record = first.extract(_record())

    ExtractorAgent(llm=stub, model_id="stub-model", prompt_version="2").extract(record)

    assert len(stub.calls) == 2


def test_force_re_extracts_even_when_cached() -> None:
    stub = StubLLM({})
    agent = ExtractorAgent(llm=stub, model_id="stub-model")
    record = agent.extract(_record())

    agent.extract(record, force=True)

    assert len(stub.calls) == 2


def test_a_record_with_no_document_is_left_alone() -> None:
    stub = StubLLM({})
    agent = ExtractorAgent(llm=stub, model_id="stub-model")

    record = agent.extract(_record(document_text=""))

    assert stub.calls == []
    assert record.state == "acquired"


def test_the_document_is_put_in_front_of_the_model() -> None:
    stub = StubLLM({})

    ExtractorAgent(llm=stub, model_id="stub-model").extract(_record())

    prompt = " ".join(str(getattr(message, "content", message)) for message in stub.calls[0])
    assert "412 adults" in prompt


def test_an_unparseable_reply_is_re_asked_then_accepted() -> None:
    """``invoke_json_object`` already handles this; extraction inherits it."""

    stub = StubLLM("not json at all", {"sample_size": {"value": 412, "quote": "412 adults"}})
    agent = ExtractorAgent(llm=stub, model_id="stub-model")

    record = agent.extract(_record())

    assert len(stub.calls) == 2
    assert record.sample_size.value == 412


def test_extracted_values_survive_a_storage_round_trip() -> None:
    agent = _agent({"sample_size": {"value": 412, "quote": "412 adults"}})
    record = agent.extract(_record())

    restored = EvidenceRecord.from_document(record.to_document()).verified()

    assert restored.sample_size.value == 412
    assert restored.sample_size.status == "extracted"


def test_a_stale_extraction_is_demoted_when_the_document_changes() -> None:
    agent = _agent({"sample_size": {"value": 412, "quote": "412 adults"}})
    record = agent.extract(_record())

    record.document_text = "A different abstract entirely."

    assert record.verified().sample_size.status == "not_extractable"
    assert Extracted.not_reported().verify(DOCUMENT).status == "not_reported"
