"""Tests for the deterministic publication gates.

These are the controls that stand in for a human reviewer, so each is tested on its own
and on the failure it exists to prevent. Every test here runs without a model.
"""

from __future__ import annotations

import pytest

from app.models.evidence import (
    Claim,
    Classification,
    EvidenceBundle,
    EvidenceRecord,
    NumberRef,
    Span,
)
from app.services.evidence.gates import (
    g1_sources_resolve,
    g2_numbers_traceable,
    g6_no_retracted_sources,
    g10_unknown_ceiling,
    numeric_tokens,
    run_gates,
)

KEY = "doi:10.1001/jama.2024.1234"
DOCUMENT = (
    "METHODS: We randomised 412 adults to an eight-hour eating window.\n\n"
    "RESULTS: Fasting glucose fell by 4.2 mg/dL over 12 weeks."
)


def _record(**overrides) -> EvidenceRecord:
    defaults = dict(
        source_key=KEY,
        document_text=DOCUMENT,
        classification=Classification(design="rct", subject="human", basis=("pt:rct",)),
        state="approved",
    )
    defaults.update(overrides)
    return EvidenceRecord(**defaults)


def _span(quote: str) -> Span:
    span = Span.locate(DOCUMENT, quote)
    assert span is not None
    return span


def _bundle(claim: Claim) -> EvidenceBundle:
    return EvidenceBundle(bundle_id="b1", claims=[claim])


# -- G1 ----------------------------------------------------------------


def test_g1_rejects_a_key_no_one_acquired() -> None:
    """An invented citation is impossible, not merely unlikely."""

    bundle = _bundle(Claim(text="Fasting helps.", evidence_keys=["doi:10.9999/invented"]))

    violations = g1_sources_resolve(bundle, {KEY: _record()})

    assert [violation.gate for violation in violations] == ["G1"]
    assert "no such record" in violations[0].detail


def test_g1_rejects_a_record_that_has_not_cleared_review() -> None:
    bundle = _bundle(Claim(text="Fasting helps.", evidence_keys=[KEY]))

    violations = g1_sources_resolve(bundle, {KEY: _record(state="extracted")})

    assert violations and violations[0].gate == "G1"
    assert "state" in violations[0].detail


def test_g1_rejects_a_claim_citing_nothing_at_all() -> None:
    violations = g1_sources_resolve(_bundle(Claim(text="Fasting helps.")), {})

    assert violations and "cites no evidence" in violations[0].detail


def test_g1_passes_an_approved_citation() -> None:
    bundle = _bundle(Claim(text="Fasting helps.", evidence_keys=[KEY]))

    assert g1_sources_resolve(bundle, {KEY: _record()}) == []


# -- G2 ----------------------------------------------------------------


def test_numeric_tokens_finds_figures_and_ignores_years() -> None:
    tokens = numeric_tokens("A 2023 trial found a 4.2 mg/dL drop across 1,200 adults and 15%.")

    assert tokens == ["4.2", "1,200", "15%"]


def test_g2_rejects_a_number_with_no_reference() -> None:
    bundle = _bundle(
        Claim(text="Glucose fell by 4.2 mg/dL.", evidence_keys=[KEY], numbers=[])
    )

    violations = g2_numbers_traceable(bundle, {KEY: _record()})

    assert violations and violations[0].gate == "G2"
    assert "not traceable" in violations[0].detail


def test_g2_accepts_a_number_anchored_in_the_source() -> None:
    bundle = _bundle(
        Claim(
            text="Glucose fell by 4.2 mg/dL.",
            evidence_keys=[KEY],
            numbers=[NumberRef(text="4.2", source_key=KEY, span=_span("4.2 mg/dL"))],
        )
    )

    assert g2_numbers_traceable(bundle, {KEY: _record()}) == []


def test_g2_rejects_a_number_that_is_not_in_the_text_it_quotes() -> None:
    """The classic failure: a real quote carrying a figure the paper never reported."""

    bundle = _bundle(
        Claim(
            text="Glucose fell by 9.9 mg/dL.",
            evidence_keys=[KEY],
            numbers=[NumberRef(text="9.9", source_key=KEY, span=_span("4.2 mg/dL"))],
        )
    )

    violations = g2_numbers_traceable(bundle, {KEY: _record()})

    assert violations
    assert any("does not appear in the quoted source text" in v.detail for v in violations)


