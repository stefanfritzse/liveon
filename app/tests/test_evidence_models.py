"""Tests for the evidence model invariants.

These protect I2 (every value is anchored to a span) and I3 (unknown is a value), which
the rest of the pipeline relies on rather than re-checking.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.evidence import (
    Claim,
    Classification,
    Effect,
    EvidenceBundle,
    EvidenceRecord,
    Extracted,
    NumberRef,
    Outcome,
    Span,
    Violation,
    make_source_key,
    normalise_identifier,
    parse_source_key,
)

DOCUMENT = (
    "Time-restricted eating and cardiometabolic risk\n\n"
    "METHODS: We randomised 412 adults aged 40 to 70 to an eight-hour eating window.\n\n"
    "RESULTS: Fasting glucose fell by 4.2 mg/dL (95% CI 2.1 to 6.3) over 12 weeks."
)


def _span_for(quote: str) -> Span:
    span = Span.locate(DOCUMENT, quote)
    assert span is not None, f"fixture quote not present: {quote!r}"
    return span


# -- source identity ---------------------------------------------------


@pytest.mark.parametrize(
    ("scheme", "raw", "expected"),
    [
        ("doi", "https://doi.org/10.1001/JAMA.2024.1234", "10.1001/jama.2024.1234"),
        ("doi", "doi:10.1001/jama.2024.1234", "10.1001/jama.2024.1234"),
        ("doi", "10.1001/jama.2024.1234.", "10.1001/jama.2024.1234"),
        ("pmid", " 38412345 ", "38412345"),
        ("pmcid", "pmc10123456", "PMC10123456"),
        ("nct", "nct 01234567", "NCT01234567"),
    ],
)
def test_identifiers_collapse_to_one_canonical_spelling(scheme: str, raw: str, expected: str) -> None:
    assert normalise_identifier(scheme, raw) == expected


def test_source_keys_round_trip() -> None:
    key = make_source_key("doi", "https://doi.org/10.1001/X")
    assert key == "doi:10.1001/x"
    assert parse_source_key(key) == ("doi", "10.1001/x")


def test_unknown_scheme_and_empty_identifier_are_rejected() -> None:
    with pytest.raises(ValueError):
        make_source_key("isbn", "123")
    with pytest.raises(ValueError):
        make_source_key("doi", "   ")
    with pytest.raises(ValueError):
        parse_source_key("not-a-key")


# -- spans -------------------------------------------------------------


def test_span_verifies_against_the_document_it_indexes() -> None:
    span = _span_for("412 adults")

    assert span.verify(DOCUMENT) is True
    assert span.verify(DOCUMENT.replace("412", "999")) is False


def test_span_rejects_offsets_that_do_not_match_their_quote() -> None:
    assert Span(quote="412 adults", start=0, end=10).verify(DOCUMENT) is False
    assert Span(quote="412 adults", start=0, end=3).verify(DOCUMENT) is False
    assert Span(quote="", start=0, end=0).verify(DOCUMENT) is False


def test_locate_returns_none_for_text_that_is_not_present() -> None:
    assert Span.locate(DOCUMENT, "1,200 adults") is None
    assert Span.locate(DOCUMENT, "") is None


# -- extracted values --------------------------------------------------


def test_extracted_without_a_span_is_demoted_rather_than_trusted() -> None:
    """A value with no anchor is the signature of an invented number (I2)."""

    orphan = Extracted(value=412, status="extracted", span=None)

    assert orphan.status == "not_extractable"
    assert orphan.value is None
    assert orphan.is_known is False


def test_absent_statuses_never_carry_a_value() -> None:
    smuggled = Extracted(value="90% of people", status="not_reported")

    assert smuggled.value is None
    assert smuggled.is_known is False


def test_verify_demotes_a_value_whose_document_changed() -> None:
    anchored = Extracted.found(412, _span_for("412 adults"))

    assert anchored.verify(DOCUMENT).is_known is True
    assert anchored.verify("a completely different abstract").status == "not_extractable"


def test_extracted_round_trips_through_storage() -> None:
    original = Extracted.found("adults aged 40 to 70", _span_for("adults aged 40 to 70"))

    restored: Extracted[str] = Extracted.from_document(original.to_document())

    assert restored == original
    assert Extracted.from_document(Extracted.not_reported().to_document()).status == "not_reported"
    assert Extracted.from_document("nonsense").status == "not_extractable"


def test_stored_extracted_value_without_a_span_loads_as_unknown() -> None:
    """Hand-edited or corrupted rows must not become facts on load."""

    restored: Extracted[int] = Extracted.from_document({"status": "extracted", "value": 412})

    assert restored.status == "not_extractable"


# -- records -----------------------------------------------------------


def _record() -> EvidenceRecord:
    record = EvidenceRecord(
        source_key="doi:10.1001/jama.2024.1234",
        title="Time-restricted eating and cardiometabolic risk",
        document_text=DOCUMENT,
        publication_types=["Randomized Controlled Trial"],
        mesh_terms=["Humans"],
        classification=Classification(design="rct", subject="human", basis=("pt:rct",)),
        sample_size=Extracted.found(412, _span_for("412 adults")),
        population=Extracted.found("adults aged 40 to 70", _span_for("adults aged 40 to 70")),
        limitations=Extracted.not_reported(),
        state="approved",
        retrieved_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )
    record.outcomes = [
        Outcome(
            name="fasting glucose",
            direction=Extracted.found("fell", _span_for("fell")),
            is_surrogate=Extracted.found(True, _span_for("Fasting glucose")),
            effect=Effect(magnitude=Extracted.found(4.2, _span_for("4.2 mg/dL"))),
        )
    ]
    return record


def test_record_round_trips_through_storage() -> None:
    original = _record()

    restored = EvidenceRecord.from_document(original.to_document())

    assert restored.source_key == original.source_key
    assert restored.sample_size.value == 412
    assert restored.sample_size.span == original.sample_size.span
    assert restored.limitations.status == "not_reported"
    assert restored.outcomes[0].effect.magnitude.value == pytest.approx(4.2)
    assert restored.classification.design == "rct"
    assert restored.retrieved_at == original.retrieved_at


def test_verified_demotes_every_field_whose_span_no_longer_holds() -> None:
    record = _record()
    record.document_text = "An unrelated abstract about something else entirely."

    checked = record.verified()

    assert checked.sample_size.status == "not_extractable"
    assert checked.population.status == "not_extractable"
    assert checked.outcomes[0].effect.magnitude.status == "not_extractable"
    # An honest "not reported" is not a broken span and survives.
    assert checked.limitations.status == "not_reported"


def test_unverified_spans_reports_the_damage() -> None:
    record = _record()

    assert record.unverified_spans() == []

    record.document_text = DOCUMENT.replace("412", "999")
    assert record.unverified_spans() != []


def test_retraction_states_that_block_publication() -> None:
    record = _record()

    assert record.is_retracted is False
    for state in ("retracted", "concern"):
        record.retraction_state = state
        assert record.is_retracted is True

    # A correction is not a block; the notice travels with the citation instead.
    record.retraction_state = "corrected"
    assert record.is_retracted is False


# -- bundles -----------------------------------------------------------


def test_bundle_collects_cited_keys_in_first_seen_order() -> None:
    bundle = EvidenceBundle(
        bundle_id="b1",
        claims=[
            Claim(text="First", evidence_keys=["doi:a", "doi:b"]),
            Claim(text="Second", evidence_keys=["doi:b", "doi:c"]),
        ],
    )

    assert bundle.source_keys() == ["doi:a", "doi:b", "doi:c"]


def test_insufficient_bundles_never_publish() -> None:
    bundle = EvidenceBundle(bundle_id="b1", review_status="approved", grade="insufficient")

    assert bundle.is_publishable is False

    bundle.grade = "preliminary"
    assert bundle.is_publishable is True

    bundle.review_status = "rejected"
    assert bundle.is_publishable is False


def test_bundle_round_trips_with_claims_numbers_and_violations() -> None:
    span = _span_for("4.2 mg/dL")
    bundle = EvidenceBundle(
        bundle_id="b1",
        topic_key="time-restricted-eating|glucose",
        grade="moderate",
        review_status="approved",
        claims=[
            Claim(
                text="Fasting glucose fell by 4.2 mg/dL.",
                claim_type="causal",
                evidence_keys=["doi:10.1001/jama.2024.1234"],
                numbers=[
                    NumberRef(text="4.2", source_key="doi:10.1001/jama.2024.1234", span=span)
                ],
            )
        ],
        violations=[Violation(gate="G5", detail="surrogate endpoint")],
    )

    restored = EvidenceBundle.from_document(bundle.to_document())

    assert restored.claims[0].numbers[0].span == span
    assert restored.claims[0].claim_type == "causal"
    assert restored.violations[0].gate == "G5"
    assert restored.grade == "moderate"


def test_malformed_number_references_are_dropped_on_load() -> None:
    restored = EvidenceBundle.from_document(
        {
            "bundle_id": "b1",
            "claims": [{"text": "x", "numbers": [{"text": "4.2", "source_key": "doi:a"}]}],
        }
    )

    assert restored.claims[0].numbers == []


# -- grade clamping (I4) -----------------------------------------------


@pytest.mark.parametrize(
    ("proposed", "computed", "expected"),
    [
        ("high", "preliminary", "preliminary"),   # a model may never raise a grade
        ("high", "high", "high"),
        ("low", "moderate", "low"),               # but it may argue one down
        ("insufficient", "high", "insufficient"),
    ],
)
def test_a_model_can_lower_a_grade_but_never_raise_one(
    proposed: str, computed: str, expected: str
) -> None:
    from app.models.evidence import clamp_grade

    assert clamp_grade(proposed, computed) == expected


def test_an_unrecognised_grade_never_raises_anything() -> None:
    """A hallucinated grade name must not become an upgrade path."""

    from app.models.evidence import clamp_grade

    assert clamp_grade("excellent", "low") == "low"
    assert clamp_grade("high", "not-a-grade") == "insufficient"
