"""Tests for the grade rubric — one per row of the table in improvements.md item 3."""

from __future__ import annotations

import pytest

from app.models.evidence import (
    Claim,
    Classification,
    EvidenceBundle,
    EvidenceRecord,
    Extracted,
    Outcome,
    Span,
    Violation,
)
from app.services.evidence.grading import compute_grade, describe_grade

DOCUMENT = "We randomised 412 adults and measured deaths over ten years."


def _span(quote: str) -> Span:
    span = Span.locate(DOCUMENT, quote)
    assert span is not None
    return span


def _record(
    key: str,
    *,
    design: str = "rct",
    subject: str = "human",
    sample: int | None = 412,
    surrogate: bool | None = False,
    source_type: str = "journal_article",
) -> EvidenceRecord:
    record = EvidenceRecord(
        source_key=key,
        source_type=source_type,
        document_text=DOCUMENT,
        classification=Classification(design=design, subject=subject),
        sample_size=(
            Extracted.found(sample, _span("412 adults"))
            if sample is not None
            else Extracted.not_reported()
        ),
        state="approved",
    )
    if surrogate is not None:
        record.outcomes = [
            Outcome(name="endpoint", is_surrogate=Extracted.found(surrogate, _span("deaths")))
        ]
    return record


def _bundle(*keys: str, contradicted: bool = False) -> EvidenceBundle:
    claim = Claim(
        text="A claim.",
        evidence_keys=list(keys),
        contradicted_by=["doi:10.9/other"] if contradicted else [],
    )
    return EvidenceBundle(bundle_id="b1", claims=[claim])


def _grade(records: dict[str, EvidenceRecord], violations=(), contradicted=False) -> str:
    bundle = _bundle(*records, contradicted=contradicted)
    grade, rationale = compute_grade(bundle, records, violations)
    assert rationale, "every grade must explain itself"
    return grade


# -- the rubric rows ---------------------------------------------------


def test_high_for_a_meta_analysis_of_human_trials() -> None:
    records = {
        "doi:a": _record("doi:a", design="meta_analysis"),
        "doi:b": _record("doi:b", design="rct"),
    }

    assert _grade(records) == "high"


def test_a_lone_review_is_moderate_because_we_cannot_tell_what_it_pooled() -> None:
    """A systematic review says it pooled something, not what. Ambiguity resolves down."""

    records = {"doi:a": _record("doi:a", design="systematic_review")}

    assert _grade(records) == "moderate"


def test_a_pooled_analysis_indexed_as_randomised_reaches_high_alone() -> None:
    record = _record("doi:a", design="meta_analysis")
    record.publication_types = ["Meta-Analysis", "Randomized Controlled Trial"]

    assert _grade({"doi:a": record}) == "high"


def test_high_for_two_independent_agreeing_human_trials() -> None:
    records = {"doi:a": _record("doi:a"), "doi:b": _record("doi:b")}

    assert _grade(records) == "high"


def test_moderate_for_a_single_adequately_sized_trial() -> None:
    records = {"doi:a": _record("doi:a", sample=412)}

    assert _grade(records) == "moderate"


def test_moderate_for_pooled_observational_evidence() -> None:
    records = {
        "doi:a": _record("doi:a", design="systematic_review"),
        "doi:b": _record("doi:b", design="prospective_cohort"),
    }

    assert _grade(records) == "moderate"


def test_low_for_human_observational_evidence_alone() -> None:
    records = {"doi:a": _record("doi:a", design="prospective_cohort")}

    assert _grade(records) == "low"


def test_low_for_a_trial_that_only_moved_a_biomarker() -> None:
    records = {"doi:a": _record("doi:a", surrogate=True)}

    assert _grade(records) == "low"


def test_low_for_a_trial_too_small_to_be_moderate() -> None:
    records = {"doi:a": _record("doi:a", sample=60)}

    assert _grade(records) == "low"


def test_preliminary_for_animal_evidence() -> None:
    records = {"doi:a": _record("doi:a", design="preclinical", subject="animal", sample=None)}

    assert _grade(records) == "preliminary"