def test_g2_rejects_a_span_that_no_longer_matches_the_stored_document() -> None:
    stale = NumberRef(text="4.2", source_key=KEY, span=Span(quote="4.2 mg/dL", start=0, end=9))
    bundle = _bundle(
        Claim(text="Glucose fell by 4.2 mg/dL.", evidence_keys=[KEY], numbers=[stale])
    )

    violations = g2_numbers_traceable(bundle, {KEY: _record()})

    assert violations
    assert any("no longer matches" in v.detail for v in violations)


def test_g2_rejects_a_number_citing_an_unknown_source() -> None:
    bundle = _bundle(
        Claim(
            text="Glucose fell by 4.2 mg/dL.",
            evidence_keys=[KEY],
            numbers=[NumberRef(text="4.2", source_key="doi:10.9999/nope", span=_span("4.2 mg/dL"))],
        )
    )

    violations = g2_numbers_traceable(bundle, {KEY: _record()})

    assert any("unknown source" in v.detail for v in violations)


def test_g2_matches_across_thousands_separators() -> None:
    document = "We followed 1200 adults for a decade."
    record = _record(document_text=document)
    span = Span.locate(document, "1200 adults")
    assert span is not None
    bundle = _bundle(
        Claim(
            text="The cohort followed 1,200 adults.",
            evidence_keys=[KEY],
            numbers=[NumberRef(text="1,200", source_key=KEY, span=span)],
        )
    )

    assert g2_numbers_traceable(bundle, {KEY: record}) == []


def test_g2_ignores_years_in_prose() -> None:
    bundle = _bundle(Claim(text="A 2024 trial reported benefits.", evidence_keys=[KEY]))

    assert g2_numbers_traceable(bundle, {KEY: _record()}) == []


# -- G6 ----------------------------------------------------------------


@pytest.mark.parametrize("state", ["retracted", "concern"])
def test_g6_blocks_retracted_and_concerning_sources(state: str) -> None:
    bundle = _bundle(Claim(text="Fasting helps.", evidence_keys=[KEY]))
    record = _record(retraction_state=state, retraction_notes=["RetractionIn: JAMA 2025"])

    violations = g6_no_retracted_sources(bundle, {KEY: record})

    assert violations and violations[0].gate == "G6"
    assert state in violations[0].detail


def test_g6_allows_a_corrected_paper() -> None:
    bundle = _bundle(Claim(text="Fasting helps.", evidence_keys=[KEY]))

    assert g6_no_retracted_sources(bundle, {KEY: _record(retraction_state="corrected")}) == []


# -- G10 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "classification",
    [
        Classification(design="unknown", subject="human"),
        Classification(design="rct", subject="unknown"),
        Classification(),
    ],
)
def test_g10_blocks_sources_we_cannot_classify(classification: Classification) -> None:
    bundle = _bundle(Claim(text="Fasting helps.", evidence_keys=[KEY]))

    violations = g10_unknown_ceiling(bundle, {KEY: _record(classification=classification)})

    assert violations and violations[0].gate == "G10"


def test_g10_does_not_double_report_a_key_g1_already_rejected() -> None:
    bundle = _bundle(Claim(text="Fasting helps.", evidence_keys=["doi:10.9999/nope"]))

    assert g10_unknown_ceiling(bundle, {}) == []


# -- run_gates ---------------------------------------------------------


def test_run_gates_reports_every_failure_at_once() -> None:
    bundle = _bundle(
        Claim(
            text="Glucose fell by 9.9 mg/dL.",
            evidence_keys=[KEY, "doi:10.9999/invented"],
        )
    )
    record = _record(retraction_state="retracted", classification=Classification())

    gates = {violation.gate for violation in run_gates(bundle, {KEY: record})}

    assert gates == {"G1", "G2", "G6", "G10"}


def test_run_gates_passes_a_clean_bundle() -> None:
    bundle = _bundle(
        Claim(
            text="Glucose fell by 4.2 mg/dL in a randomised trial.",
            claim_type="causal",
            evidence_keys=[KEY],
            numbers=[NumberRef(text="4.2", source_key=KEY, span=_span("4.2 mg/dL"))],
        )
    )

    assert run_gates(bundle, {KEY: _record()}) == []
