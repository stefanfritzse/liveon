"""Tests for the evidence reviewer.

The point of these is the asymmetry: the model's available moves are all safe ones. It can
lower a grade, add a concern, or refuse. Everything else is code's decision.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import pytest
from langchain_core.messages import AIMessage

from app.models.evidence import (
    Claim,
    Classification,
    EvidenceBundle,
    EvidenceRecord,
    Extracted,
    NumberRef,
    Outcome,
    Span,
)
from app.services.evidence.reviewer import EvidenceReviewer, max_regenerations

KEY = "doi:10.1001/jama.2024.1234"
OTHER = "doi:10.1002/trial.2"
DOCUMENT = (
    "METHODS: We randomised 412 adults to an eight-hour eating window.\n\n"
    "RESULTS: Deaths fell by 4.2 percent over ten years."
)
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


class ExplodingLLM:
    def invoke(self, input: Any, **_: Any) -> AIMessage:
        raise RuntimeError("model is down")


def _span(quote: str) -> Span:
    span = Span.locate(DOCUMENT, quote)
    assert span is not None
    return span


def _record(key: str = KEY, **overrides) -> EvidenceRecord:
    defaults = dict(
        source_key=key,
        title="Time-restricted eating",
        document_text=DOCUMENT,
        classification=Classification(design="rct", subject="human"),
        sample_size=Extracted.found(412, _span("412 adults")),
        state="approved",
    )
    defaults.update(overrides)
    record = EvidenceRecord(**defaults)
    record.outcomes = [
        Outcome(name="deaths", is_surrogate=Extracted.found(False, _span("Deaths")))
    ]
    return record


def _bundle(**overrides) -> EvidenceBundle:
    claim = overrides.pop(
        "claim",
        Claim(
            text="The schedule reduced deaths by 4.2 percent.",
            claim_type="causal",
            evidence_keys=[KEY],
            numbers=[NumberRef(text="4.2", source_key=KEY, span=_span("4.2 percent"))],
        ),
    )
    defaults = dict(bundle_id="b1", topic_key="tre|mortality", claims=[claim])
    defaults.update(overrides)
    return EvidenceBundle(**defaults)


def _reviewer(llm=None, **overrides) -> EvidenceReviewer:
    defaults = dict(llm=llm, model_id="stub-model", now=lambda: NOW)
    defaults.update(overrides)
    return EvidenceReviewer(**defaults)


# -- deterministic layer -----------------------------------------------


def test_a_clean_bundle_is_approved_and_graded_without_a_model() -> None:
    """Deterministic-only review is a valid configuration and the benchmark runs it."""

    bundle = _bundle()

    decision = _reviewer().review(bundle, {KEY: _record()})

    assert decision.status == "approved"
    assert decision.grade == "moderate"
    assert decision.is_approved is True
    assert decision.rationale


def test_the_decision_is_recorded_on_the_bundle() -> None:
    bundle = _bundle()

    decision = _reviewer().review(bundle, {KEY: _record()})

    assert bundle.review_status == decision.status
    assert bundle.grade == decision.grade
    assert bundle.review is decision
    assert bundle.grade_rationale == decision.rationale


def test_a_writing_failure_asks_for_a_rewrite() -> None:
    claim = Claim(
        text="The schedule reduced deaths by 9.9 percent.",
        claim_type="causal",
        evidence_keys=[KEY],
    )

    decision = _reviewer().review(_bundle(claim=claim), {KEY: _record()})

    assert decision.status == "regenerate"
    assert decision.may_retry is True
    assert {v.gate for v in decision.violations} >= {"G2"}


def test_an_evidence_failure_is_final() -> None:
    """No rewrite fixes a retracted paper, so the bundle is rejected rather than retried."""

    decision = _reviewer().review(_bundle(), {KEY: _record(retraction_state="retracted")})

    assert decision.status == "rejected"
    assert decision.may_retry is False
    assert decision.grade == "insufficient"


def test_a_topic_inside_the_cooldown_is_rejected_not_retried() -> None:
    decision = _reviewer().review(
        _bundle(), {KEY: _record()}, last_used_at=NOW - timedelta(days=2)
    )

    assert decision.status == "rejected"
    assert {v.gate for v in decision.violations} >= {"G9"}


def test_the_model_is_never_asked_about_a_bundle_the_gates_refused() -> None:
    """There is nothing for it to add, and asking invites it to argue."""

    stub = StubLLM({"status": "approved", "grade": "high"})
    claim = Claim(text="Deaths fell by 9.9 percent.", evidence_keys=[KEY])

    _reviewer(stub).review(_bundle(claim=claim), {KEY: _record()})

    assert stub.calls == []


def test_the_certainty_rule_is_evaluated_against_the_computed_grade() -> None:
    """G8 needs a grade, and the grade needs the other gates: ordering, not luck."""

    claim = Claim(
        text="The benefit is proven.",
        claim_type="associative",
        evidence_keys=[KEY],
    )

    decision = _reviewer().review(_bundle(claim=claim), {KEY: _record()})

    assert decision.status == "regenerate"
    assert any(v.gate == "G8" for v in decision.violations)


def test_certainty_language_survives_when_the_evidence_really_is_high() -> None:
    claim = Claim(
        text="The benefit is proven across trials.",
        claim_type="causal",
        evidence_keys=[KEY, OTHER],
    )
    records = {KEY: _record(), OTHER: _record(OTHER)}

    decision = _reviewer().review(_bundle(claim=claim), records)

    assert decision.grade == "high"
    assert decision.status == "approved"


# -- advisory layer ----------------------------------------------------


def test_the_model_may_lower_a_grade() -> None:
    stub = StubLLM({"status": "downgraded", "grade": "low", "concerns": ["overstated"], "notes": "n"})

    decision = _reviewer(stub).review(_bundle(), {KEY: _record()})

    assert decision.grade == "low"
    assert decision.status == "downgraded"
    assert any(v.gate == "ADVISORY" for v in decision.violations)


def test_the_model_may_not_raise_a_grade() -> None:
    """Invariant I4. A reviewer that can be talked upward is not a reviewer."""

    stub = StubLLM({"status": "approved", "grade": "high", "notes": "looks great"})

    decision = _reviewer(stub).review(_bundle(), {KEY: _record()})

    assert decision.grade == "moderate"


def test_the_model_may_refuse() -> None:
    stub = StubLLM({"status": "rejected", "grade": "moderate", "concerns": ["ignores contrary work"]})

    decision = _reviewer(stub).review(_bundle(), {KEY: _record()})

    assert decision.status == "rejected"
    assert decision.is_approved is False


def test_a_model_grade_of_insufficient_rejects_the_bundle() -> None:
    stub = StubLLM({"status": "approved", "grade": "insufficient"})

    decision = _reviewer(stub).review(_bundle(), {KEY: _record()})

    assert decision.status == "rejected"


def test_an_unrecognised_status_falls_back_to_the_computed_outcome() -> None:
    stub = StubLLM({"status": "brilliant", "grade": "moderate"})

    decision = _reviewer(stub).review(_bundle(), {KEY: _record()})

    assert decision.status == "approved"
    assert decision.grade == "moderate"


def test_a_review_that_cannot_run_refuses_rather_than_publishing() -> None:
    """Fail closed: an unavailable reviewer must not mean an unreviewed publication."""

    decision = _reviewer(ExplodingLLM()).review(_bundle(), {KEY: _record()})

    assert decision.status == "regenerate"
    assert decision.is_approved is False
    assert "unavailable" in decision.notes


def test_unparseable_advice_is_re_asked_then_used() -> None:
    stub = StubLLM("not json", {"status": "approved", "grade": "moderate"})

    decision = _reviewer(stub).review(_bundle(), {KEY: _record()})

    assert len(stub.calls) == 2
    assert decision.status == "approved"


def test_the_advisory_prompt_shows_no_documents_or_urls() -> None:
    """The reviewer judges the writing against summaries, not against raw text it could quote."""

    stub = StubLLM({"status": "approved", "grade": "moderate"})

    _reviewer(stub).review(_bundle(), {KEY: _record()})

    prompt = " ".join(str(getattr(message, "content", message)) for message in stub.calls[0])
    assert "METHODS: We randomised" not in prompt
    assert "http" not in prompt
    assert "Time-restricted eating" in prompt


# -- bookkeeping -------------------------------------------------------


def test_the_decision_records_what_reviewed_it() -> None:
    stub = StubLLM({"status": "approved", "grade": "moderate"})

    decision = _reviewer(stub).review(_bundle(), {KEY: _record()})

    assert decision.model_id == "stub-model"
    assert decision.prompt_version == "1"
    assert decision.reviewed_at == NOW


def test_a_decision_round_trips_through_storage() -> None:
    from app.models.evidence import ReviewDecision

    decision = _reviewer().review(_bundle(), {KEY: _record()})

    restored = ReviewDecision.from_document(decision.to_document())

    assert restored is not None
    assert restored.status == decision.status
    assert restored.grade == decision.grade
    assert restored.reviewed_at == decision.reviewed_at


def test_the_regeneration_cap_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert max_regenerations() == 2

    monkeypatch.setenv("LIVEON_MAX_REGENERATIONS", "0")
    assert max_regenerations() == 0

    monkeypatch.setenv("LIVEON_MAX_REGENERATIONS", "nonsense")
    assert max_regenerations() == 2
