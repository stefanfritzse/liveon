"""Tests for canonical topic naming, and the live-run regression behind it.

The headline test is `test_ten_real_trials_of_one_intervention_form_one_cluster`. It uses
MeSH terms captured from an actual PubMed run on 2026-08-30, during which the pipeline
turned ten randomised trials of time-restricted eating into ten separate topics and wrote
an article from a single paper. Nothing in the offline fixtures could catch that, because
every fixture used a hand-written, consistent intervention string.
"""

from __future__ import annotations

import pytest

from app.models.evidence import Classification, EvidenceRecord, Extracted, Outcome, Span
from app.services.evidence.clustering import cluster_key, cluster_records
from app.services.evidence.synthesizer import topic_key_for
from app.services.evidence.vocabulary import (
    CHECK_TAGS,
    INTERVENTION_TERMS,
    canonical_intervention,
    canonical_topic,
    fallback_topic,
    topical_terms,
)
from app.tests.fixtures.live_mesh import LIVE_TRE_RECORDS


def _record(
    key: str = "doi:10.1/x",
    *,
    mesh: list[str] | None = None,
    intervention: str | None = None,
    title: str = "A study of something",
    outcome: str | None = None,
) -> EvidenceRecord:
    document = f"We studied {intervention or 'something'} in adults."
    record = EvidenceRecord(
        source_key=key,
        title=title,
        document_text=document,
        mesh_terms=mesh or [],
        classification=Classification(design="rct", subject="human"),
    )
    if intervention:
        span = Span.locate(document, intervention)
        if span is not None:
            record.intervention = Extracted.found(intervention, span)
    if outcome:
        record.outcomes = [Outcome(name=outcome)]
    return record


# -- the live-run regression -------------------------------------------


def test_ten_real_trials_of_one_intervention_form_one_cluster() -> None:
    """The failure that made this module exist, asserted against the real metadata."""

    records = [
        _record(case["source_key"], mesh=case["mesh_terms"], intervention=case["intervention"])
        for case in LIVE_TRE_RECORDS
    ]

    clusters = cluster_records(records, max_sources=10)

    assert len(clusters) == 1
    assert clusters[0].key == "intermittent-fasting"
    assert clusters[0].size == 10


def test_the_prose_those_records_carried_would_still_split_them() -> None:
    """Proof the fix is the MeSH keying, not an accident of this data."""

    prose_keys = {
        case["intervention"] for case in LIVE_TRE_RECORDS if case["intervention"]
    }

    # Seven distinct phrasings for one intervention, plus three that failed to extract.
    assert len(prose_keys) > 5
    assert sum(1 for case in LIVE_TRE_RECORDS if case["intervention"] is None) == 3


def test_every_live_record_resolves_to_the_same_canonical_topic() -> None:
    topics = {canonical_intervention(case["mesh_terms"]) for case in LIVE_TRE_RECORDS}

    assert topics == {"intermittent-fasting"}


def test_the_topic_key_is_canonical_too() -> None:
    """G9 compares this against what was published recently, so it must be stable."""

    records = [
        _record(case["source_key"], mesh=case["mesh_terms"], outcome="body weight")
        for case in LIVE_TRE_RECORDS
    ]

    assert topic_key_for(records).startswith("intermittent-fasting|")


# -- the vocabulary ----------------------------------------------------


def test_check_tags_are_not_topics() -> None:
    """These appear on nearly every clinical paper; keying on them merges everything."""

    assert topical_terms(["Humans", "Female", "Middle Aged", "Intermittent Fasting"]) == [
        "intermittent fasting"
    ]
    assert canonical_intervention(["Humans", "Female", "Adult"]) is None


def test_the_most_specific_mapped_term_wins() -> None:
    """A paper indexed with both is about the narrower one."""

    assert canonical_intervention(["Fasting", "Intermittent Fasting"]) == "intermittent-fasting"
    assert canonical_intervention(["Fasting"]) == "fasting"


