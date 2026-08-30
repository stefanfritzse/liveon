"""Group approved records into the topics an article can be written about.

The old article pipeline took the newest unpublished headline and summarised it alone.
That is why it could never say "previous studies agree" or "this contradicts last year's
trial" — it only ever held one paper at a time.

Clustering is by *intervention*, because that is the question a reader has: what does
time-restricted eating do? A cluster therefore gathers everything acquired about one
intervention, whatever endpoint each study measured, and the synthesizer decides what can
honestly be said across them.

Records with no extracted intervention fall back to the significant words of their title.
That is deliberately weaker — it groups less — which is the safe direction: a cluster that
splits produces two narrower articles, while a cluster that over-merges produces one
article claiming two unrelated things.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
import re
from typing import Iterable, Sequence

from app.models.evidence import EvidenceRecord
from app.services.evidence.synthesizer import topic_key_for

LOGGER = logging.getLogger(__name__)

__all__ = ["RecordCluster", "cluster_records", "max_cluster_sources"]

_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: Words that carry no topic meaning in a study title.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "association", "associations", "be", "by",
        "does", "effect", "effects", "for", "from", "impact", "in", "is", "of", "on",
        "or", "randomised", "randomized", "review", "risk", "role", "studies", "study",
        "the", "to", "trial", "with", "among", "versus", "vs", "analysis", "systematic",
    }
)


def max_cluster_sources() -> int:
    """How many sources one bundle may carry.

    A bound is needed because the synthesis prompt grows with the cluster and the model
    is local. Ordering decides which sources survive the cut, so the cap costs breadth
    rather than quality.
    """

    raw = (os.getenv("LIVEON_MAX_CLUSTER_SOURCES") or "").strip()
    try:
        return max(1, int(raw)) if raw else 5
    except ValueError:
        LOGGER.warning("Ignoring invalid LIVEON_MAX_CLUSTER_SOURCES=%r", raw)
        return 5


@dataclass(slots=True)
class RecordCluster:
    """Records about one intervention, ready to be synthesised together."""

    key: str
    records: list[EvidenceRecord] = field(default_factory=list)

    @property
    def topic_key(self) -> str:
        """The key used for the repetition window; includes the outcome."""

        return topic_key_for(self.records)

    @property
    def size(self) -> int:
        return len(self.records)

    def newest_published_at(self):
        """The most recent source publication date in the cluster, if any is known."""

        dates = [
            record.source_published_at
            for record in self.records
            if record.source_published_at is not None
        ]
        return max(dates) if dates else None


def cluster_records(
    records: Iterable[EvidenceRecord],
    *,
    max_sources: int | None = None,
) -> list[RecordCluster]:
    """Group ``records`` by intervention, strongest evidence first within each cluster.

    Ordering inside a cluster matters twice over: it decides which sources survive the
    size cap, and it decides which handle the synthesizer sees first.
    """

    limit = max_sources if max_sources is not None else max_cluster_sources()
    grouped: dict[str, list[EvidenceRecord]] = {}

    for record in records:
        key = cluster_key(record)
        grouped.setdefault(key, []).append(record)

    clusters = [
        RecordCluster(key=key, records=_ordered(members)[:limit])
        for key, members in grouped.items()
    ]
    # Deterministic order so two runs over the same store agree.
    clusters.sort(key=lambda cluster: cluster.key)

    LOGGER.info(
        "Clustered %s record(s) into %s topic(s)",
        sum(cluster.size for cluster in clusters),
        len(clusters),
        extra={"event": "evidence.clustered", "clusters": len(clusters)},
    )
    return clusters


def cluster_key(record: EvidenceRecord) -> str:
    """The intervention this record is about, normalised."""

    if record.intervention.is_known and record.intervention.value:
        slug = _slug(str(record.intervention.value))
        if slug:
            return slug

    return _title_slug(record.title) or record.source_key


def _ordered(records: Sequence[EvidenceRecord]) -> list[EvidenceRecord]:
    """Strongest and newest first, with a stable tiebreak."""

    return sorted(
        records,
        key=lambda record: (
            -_design_weight(record),
            -(record.source_published_at.timestamp() if record.source_published_at else 0.0),
            record.source_key,
        ),
    )


#: How much each design contributes to a record's position in its cluster. This is an
#: ordering heuristic, not a grade: the rubric in grading.py remains the only thing that
#: decides how strongly a finding may be stated.
_DESIGN_WEIGHT = {
    "meta_analysis": 6,
    "systematic_review": 5,
    "rct": 4,
    "non_randomised_trial": 3,
    "prospective_cohort": 3,
    "retrospective_cohort": 2,
    "case_control": 2,
    "cross_sectional": 1,
    "case_report": 1,
    "narrative_review": 1,
    "preclinical": 0,
    "unknown": 0,
}


def _design_weight(record: EvidenceRecord) -> int:
    weight = _DESIGN_WEIGHT.get(record.classification.design, 0)
    return weight + (1 if record.classification.is_human else 0)


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return "-".join(part for part in slug.split("-") if part)[:80]


def _title_slug(title: str) -> str:
    """A fallback key from the significant words of a title."""

    words = [
        word
        for word in _SLUG_RE.sub(" ", (title or "").lower()).split()
        if word and word not in _STOPWORDS and len(word) > 2
    ]
    return "-".join(sorted(words)[:3])
