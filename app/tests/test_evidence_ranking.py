"""Tests for clustering and ranking.

The headline assertion is the one improvements.md item 7 asks for: a mouse study published
six hours ago must not outrank a human meta-analysis published yesterday.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.evidence import (
    Classification,
    EvidenceRecord,
    Extracted,
    Outcome,
    Span,
)
from app.services.evidence.clustering import (
    RecordCluster,
    cluster_key,
    cluster_records,
    max_cluster_sources,
)
from app.services.evidence.ranking import (
    W_RECENCY,
    W_STRENGTH,
    rank_clusters,
    score_cluster,
)

NOW = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
DOCUMENT = "We studied 412 adults and recorded deaths over ten years."


def _span(quote: str) -> Span:
    span = Span.locate(DOCUMENT, quote)
    assert span is not None
    return span


def _record(
    key: str,
    *,
    design: str = "rct",
    subject: str = "human",
    intervention: str | None = "time-restricted eating",
    title: str = "A study",
    published: datetime | None = None,
    sample: int | None = 412,
    outcome: str = "mortality",
    surrogate: bool | None = False,
) -> EvidenceRecord:
    record = EvidenceRecord(
        source_key=key,
        title=title,
        document_text=DOCUMENT,
        classification=Classification(design=design, subject=subject),
        source_published_at=published,
        state="approved",
    )
    if intervention is not None:
        record.intervention = Extracted.found(intervention, _span("412 adults"))
    if sample is not None:
        record.sample_size = Extracted.found(sample, _span("412 adults"))
    record.outcomes = [
        Outcome(
            name=outcome,
            is_surrogate=(
                Extracted.found(surrogate, _span("deaths"))
                if surrogate is not None
                else Extracted.not_extractable()
            ),
        )
    ]
    return record


# -- clustering --------------------------------------------------------


def test_records_about_one_intervention_cluster_together() -> None:
    records = [
        _record("doi:a", intervention="time-restricted eating"),
        _record("doi:b", intervention="Time-Restricted Eating"),
        _record("doi:c", intervention="resistance training"),
    ]

    clusters = cluster_records(records)

    assert len(clusters) == 2
    sizes = {cluster.key: cluster.size for cluster in clusters}
    assert sizes["time-restricted-eating"] == 2
    assert sizes["resistance-training"] == 1


def test_a_record_with_no_extracted_intervention_falls_back_to_its_title() -> None:
    """Weaker grouping is the safe direction: it splits rather than over-merges."""

    record = _record("doi:a", intervention=None, title="Effects of Sauna Bathing on Mortality")

    assert cluster_key(record) == "bathing-mortality-sauna"


def test_the_strongest_evidence_leads_its_cluster() -> None:
    records = [
        _record("doi:weak", design="cross_sectional"),
        _record("doi:strong", design="meta_analysis"),
        _record("doi:mid", design="rct"),
    ]

    cluster = cluster_records(records)[0]

    assert [record.source_key for record in cluster.records] == [
        "doi:strong",
        "doi:mid",
        "doi:weak",
    ]


def test_clusters_are_capped_so_prompts_stay_bounded() -> None:
    records = [_record(f"doi:{index}") for index in range(10)]

    cluster = cluster_records(records, max_sources=3)[0]

    assert cluster.size == 3


def test_the_cap_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert max_cluster_sources() == 5

    monkeypatch.setenv("LIVEON_MAX_CLUSTER_SOURCES", "2")
    assert max_cluster_sources() == 2

    monkeypatch.setenv("LIVEON_MAX_CLUSTER_SOURCES", "nonsense")
    assert max_cluster_sources() == 5


def test_clustering_is_deterministic() -> None:
    records = [_record("doi:a"), _record("doi:b", intervention="walking")]

    first = [cluster.key for cluster in cluster_records(records)]
    second = [cluster.key for cluster in cluster_records(list(reversed(records)))]

    assert first == second


def test_a_cluster_reports_its_topic_key_and_newest_source() -> None:
    older = _record("doi:a", published=NOW - timedelta(days=10))
    newer = _record("doi:b", published=NOW - timedelta(days=1))

    cluster = cluster_records([older, newer])[0]

    assert cluster.topic_key == "time-restricted-eating|mortality"
    assert cluster.newest_published_at() == NOW - timedelta(days=1)


def test_an_empty_input_produces_no_clusters() -> None:
    assert cluster_records([]) == []


# -- ranking -----------------------------------------------------------


def test_a_meta_analysis_outranks_a_newer_mouse_study() -> None:
    """The failure improvements.md item 7 names, asserted directly."""

    meta = RecordCluster(
        key="time-restricted-eating",
        records=[
            _record("doi:meta", design="meta_analysis", published=NOW - timedelta(days=1)),
            _record("doi:trial", design="rct", published=NOW - timedelta(days=40)),
        ],
    )
    mouse = RecordCluster(
        key="rapamycin",
        records=[
            _record(
                "doi:mouse",
                design="preclinical",
                subject="animal",
                sample=None,
                published=NOW - timedelta(hours=6),
            )
        ],
    )

    ranked = rank_clusters([mouse, meta], now=NOW)

    assert ranked[0].cluster is meta
    assert ranked[0].grade == "high"
    assert ranked[1].grade == "preliminary"


def test_strength_outweighs_recency_by_design() -> None:
    """Stated as a weight rather than left to emerge from the ordering."""

    assert W_STRENGTH > W_RECENCY


def test_a_recently_covered_topic_loses_its_novelty() -> None:
    cluster = RecordCluster(key="k", records=[_record("doi:a", published=NOW)])

    fresh = score_cluster(cluster, last_used_at=None, now=NOW)
    stale = score_cluster(cluster, last_used_at=NOW - timedelta(days=2), now=NOW)

    assert fresh.components["novelty"] == 1.0
    assert stale.components["novelty"] < 0.1
    assert fresh.score > stale.score


def test_novelty_returns_once_the_window_has_passed() -> None:
    cluster = RecordCluster(key="k", records=[_record("doi:a", published=NOW)])

    scored = score_cluster(cluster, last_used_at=NOW - timedelta(days=60), now=NOW)

    assert scored.components["novelty"] == 1.0


def test_recency_decays_by_half_life() -> None:
    recent = RecordCluster(key="k", records=[_record("doi:a", published=NOW)])
    older = RecordCluster(
        key="k", records=[_record("doi:a", published=NOW - timedelta(days=21))]
    )

    assert score_cluster(recent, now=NOW).components["recency"] == pytest.approx(1.0)
    assert score_cluster(older, now=NOW).components["recency"] == pytest.approx(0.5)


def test_an_undated_source_scores_no_recency_rather_than_full_marks() -> None:
    cluster = RecordCluster(key="k", records=[_record("doi:a", published=None)])

    assert score_cluster(cluster, now=NOW).components["recency"] == 0.0


def test_the_half_life_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_RECENCY_HALFLIFE_DAYS", "7")
    cluster = RecordCluster(
        key="k", records=[_record("doi:a", published=NOW - timedelta(days=7))]
    )

    assert score_cluster(cluster, now=NOW).components["recency"] == pytest.approx(0.5)


def test_already_published_sources_reduce_a_cluster_score() -> None:
    cluster = RecordCluster(
        key="k",
        records=[_record("doi:a", published=NOW), _record("doi:b", published=NOW)],
    )

    clean = score_cluster(cluster, now=NOW)
    reused = score_cluster(cluster, used_source_keys=frozenset({"doi:a"}), now=NOW)

    assert reused.components["redundancy"] == 0.5
    assert reused.score < clean.score


def test_topic_priorities_lift_a_subject_area(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_TOPIC_PRIORITIES", '{"sleep": 2.0}')
    sleep = RecordCluster(key="sleep-duration", records=[_record("doi:a", published=NOW)])
    other = RecordCluster(key="walking", records=[_record("doi:b", published=NOW)])

    ranked = rank_clusters([other, sleep], now=NOW)

    assert ranked[0].cluster is sleep
    assert ranked[0].components["priority"] == 2.0


def test_invalid_priority_configuration_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_TOPIC_PRIORITIES", "not json")
    cluster = RecordCluster(key="k", records=[_record("doi:a", published=NOW)])

    assert score_cluster(cluster, now=NOW).components["priority"] == 0.0


def test_ranking_reads_last_use_through_the_callable_it_is_given() -> None:
    asked: list[str] = []

    def _last_used(topic_key: str):
        asked.append(topic_key)
        return NOW - timedelta(days=1)

    cluster = RecordCluster(key="k", records=[_record("doi:a", published=NOW)])

    ranked = rank_clusters([cluster], last_used_at=_last_used, now=NOW)

    assert asked == [cluster.topic_key]
    assert ranked[0].components["novelty"] < 0.1


def test_ranking_is_stable_for_identical_candidates() -> None:
    first = RecordCluster(key="aaa", records=[_record("doi:a", published=NOW)])
    second = RecordCluster(key="bbb", records=[_record("doi:b", published=NOW)])

    assert [item.cluster.key for item in rank_clusters([second, first], now=NOW)] == [
        "aaa",
        "bbb",
    ]


def test_ranking_an_empty_field_returns_nothing() -> None:
    assert rank_clusters([], now=NOW) == []