@pytest.mark.parametrize(
    ("mesh", "expected"),
    [
        (["Resistance Training"], "resistance-training"),
        (["Exercise"], "exercise"),
        (["Sleep Duration"], "sleep"),
        (["Circadian Rhythm"], "circadian-rhythm"),
        (["Caloric Restriction"], "caloric-restriction"),
        (["Diet, Mediterranean"], "mediterranean-diet"),
        (["Sirolimus"], "rapamycin"),
    ],
)
def test_vocabulary_entries_map_to_their_canonical_topic(
    mesh: list[str], expected: str
) -> None:
    assert canonical_intervention(mesh) == expected


def test_synonyms_collapse_to_one_topic() -> None:
    """Two descriptors for one idea must not become two topics."""

    assert canonical_intervention(["Meditation"]) == canonical_intervention(["Mindfulness"])
    assert canonical_intervention(["Loneliness"]) == canonical_intervention(["Social Support"])


def test_the_vocabulary_is_ordered_specific_first() -> None:
    """The ordering is load-bearing, so a careless insertion should fail here."""

    terms = [descriptor for descriptor, _ in INTERVENTION_TERMS]

    assert terms.index("intermittent fasting") < terms.index("fasting")
    assert terms.index("resistance training") < terms.index("exercise")
    assert terms.index("sleep duration") < terms.index("sleep")


def test_no_vocabulary_term_is_also_a_check_tag() -> None:
    """A term in both lists would be silently unreachable."""

    assert not {descriptor for descriptor, _ in INTERVENTION_TERMS} & CHECK_TAGS


# -- the fallback chain ------------------------------------------------


def test_an_unmapped_topic_still_gets_a_controlled_key() -> None:
    """Weaker than the vocabulary, but still a term an indexer chose."""

    assert canonical_intervention(["Telomere Shortening"]) is None
    assert fallback_topic(["Humans", "Telomere Shortening"]) == "telomere-shortening"


def test_a_record_with_no_usable_indexing_falls_back_to_prose() -> None:
    record = _record(mesh=["Humans", "Female"], intervention="daily cold plunges")

    assert cluster_key(record) == "daily-cold-plunges"


def test_a_record_with_nothing_at_all_falls_back_to_its_title() -> None:
    record = _record(mesh=[], title="Effects of Sauna Bathing on Mortality")

    assert cluster_key(record) == "bathing-mortality-sauna"


def test_the_prose_fallback_does_not_guess_at_a_common_stem() -> None:
    """Merging on prose is guesswork, and guessing wrong merges unrelated topics.

    Two phrasings of one intervention stay apart here, and that is accepted: the cost is
    two narrower articles, where over-merging would cost one article claiming two things.
    MeSH is what merges the real cases, and this path only runs when a record carries no
    usable indexing at all.
    """

    first = _record(intervention="cold plunge immersion at eleven degrees for three minutes")
    second = _record(intervention="cold plunge immersion at eleven degrees daily")

    assert cluster_key(first) != cluster_key(second)

    # The same two papers, indexed, land together.
    assert cluster_key(_record(mesh=["Cold Temperature"])) == cluster_key(
        _record(mesh=["Humans", "Cold Temperature"])
    )


def test_clustering_still_separates_genuinely_different_topics() -> None:
    """Fixing the over-splitting must not create over-merging."""

    fasting = _record("doi:1", mesh=["Humans", "Intermittent Fasting"])
    lifting = _record("doi:2", mesh=["Humans", "Resistance Training"])
    sleeping = _record("doi:3", mesh=["Humans", "Sleep Duration"])

    clusters = cluster_records([fasting, lifting, sleeping])

    assert {cluster.key for cluster in clusters} == {
        "intermittent-fasting",
        "resistance-training",
        "sleep",
    }


def test_canonical_topic_can_refuse_to_guess() -> None:
    assert canonical_topic(["Humans", "Female"], fallback=False) is None
    assert canonical_topic(["Humans", "Female"]) is None
    assert canonical_topic([]) is None