def test_preliminary_for_a_preprint() -> None:
    records = {"doi:a": _record("doi:a", source_type="preprint")}

    assert _grade(records) == "preliminary"


def test_preliminary_for_a_study_below_the_sample_floor() -> None:
    document = "We randomised 12 adults and measured deaths over ten years."
    record = _record("doi:a")
    record.document_text = document
    span = Span.locate(document, "12 adults")
    assert span is not None
    record.sample_size = Extracted.found(12, span)

    assert _grade({"doi:a": record}) == "preliminary"


# -- blocking and capping ----------------------------------------------


@pytest.mark.parametrize("gate", ["G1", "G2", "G6", "G10"])
def test_a_blocking_gate_forces_insufficient(gate: str) -> None:
    """These four mean the evidence cannot be relied on at all, so nothing publishes."""

    records = {
        "doi:a": _record("doi:a", design="meta_analysis"),
        "doi:b": _record("doi:b", design="rct"),
    }

    assert _grade(records, violations=[Violation(gate=gate, detail="x")]) == "insufficient"


def test_a_capping_gate_lowers_but_does_not_block() -> None:
    records = {
        "doi:a": _record("doi:a", design="meta_analysis"),
        "doi:b": _record("doi:b", design="rct"),
    }

    assert _grade(records, violations=[Violation(gate="G7", detail="tiny")]) == "preliminary"
    assert _grade(records, violations=[Violation(gate="G5", detail="surrogate")]) == "low"


def test_the_strictest_cap_wins() -> None:
    records = {
        "doi:a": _record("doi:a", design="meta_analysis"),
        "doi:b": _record("doi:b", design="rct"),
    }
    violations = [Violation(gate="G5", detail="x"), Violation(gate="G7", detail="y")]

    assert _grade(records, violations=violations) == "preliminary"


def test_a_cap_never_raises_a_lower_grade() -> None:
    records = {"doi:a": _record("doi:a", design="preclinical", subject="animal", sample=None)}

    assert _grade(records, violations=[Violation(gate="G5", detail="x")]) == "preliminary"


def test_contradicted_evidence_cannot_reach_high() -> None:
    records = {
        "doi:a": _record("doi:a", design="meta_analysis"),
        "doi:b": _record("doi:b", design="rct"),
    }

    assert _grade(records, contradicted=True) == "moderate"


def test_a_bundle_citing_nothing_resolvable_is_insufficient() -> None:
    bundle = _bundle("doi:missing")

    grade, rationale = compute_grade(bundle, {})

    assert grade == "insufficient"
    assert "No cited evidence" in rationale[0]


def test_an_unclassified_endpoint_does_not_count_as_clinical() -> None:
    """The optimistic reading is exactly what must not leak into a grade."""

    records = {
        "doi:a": _record("doi:a", design="meta_analysis", surrogate=None),
        "doi:b": _record("doi:b", design="rct", surrogate=None),
    }

    assert _grade(records) == "moderate"


# -- reader-facing wording ---------------------------------------------


def test_the_summary_line_is_built_from_the_records_themselves() -> None:
    records = [
        _record("doi:a", design="rct"),
        _record("doi:b", design="prospective_cohort"),
    ]

    assert describe_grade("moderate", records) == (
        "Moderate — 1 human cohort study, 1 human randomised trial"
    )


def test_the_summary_line_counts_repeated_designs() -> None:
    records = [_record("doi:a"), _record("doi:b")]

    assert describe_grade("high", records) == "High — 2 human randomised trials"


def test_animal_evidence_is_named_as_animal_evidence_in_the_summary() -> None:
    records = [_record("doi:a", design="preclinical", subject="animal", sample=None)]

    assert "animal" in describe_grade("preliminary", records)


def test_unassessed_content_says_so() -> None:
    assert describe_grade("insufficient", [_record("doi:a")]) == "Not assessed"
    assert describe_grade("moderate", []) == "Not assessed"


def test_design_labels_are_pluralised_properly() -> None:
    """"2 human meta-analysiss" reached a reader once."""

    records = [_record("doi:a", design="meta_analysis"), _record("doi:b", design="meta_analysis")]

    assert describe_grade("high", records) == "High — 2 human meta-analyses"
