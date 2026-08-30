"""Tests for the policy gates: G3, G4, G5, G7, G8, G9.

The spine gates in test_evidence_gates.py ask whether a citation is real. These ask
whether the sentence built on it is honest — the judgements a human editor would make,
written as code because there is no human editor.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.evidence import (
    Claim,
    Classification,
    EvidenceBundle,
    EvidenceRecord,
    Extracted,
    Outcome,
    Span,
)
from app.services.evidence.gates import (
    CAP_GRADES,
    GATE_SEVERITY,
    g3_subject_consistency,
    g4_causal_language,
    g5_surrogate_endpoints,
    g7_sample_size_floor,
    g8_claim_ceiling,
    g9_topic_cooldown,
)

KEY = "doi:10.1001/jama.2024.1234"
DOCUMENT = (
    "METHODS: We randomised 412 adults to an eight-hour eating window.\n\n"
    "RESULTS: Fasting glucose fell by 4.2 mg/dL over 12 weeks."
)


def _span(quote: str, document: str = DOCUMENT) -> Span:
    span = Span.locate(document, quote)
    assert span is not None
    return span


def _record(**overrides) -> EvidenceRecord:
    defaults = dict(
        source_key=KEY,
        document_text=DOCUMENT,
        classification=Classification(design="rct", subject="human"),
        sample_size=Extracted.found(412, _span("412 adults")),
        state="approved",
    )
    defaults.update(overrides)
    return EvidenceRecord(**defaults)


def _bundle(*claims: Claim, grade: str = "moderate", topic_key: str = "") -> EvidenceBundle:
    return EvidenceBundle(
        bundle_id="b1", claims=list(claims), grade=grade, topic_key=topic_key
    )


# -- G3: subject consistency -------------------------------------------


def _animal_record(**overrides) -> EvidenceRecord:
    defaults = dict(
        classification=Classification(design="preclinical", subject="animal"),
        sample_size=Extracted.not_reported(),
    )
    defaults.update(overrides)
    return _record(**defaults)


@pytest.mark.parametrize(
    "text",
    [
        "People who fast live longer.",
        "Adults saw lower glucose.",
        "Patients benefited from the compound.",
        "You can expect the same effect.",
    ],
)
def test_g3_blocks_human_language_on_animal_only_evidence(text: str) -> None:
    """The failure this exists for: a mouse result written up as a human benefit."""

    violations = g3_subject_consistency(
        _bundle(Claim(text=text, evidence_keys=[KEY])), {KEY: _animal_record()}
    )

    assert violations and violations[0].gate == "G3"


def test_g3_allows_animal_evidence_described_as_animal_evidence() -> None:
    bundle = _bundle(Claim(text="Mice given the compound lived longer.", evidence_keys=[KEY]))

    assert g3_subject_consistency(bundle, {KEY: _animal_record()}) == []


def test_g3_does_not_fire_when_human_evidence_is_also_cited() -> None:
    other = "doi:10.1002/human"
    bundle = _bundle(Claim(text="Adults saw lower glucose.", evidence_keys=[KEY, other]))
    records = {KEY: _animal_record(), other: _record(source_key=other)}

    assert g3_subject_consistency(bundle, records) == []


def test_g3_rejects_rather_than_downgrades() -> None:
    """A claim that says "people" about mice is not weaker, it is untrue."""

    assert GATE_SEVERITY["G3"] == "reject"


# -- G4: causal language -----------------------------------------------


def _cohort_record(**overrides) -> EvidenceRecord:
    defaults = dict(classification=Classification(design="prospective_cohort", subject="human"))
    defaults.update(overrides)
    return _record(**defaults)


def test_g4_blocks_a_causal_claim_type_on_observational_evidence() -> None:
    bundle = _bundle(
        Claim(text="Walking lowers blood pressure.", claim_type="causal", evidence_keys=[KEY])
    )

    violations = g4_causal_language(bundle, {KEY: _cohort_record()})

    assert violations and violations[0].gate == "G4"
    assert "typed causal" in violations[0].detail


def test_g4_blocks_unhedged_causal_prose_however_the_claim_is_labelled() -> None:
    """The label and the prose can disagree, and the prose is what readers get."""

    bundle = _bundle(
        Claim(
            text="Walking reduces blood pressure.",
            claim_type="associative",
            evidence_keys=[KEY],
        )
    )

    violations = g4_causal_language(bundle, {KEY: _cohort_record()})

    assert violations and "Unhedged causal verb" in violations[0].detail


@pytest.mark.parametrize(
    "text",
    [
        "Walking was associated with lower blood pressure.",
        "Walking may reduce blood pressure.",
        "More walking is linked to lower blood pressure.",
    ],
)
def test_g4_allows_hedged_or_associative_wording(text: str) -> None:
    bundle = _bundle(Claim(text=text, claim_type="associative", evidence_keys=[KEY]))

    assert g4_causal_language(bundle, {KEY: _cohort_record()}) == []


def test_g4_allows_causal_language_on_randomised_evidence() -> None:
    bundle = _bundle(
        Claim(text="The diet reduces fasting glucose.", claim_type="causal", evidence_keys=[KEY])
    )

    assert g4_causal_language(bundle, {KEY: _record()}) == []


# -- G5: surrogate endpoints -------------------------------------------


def _surrogate_record(is_surrogate: bool = True, **overrides) -> EvidenceRecord:
    record = _record(**overrides)
    record.outcomes = [
        Outcome(
            name="fasting glucose",
            is_surrogate=Extracted.found(is_surrogate, _span("Fasting glucose")),
        )
    ]
    return record


def test_g5_caps_a_biomarker_presented_as_clinical_benefit() -> None:
    bundle = _bundle(Claim(text="The diet helps people live longer.", evidence_keys=[KEY]))

    violations = g5_surrogate_endpoints(bundle, {KEY: _surrogate_record()})

    assert violations and violations[0].gate == "G5"
    assert GATE_SEVERITY["G5"] == "cap"
    assert CAP_GRADES["G5"] == "low"


def test_g5_allows_the_biomarker_to_be_reported_as_a_biomarker() -> None:
    bundle = _bundle(Claim(text="Fasting glucose improved.", evidence_keys=[KEY]))

    assert g5_surrogate_endpoints(bundle, {KEY: _surrogate_record()}) == []


def test_g5_does_not_fire_when_a_clinical_endpoint_was_measured() -> None:
    bundle = _bundle(Claim(text="The diet reduced mortality.", evidence_keys=[KEY]))

    assert g5_surrogate_endpoints(bundle, {KEY: _surrogate_record(is_surrogate=False)}) == []


def test_g5_does_not_fire_when_no_endpoint_was_classified() -> None:
    """Unknown is not surrogate; G7 and G10 handle missing information."""

    bundle = _bundle(Claim(text="The diet helps people live longer.", evidence_keys=[KEY]))

    assert g5_surrogate_endpoints(bundle, {KEY: _record()}) == []


# -- G7: sample-size floor ---------------------------------------------


def test_g7_caps_a_tiny_human_study() -> None:
    document = "We randomised 12 adults to the protocol."
    record = _record(
        document_text=document,
        sample_size=Extracted.found(12, _span("12 adults", document)),
    )

    violations = g7_sample_size_floor(
        _bundle(Claim(text="The protocol worked.", evidence_keys=[KEY])), {KEY: record}
    )

    assert violations and violations[0].gate == "G7"
    assert CAP_GRADES["G7"] == "preliminary"


def test_g7_caps_when_no_cited_study_reports_its_sample_size() -> None:
    """Not knowing how many people were studied is not the same as it being enough."""

    violations = g7_sample_size_floor(
        _bundle(Claim(text="The protocol worked.", evidence_keys=[KEY])),
        {KEY: _record(sample_size=Extracted.not_reported())},
    )

    assert violations and "No cited human study reports" in violations[0].detail


def test_g7_passes_an_adequately_sized_study() -> None:
    bundle = _bundle(Claim(text="The protocol worked.", evidence_keys=[KEY]))

    assert g7_sample_size_floor(bundle, {KEY: _record()}) == []


def test_g7_ignores_animal_studies() -> None:
    """Sample-size floors are about generalising to people."""

    bundle = _bundle(Claim(text="Mice lived longer.", evidence_keys=[KEY]))

    assert g7_sample_size_floor(bundle, {KEY: _animal_record()}) == []


# -- G8: claim ceiling -------------------------------------------------


def test_g8_applies_the_ceiling_to_every_claim() -> None:
    bundle = _bundle(
        Claim(text="Glucose fell in the trial.", evidence_keys=[KEY]),
        Claim(text="Take 500 mg of magnesium daily.", evidence_keys=[KEY]),
    )

    violations = g8_claim_ceiling(bundle, {KEY: _record()})

    assert [violation.gate for violation in violations] == ["G8"]
    assert "dosing" in violations[0].detail


def test_g8_reads_the_bundle_grade_for_the_certainty_rule() -> None:
    claim = Claim(text="The benefit is proven.", evidence_keys=[KEY])

    assert g8_claim_ceiling(_bundle(claim, grade="moderate"), {KEY: _record()}) != []
    assert g8_claim_ceiling(_bundle(claim, grade="high"), {KEY: _record()}) == []


# -- G9: topic cooldown ------------------------------------------------


def test_g9_refuses_a_topic_published_inside_the_cooldown() -> None:
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    bundle = _bundle(topic_key="tre|glucose")

    violations = g9_topic_cooldown(
        bundle, {}, last_used_at=now - timedelta(days=3), now=now, cooldown_days=30
    )

    assert violations and violations[0].gate == "G9"
    assert "3 day(s) ago" in violations[0].detail


def test_g9_allows_a_topic_once_the_window_has_passed() -> None:
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    bundle = _bundle(topic_key="tre|glucose")

    assert (
        g9_topic_cooldown(
            bundle, {}, last_used_at=now - timedelta(days=31), now=now, cooldown_days=30
        )
        == []
    )


def test_g9_is_silent_for_a_topic_never_published() -> None:
    assert g9_topic_cooldown(_bundle(topic_key="tre|glucose"), {}, last_used_at=None) == []


def test_g9_needs_a_topic_key_to_compare() -> None:
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)

    assert g9_topic_cooldown(_bundle(), {}, last_used_at=now, now=now) == []


def test_g9_reads_the_cooldown_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_TOPIC_COOLDOWN_DAYS", "7")
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    bundle = _bundle(topic_key="tre|glucose")

    assert g9_topic_cooldown(bundle, {}, last_used_at=now - timedelta(days=10), now=now) == []
    assert g9_topic_cooldown(bundle, {}, last_used_at=now - timedelta(days=2), now=now) != []


def test_an_invalid_cooldown_setting_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVEON_TOPIC_COOLDOWN_DAYS", "not-a-number")
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    bundle = _bundle(topic_key="tre|glucose")

    assert g9_topic_cooldown(bundle, {}, last_used_at=now - timedelta(days=2), now=now) != []


# -- severity policy ---------------------------------------------------


def test_every_gate_declares_a_severity() -> None:
    """The grader reads this table rather than re-deriving the policy."""

    assert set(GATE_SEVERITY) == {f"G{index}" for index in range(1, 11)}
    assert set(CAP_GRADES) <= {gate for gate, kind in GATE_SEVERITY.items() if kind == "cap"}
