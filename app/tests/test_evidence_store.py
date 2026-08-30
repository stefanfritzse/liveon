"""Tests for the research knowledge store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.evidence import (
    Claim,
    Classification,
    EvidenceBundle,
    EvidenceRecord,
    Extracted,
    NumberRef,
    Span,
)
from app.services.evidence.store import EvidenceStore

DOCUMENT = "METHODS: We randomised 412 adults to an eight-hour eating window."


@pytest.fixture()
def store(tmp_path: Path) -> EvidenceStore:
    with EvidenceStore(tmp_path / "content.db") as opened:
        yield opened


def _span(quote: str, document: str = DOCUMENT) -> Span:
    span = Span.locate(document, quote)
    assert span is not None
    return span


def _record(**overrides) -> EvidenceRecord:
    defaults = dict(
        source_key="doi:10.1001/jama.2024.1234",
        title="Time-restricted eating",
        aliases=["doi:10.1001/jama.2024.1234", "pmid:38412345", "pmcid:PMC10123456"],
        document_text=DOCUMENT,
        classification=Classification(design="rct", subject="human"),
        sample_size=Extracted.found(412, _span("412 adults")),
        state="acquired",
        retrieved_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return EvidenceRecord(**defaults)


def test_record_round_trips_through_the_database(store: EvidenceStore) -> None:
    store.upsert_record(_record())

    loaded = store.get_record("doi:10.1001/jama.2024.1234")

    assert loaded is not None
    assert loaded.title == "Time-restricted eating"
    assert loaded.sample_size.value == 412
    assert loaded.classification.design == "rct"


def test_missing_records_are_absent_not_empty(store: EvidenceStore) -> None:
    assert store.get_record("doi:10.9999/nope") is None
    assert store.exists("doi:10.9999/nope") is False
    assert store.get_records(["doi:10.9999/nope"]) == {}


def test_every_alias_resolves_to_one_canonical_key(store: EvidenceStore) -> None:
    """A DOI, its PMID and its PMC ID are the same paper, so dedup is a key lookup."""

    store.upsert_record(_record())

    for alias in ("doi:10.1001/jama.2024.1234", "pmid:38412345", "pmcid:PMC10123456"):
        assert store.resolve(alias) == "doi:10.1001/jama.2024.1234"

    assert store.resolve("pmid:99999999") is None
    assert store.resolve("") is None


def test_a_second_sighting_updates_without_losing_the_first(store: EvidenceStore) -> None:
    store.upsert_record(_record())
    first_seen = store._conn.execute(
        "SELECT first_seen_at FROM evidence_sources;"
    ).fetchone()["first_seen_at"]

    store.upsert_record(_record(title="Time-restricted eating (revised)"))

    row = store._conn.execute("SELECT first_seen_at, title FROM evidence_sources;").fetchone()
    assert row["first_seen_at"] == first_seen
    assert row["title"] == "Time-restricted eating (revised)"


def test_a_metadata_refresh_cannot_blank_the_document_spans_point_into(
    store: EvidenceStore,
) -> None:
    """Losing document_text would invalidate every stored span silently."""

    store.upsert_record(_record())

    store.upsert_record(_record(document_text="", sample_size=Extracted.not_reported()))

    loaded = store.get_record("doi:10.1001/jama.2024.1234")
    assert loaded is not None
    assert loaded.document_text == DOCUMENT


def test_loading_re_verifies_spans_against_the_stored_document(store: EvidenceStore) -> None:
    """A value whose anchor no longer holds comes back as unknown, not as a fact."""

    store.upsert_record(_record())
    # Change the document only. The span still says "412 adults" and still points at the
    # same offsets, so the mismatch is exactly the corruption verification exists to catch.
    store._conn.execute(
        "UPDATE evidence_sources SET data = replace(data, 'randomised 412', 'randomised 999');"
    )
    store._conn.commit()

    loaded = store.get_record("doi:10.1001/jama.2024.1234")

    assert loaded is not None
    assert loaded.sample_size.status == "not_extractable"


def test_state_transitions_are_recorded(store: EvidenceStore) -> None:
    store.upsert_record(_record())

    store.set_state("doi:10.1001/jama.2024.1234", "approved")

    assert store.records_in_state("approved")[0].source_key == "doi:10.1001/jama.2024.1234"
    assert store.records_in_state("acquired") == []
    with pytest.raises(KeyError):
        store.set_state("doi:10.9999/nope", "approved")


def test_retraction_is_orthogonal_to_the_review_lifecycle(store: EvidenceStore) -> None:
    """A retracted paper stays 'approved' in lifecycle terms; G6 is what blocks it."""

    store.upsert_record(_record(state="approved"))

    store.set_retraction(
        "doi:10.1001/jama.2024.1234", "retracted", notes=["RetractionIn: JAMA 2026"]
    )

    loaded = store.get_record("doi:10.1001/jama.2024.1234")
    assert loaded is not None
    assert loaded.state == "approved"
    assert loaded.is_retracted is True
    assert loaded.retraction_notes == ["RetractionIn: JAMA 2026"]


def test_bundles_persist_with_their_claims_and_roles(store: EvidenceStore) -> None:
    store.upsert_record(_record(state="approved"))
    bundle = EvidenceBundle(
        bundle_id="bundle-1",
        topic_key="tre|glucose",
        grade="moderate",
        review_status="approved",
        claims=[
            Claim(
                text="Glucose fell by 4.2 mg/dL.",
                evidence_keys=["doi:10.1001/jama.2024.1234", "doi:10.1002/other"],
                numbers=[
                    NumberRef(
                        text="412",
                        source_key="doi:10.1001/jama.2024.1234",
                        span=_span("412 adults"),
                    )
                ],
            )
        ],
    )

    store.save_bundle(bundle, roles={"doi:10.1001/jama.2024.1234": "primary"})

    loaded = store.get_bundle("bundle-1")
    assert loaded is not None
    assert loaded.grade == "moderate"
    assert loaded.claims[0].numbers[0].text == "412"
    assert store.bundle_roles("bundle-1") == {
        "doi:10.1001/jama.2024.1234": "primary",
        "doi:10.1002/other": "supporting",
    }


def test_resaving_a_bundle_replaces_its_roles(store: EvidenceStore) -> None:
    bundle = EvidenceBundle(
        bundle_id="bundle-1", claims=[Claim(text="a", evidence_keys=["doi:a", "doi:b"])]
    )
    store.save_bundle(bundle)

    bundle.claims = [Claim(text="a", evidence_keys=["doi:a"])]
    store.save_bundle(bundle)

    assert set(store.bundle_roles("bundle-1")) == {"doi:a"}


def test_usage_links_published_content_back_to_its_sources(store: EvidenceStore) -> None:
    """This is what lets a retraction find the articles that relied on the paper."""

    store.record_usage(
        source_keys=["doi:a", "doi:b"],
        content_type="article",
        content_id="article-1",
        bundle_id="bundle-1",
        topic_key="tre|glucose",
        used_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )

    usage = store.usage_for_source("doi:a")

    assert len(usage) == 1
    assert usage[0]["content_id"] == "article-1"
    assert usage[0]["content_type"] == "article"


def test_usage_is_idempotent_per_piece_of_content(store: EvidenceStore) -> None:
    for _ in range(3):
        store.record_usage(
            source_keys=["doi:a"], content_type="tip", content_id="tip-1", topic_key="t"
        )

    assert len(store.usage_for_source("doi:a")) == 1


def test_last_used_at_drives_the_repetition_window(store: EvidenceStore) -> None:
    assert store.last_used_at("tre|glucose") is None

    older = datetime.now(timezone.utc) - timedelta(days=40)
    newer = datetime.now(timezone.utc) - timedelta(days=2)
    store.record_usage(
        source_keys=["doi:a"], content_type="tip", content_id="t1",
        topic_key="tre|glucose", used_at=older,
    )
    store.record_usage(
        source_keys=["doi:b"], content_type="tip", content_id="t2",
        topic_key="tre|glucose", used_at=newer,
    )

    latest = store.last_used_at("tre|glucose")
    assert latest is not None
    assert abs((latest - newer).total_seconds()) < 1


def test_a_record_without_a_key_is_refused(store: EvidenceStore) -> None:
    with pytest.raises(ValueError):
        store.upsert_record(EvidenceRecord(source_key=""))


def test_an_unknown_bundle_role_is_refused(store: EvidenceStore) -> None:
    """A typo must not become data: a lost "contradicting" role loses a disagreement."""

    bundle = EvidenceBundle(bundle_id="b1", claims=[Claim(text="a", evidence_keys=["doi:a"])])

    with pytest.raises(ValueError, match="Unknown bundle role"):
        store.save_bundle(bundle, roles={"doi:a": "primry"})
