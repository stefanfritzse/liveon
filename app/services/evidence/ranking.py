"""Decide which topic to write about next.

The article pipeline used to take the newest unpublished item in feed order, which meant a
mouse study published six hours ago outranked a human meta-analysis published yesterday.
Recency is a signal, not the signal.

The score is an explicit weighted sum, in code, with every weight a module constant that
can be read, argued with, and tested:

    strength   how good the evidence is, from the same rubric that grades it
    novelty    how long since this topic was last published
    recency    how fresh the underlying research is, decaying by half-life
    priority   editorial importance of the subject area
    redundancy how much of this cluster has already been used  (subtracted)

The weights are not tuned against anything — there is no ground truth to tune them
against — so they encode a stated editorial position instead: evidence strength matters
roughly twice as much as recency, and a topic covered last week is worth very little
however strong it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from typing import Callable, Mapping, Sequence

from app.services.evidence.clustering import RecordCluster
from app.services.evidence.grading import provisional_grade

LOGGER = logging.getLogger(__name__)

__all__ = ["RankedCluster", "rank_clusters", "score_cluster"]

W_STRENGTH = 3.0
W_NOVELTY = 2.0
W_RECENCY = 1.5
W_PRIORITY = 1.0
W_REDUNDANCY = 2.0

#: How much each grade contributes, normalised to 0-1.
_GRADE_WEIGHT = {
    "high": 1.0,
    "moderate": 0.75,
    "low": 0.45,
    "preliminary": 0.2,
    "insufficient": 0.0,
}

_DEFAULT_HALFLIFE_DAYS = 21.0

#: A topic used this recently scores no novelty at all; G9 will refuse it anyway, but
#: ranking should not spend a synthesis call finding that out.
_NOVELTY_FLOOR_DAYS = 30.0


@dataclass(slots=True, frozen=True)
class RankedCluster:
    """A cluster with its score and the numbers behind it."""

    cluster: RecordCluster
    score: float
    grade: str
    components: Mapping[str, float]

    @property
    def topic_key(self) -> str:
        return self.cluster.topic_key


def rank_clusters(
    clusters: Sequence[RecordCluster],
    *,
    last_used_at: Callable[[str], datetime | None] | None = None,
    used_source_keys: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> list[RankedCluster]:
    """Return ``clusters`` best first.

    ``last_used_at`` is normally ``EvidenceStore.last_used_at``; passing it as a callable
    keeps this module free of storage concerns and trivially testable.
    """

    moment = now or datetime.now(timezone.utc)
    ranked = [
        score_cluster(
            cluster,
            last_used_at=last_used_at(cluster.topic_key) if last_used_at else None,
            used_source_keys=used_source_keys,
            now=moment,
        )
        for cluster in clusters
    ]

    # Ties break on grade, then on how many sources back the topic, then on the key, so
    # two runs over the same store produce the same order.
    ranked.sort(
        key=lambda item: (
            -item.score,
            -_GRADE_WEIGHT.get(item.grade, 0.0),
            -item.cluster.size,
            item.cluster.key,
        )
    )

    if ranked:
        LOGGER.info(
            "Ranked %s candidate topic(s); leader %r scored %.2f (%s)",
            len(ranked),
            ranked[0].cluster.key,
            ranked[0].score,
            ranked[0].grade,
            extra={"event": "evidence.ranked", "leader": ranked[0].cluster.key},
        )
    return ranked


def score_cluster(
    cluster: RecordCluster,
    *,
    last_used_at: datetime | None = None,
    used_source_keys: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> RankedCluster:
    """Score one cluster, keeping the components for the run log."""

    moment = now or datetime.now(timezone.utc)
    grade, _ = provisional_grade(cluster.records)

    strength = _GRADE_WEIGHT.get(grade, 0.0)
    novelty = _novelty(last_used_at, moment)
    recency = _recency(cluster, moment)
    priority = _priority(cluster)
    redundancy = _redundancy(cluster, used_source_keys)

    score = (
        W_STRENGTH * strength
        + W_NOVELTY * novelty
        + W_RECENCY * recency
        + W_PRIORITY * priority
        - W_REDUNDANCY * redundancy
    )

    return RankedCluster(
        cluster=cluster,
        score=score,
        grade=grade,
        components={
            "strength": strength,
            "novelty": novelty,
            "recency": recency,
            "priority": priority,
            "redundancy": redundancy,
        },
    )


def _novelty(last_used_at: datetime | None, now: datetime) -> float:
    """1.0 for a topic never covered, falling to 0 for one covered just now."""

    if last_used_at is None:
        return 1.0
    days = max(0.0, (now - last_used_at).total_seconds() / 86400.0)
    return min(1.0, days / _NOVELTY_FLOOR_DAYS)


def _recency(cluster: RecordCluster, now: datetime) -> float:
    """Exponential decay on the newest source, by half-life.

    Research does not stop being true, so this decays rather than cuts off: a five-year-old
    meta-analysis still scores, just less than this morning's.
    """

    newest = cluster.newest_published_at()
    if newest is None:
        return 0.0
    days = max(0.0, (now - newest).total_seconds() / 86400.0)
    return math.pow(0.5, days / _halflife_days())


def _halflife_days() -> float:
    raw = (os.getenv("LIVEON_RECENCY_HALFLIFE_DAYS") or "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_HALFLIFE_DAYS
    except ValueError:
        LOGGER.warning("Ignoring invalid LIVEON_RECENCY_HALFLIFE_DAYS=%r", raw)
        return _DEFAULT_HALFLIFE_DAYS
    return value if value > 0 else _DEFAULT_HALFLIFE_DAYS


def _priority(cluster: RecordCluster) -> float:
    """Editorial weighting, from ``LIVEON_TOPIC_PRIORITIES``.

    A JSON object mapping a substring to a weight, e.g. ``{"sleep": 1.0, "supplement": -0.5}``.
    Matching is by substring on the cluster key so operators do not have to guess the
    exact slug an extraction produced.
    """

    priorities = _topic_priorities()
    if not priorities:
        return 0.0

    key = cluster.key.lower()
    matched = [weight for term, weight in priorities.items() if term and term in key]
    return max(matched) if matched else 0.0


def _topic_priorities() -> dict[str, float]:
    raw = (os.getenv("LIVEON_TOPIC_PRIORITIES") or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.warning("Ignoring invalid JSON in LIVEON_TOPIC_PRIORITIES")
        return {}
    if not isinstance(payload, dict):
        LOGGER.warning("LIVEON_TOPIC_PRIORITIES must be a JSON object")
        return {}

    weights: dict[str, float] = {}
    for term, weight in payload.items():
        try:
            weights[str(term).strip().lower()] = float(weight)
        except (TypeError, ValueError):
            continue
    return weights


def _redundancy(cluster: RecordCluster, used_source_keys: frozenset[str]) -> float:
    """The fraction of this cluster that has already been published elsewhere."""

    if not cluster.records or not used_source_keys:
        return 0.0
    used = sum(1 for record in cluster.records if record.source_key in used_source_keys)
    return used / len(cluster.records)
