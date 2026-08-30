"""Tests for metadata-driven classification.

Design and subject decide how strongly a finding may be stated, so they are computed from
indexed metadata rather than asked of a model (I1). What matters here is that the answer
is right, that it is conservative when the metadata is silent, and that it records why.
"""

from __future__ import annotations

import pytest

from app.models.evidence import EvidenceRecord
from app.services.evidence.classification import classify, classify_record


def test_randomised_human_trial() -> None:
    result = classify(
        publication_types=["Journal Article", "Randomized Controlled Trial"],
        mesh_terms=["Humans", "Middle Aged", "Fasting"],
    )

    assert result.design == "rct"
    assert result.subject == "human"
    assert result.is_known is True
    assert result.is_human is True


def test_meta_analysis_wins_over_the_review_label_it_also_carries() -> None:
    """PubMed indexes a meta-analysis as both; the stronger design must win."""

    result = classify(
        publication_types=["Review", "Meta-Analysis", "Systematic Review"],
        mesh_terms=["Humans"],
    )

    assert result.design == "meta_analysis"


def test_mouse_study_is_animal_evidence() -> None:
    result = classify(publication_types=["Journal Article"], mesh_terms=["Animals", "Mice"])

    assert result.subject == "animal"
    assert result.is_human is False
    # Nothing labels the design, but a mouse experiment is still preclinical, and
    # calling it "unknown" would block it under G10 for the wrong reason.
    assert result.design == "preclinical"


def test_study_in_both_humans_and_animals_is_mixed() -> None:
    result = classify(publication_types=["Journal Article"], mesh_terms=["Humans", "Animals"])

    assert result.subject == "mixed"
    assert result.is_human is True


def test_cell_culture_work_is_in_vitro() -> None:
    result = classify(
        publication_types=["Journal Article"], mesh_terms=["Cells, Cultured", "Autophagy"]
    )

    assert result.subject == "in_vitro"
    assert result.design == "preclinical"


@pytest.mark.parametrize(
    ("mesh", "expected"),
    [
        (["Humans", "Prospective Studies"], "prospective_cohort"),
        (["Humans", "Cohort Studies"], "prospective_cohort"),
        (["Humans", "Retrospective Studies"], "retrospective_cohort"),
        (["Humans", "Case-Control Studies"], "case_control"),
        (["Humans", "Cross-Sectional Studies"], "cross_sectional"),
    ],
)
def test_observational_designs_come_from_mesh_when_no_trial_type_is_indexed(
    mesh: list[str], expected: str
) -> None:
    result = classify(publication_types=["Journal Article"], mesh_terms=mesh)

    assert result.design == expected
    assert result.subject == "human"


def test_publication_type_beats_mesh_when_both_are_present() -> None:
    result = classify(
        publication_types=["Randomized Controlled Trial"],
        mesh_terms=["Humans", "Prospective Studies"],
    )

    assert result.design == "rct"


def test_unindexed_record_stays_unknown() -> None:
    """Silence is not permission: unknown caps the grade at insufficient under G10."""

    result = classify(publication_types=["Journal Article"], mesh_terms=[])

    assert result.design == "unknown"
    assert result.subject == "unknown"
    assert result.is_known is False


def test_classification_records_what_decided_it() -> None:
    result = classify(
        publication_types=["Randomized Controlled Trial"], mesh_terms=["Humans"]
    )

    assert "pt:randomized controlled trial" in result.basis
    assert "humans" in result.basis


def test_source_type_answers_for_aggregated_evidence() -> None:
    result = classify(
        publication_types=["Journal Article"], mesh_terms=["Humans"], source_type="meta_analysis"
    )

    assert result.design == "meta_analysis"
    assert result.basis[-1].startswith("source_type:")


def test_classify_record_reads_the_metadata_the_record_arrived_with() -> None:
    record = EvidenceRecord(
        source_key="doi:10.1/x",
        publication_types=["Meta-Analysis"],
        mesh_terms=["Humans"],
    )

    assert classify_record(record).design == "meta_analysis"


def test_malformed_metadata_does_not_raise() -> None:
    assert classify(publication_types=None, mesh_terms="Humans").design == "unknown"
