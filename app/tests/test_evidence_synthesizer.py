"""Tests for evidence synthesis.

The behaviour that matters here is what the model *cannot* do: cite a source it was not
given, or write a number that is not already anchored in an extraction.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Sequence

import pytest
from langchain_core.messages import AIMessage

from app.models.evidence import (
    Classification,
    Effect,
    EvidenceRecord,
    Extracted,
    Outcome,
    Span,
)
from app.services.evidence.gates import g2_numbers_traceable
from app.services.evidence.synthesizer import (
    SynthesizerAgent,
    number_references,
    topic_key_for,
)

TRIAL_DOC = (
    "METHODS: We randomised 412 adults to an eight-hour eating window.\n\n"
    "RESULTS: Fasting glucose fell by 4.2 mg/dL over 12 weeks."
)
COHORT_DOC = "We followed 9800 adults for a decade and saw a 15 percent lower risk."
NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


class StubLLM:
    def __init__(self, *responses: Any) -> None:
        self._responses = [
            response if isinstance(response, str) else json.dumps(response)
            for response in responses
        ]
        self.calls: list[Sequence[Any]] = []

    def invoke(self, input: Any, **_: Any) -> AIMessage:
        self.calls.append(input if isinstance(input, list) else [input])
        return AIMessage(content=self._responses[min(len(self.calls) - 1, len(self._responses) - 1)])


def _span(document: str, quote: str) -> Span:
    span = Span.locate(document, quote)
    assert span is not None
    return span


def _trial() -> EvidenceRecord:
    record = EvidenceRecord(
        source_key="doi:10.1/trial",
        title="Time-restricted eating trial",
        document_text=TRIAL_DOC,
        classification=Classification(design="rct", subject="human"),
        sample_size=Extracted.found(412, _span(TRIAL_DOC, "412 adults")),
        intervention=Extracted.found("eight-hour eating window", _span(TRIAL_DOC, "eight-hour eating window")),
        state="approved",
    )
    record.outcomes = [
        Outcome(
            name="fasting glucose",
            is_surrogate=Extracted.found(True, _span(TRIAL_DOC, "Fasting glucose")),
            effect=Effect(magnitude=Extracted.found(4.2, _span(TRIAL_DOC, "4.2 mg/dL"))),
        )
    ]
    return record


def _cohort() -> EvidenceRecord:
    record = EvidenceRecord(
        source_key="doi:10.2/cohort",
        title="Ten-year cohort",
        document_text=COHORT_DOC,
        classification=Classification(design="prospective_cohort", subject="human"),
        sample_size=Extracted.found(9800, _span(COHORT_DOC, "9800 adults")),
        state="approved",
    )
    record.outcomes = [
        Outcome(
            name="mortality",
            effect=Effect(magnitude=Extracted.found(15, _span(COHORT_DOC, "15 percent"))),
        )
    ]
    return record


def _agent(*responses: Any) -> SynthesizerAgent:
    return SynthesizerAgent(
        llm=StubLLM(*responses),
        model_id="stub",
        now=lambda: NOW,
        bundle_id_factory=lambda: "bundle-1",
    )


# -- claims ------------------------------------------------------------


def test_claims_are_built_from_handles_and_typed() -> None:
    agent = _agent(
        {
            "topic": "time-restricted eating and glucose",
            "claims": [
                {
                    "text": "Fasting glucose fell by 4.2 mg/dL in a randomised trial.",
                    "claim_type": "causal",
                    "evidence": ["E1"],
                    "population_scope": "adults aged 40 to 70",
                    "limitations": ["surrogate endpoint"],
                }
            ],
        }
    )

    bundle = agent.synthesize([_trial()])

    assert bundle.bundle_id == "bundle-1"
    claim = bundle.claims[0]
    assert claim.claim_type == "causal"
    assert claim.evidence_keys == ["doi:10.1/trial"]
    assert claim.population_scope == "adults aged 40 to 70"
    assert claim.limitations == ["surrogate endpoint"]


def test_a_handle_that_was_never_issued_is_dropped() -> None:
    """The model cannot cite its way to a source nobody acquired."""

    agent = _agent(
        {"claims": [{"text": "A claim.", "evidence": ["E1", "E7"], "claim_type": "descriptive"}]}
    )

    bundle = agent.synthesize([_trial()])

    assert bundle.claims[0].evidence_keys == ["doi:10.1/trial"]


def test_an_unknown_claim_type_falls_back_to_descriptive() -> None:
    agent = _agent({"claims": [{"text": "A claim.", "claim_type": "revolutionary", "evidence": ["E1"]}]})

    assert _agent and agent.synthesize([_trial()]).claims[0].claim_type == "descriptive"


def test_claims_without_text_are_skipped() -> None:
    agent = _agent({"claims": [{"text": "  ", "evidence": ["E1"]}, "nonsense", 42]})

    assert agent.synthesize([_trial()]).claims == []


def test_disagreement_is_recorded_rather_than_averaged() -> None:
    agent = _agent(
        {
            "claims": [
                {
                    "text": "The trial found a benefit.",
                    "evidence": ["E1"],
                    "contradicts": ["E2"],
                    "claim_type": "causal",
                }
            ]
        }
    )

    bundle = agent.synthesize([_trial(), _cohort()])

    assert bundle.claims[0].contradicted_by == ["doi:10.2/cohort"]


# -- number references -------------------------------------------------


def test_numbers_are_anchored_by_code_not_supplied_by_the_model() -> None:
    agent = _agent(
        {"claims": [{"text": "Glucose fell by 4.2 mg/dL across 412 adults.", "evidence": ["E1"]}]}
    )

    claim = agent.synthesize([_trial()]).claims[0]

    assert {number.text for number in claim.numbers} == {"4.2", "412"}
    assert all(number.source_key == "doi:10.1/trial" for number in claim.numbers)
    assert all(number.span.verify(TRIAL_DOC) for number in claim.numbers)


def test_a_figure_with_no_anchored_match_gets_no_reference() -> None:
    """And therefore fails G2 downstream, which is the whole mechanism."""

    agent = _agent({"claims": [{"text": "Glucose fell by 9.9 mg/dL.", "evidence": ["E1"]}]})

    bundle = agent.synthesize([_trial()])

    assert bundle.claims[0].numbers == []
    assert g2_numbers_traceable(bundle, {"doi:10.1/trial": _trial()}) != []


def test_anchored_numbers_satisfy_the_gate_that_checks_them() -> None:
    agent = _agent(
        {"claims": [{"text": "Glucose fell by 4.2 mg/dL.", "evidence": ["E1"], "claim_type": "causal"}]}
    )

    bundle = agent.synthesize([_trial()])

    assert g2_numbers_traceable(bundle, {"doi:10.1/trial": _trial()}) == []


def test_a_number_is_matched_against_the_record_that_actually_reports_it() -> None:
    references = number_references("Across 9800 adults, glucose fell by 4.2 mg/dL.", [_trial(), _cohort()])

    by_text = {reference.text: reference.source_key for reference in references}

    assert by_text["9800"] == "doi:10.2/cohort"
    assert by_text["4.2"] == "doi:10.1/trial"


def test_years_are_not_treated_as_findings() -> None:
    assert number_references("The 2024 trial reported a benefit.", [_trial()]) == []


# -- topic keys --------------------------------------------------------


def test_the_topic_key_comes_from_the_extracts() -> None:
    assert topic_key_for([_trial()]) == "eight-hour-eating-window|fasting-glucose"


def test_the_topic_key_is_stable_across_runs() -> None:
    assert topic_key_for([_trial()]) == topic_key_for([_trial()])


def test_the_model_hint_is_used_only_when_the_extracts_say_nothing() -> None:
    bare = EvidenceRecord(source_key="doi:10.3/bare")

    assert topic_key_for([bare], hint="Sleep and Cognition") == "sleep-and-cognition"
    assert topic_key_for([bare]) == "unclassified"


# -- prompt ------------------------------------------------------------


def test_the_prompt_contains_extracts_but_never_the_document() -> None:
    """Synthesis works from anchored fields, so it cannot quote unextracted text."""

    stub = StubLLM({"claims": []})
    agent = SynthesizerAgent(llm=stub, model_id="stub", bundle_id_factory=lambda: "b")

    agent.synthesize([_trial()])

    prompt = " ".join(str(getattr(message, "content", message)) for message in stub.calls[0])
    assert "METHODS: We randomised" not in prompt
    assert "[E1]" in prompt
    assert "412" in prompt


def test_unknown_fields_are_shown_as_unknown_in_the_prompt() -> None:
    stub = StubLLM({"claims": []})
    agent = SynthesizerAgent(llm=stub, model_id="stub", bundle_id_factory=lambda: "b")
    record = _trial()
    record.limitations = Extracted.not_reported()

    agent.synthesize([record])

    prompt = " ".join(str(getattr(message, "content", message)) for message in stub.calls[0])
    assert "limitations: not reported" in prompt


def test_synthesis_needs_at_least_one_record() -> None:
    with pytest.raises(ValueError):
        _agent({"claims": []}).synthesize([])


# -- what may back a figure --------------------------------------------


def test_a_study_duration_can_be_cited() -> None:
    """The first live run refused a good article over a duration it had itself extracted."""

    record = _trial()
    record.duration = Extracted.found("12 weeks", _span(TRIAL_DOC, "12 weeks"))

    references = number_references("Glucose fell over 12 weeks.", [record])

    assert [reference.text for reference in references] == ["12"]
    assert references[0].span.verify(TRIAL_DOC)


def test_a_figure_is_matched_token_wise_not_by_digit_substring() -> None:
    """Substring matching accepts "40" from a quote reporting 412 and 70."""

    document = "We randomised 412 adults aged 70 and over."
    record = EvidenceRecord(
        source_key="doi:10.9/substring",
        document_text=document,
        classification=Classification(design="rct", subject="human"),
        sample_size=Extracted.found(412, _span(document, "412 adults aged 70")),
        state="approved",
    )

    assert number_references("A cohort of 412 people.", [record])
    assert number_references("Some 40 participants.", [record]) == []
