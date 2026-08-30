"""The evidence pipeline: one research path, two content products.

    acquire -> extract -> cluster -> rank -> synthesise -> review -> write -> edit -> check -> publish

Articles and tips run the same first six stages and diverge only at the writer, which is
the point of improvements.md item 5: they cannot disagree about a finding because they are
reading the same reviewed bundle.

Every stage that can fail has a named outcome rather than a boolean, and every one of them
ends in "publish nothing" (invariant I5). The pipeline is deliberately willing to do a
great deal of work and then publish nothing at all — that is what fail-closed costs, and
it is cheaper than the alternative.

Enabled by ``LIVEON_EVIDENCE_PIPELINE``; until that is set the existing prose path runs
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
from typing import Any, Callable, Mapping, Protocol, Sequence

from app.models.evidence import EvidenceBundle, EvidenceRecord, ReviewDecision, Violation
from app.models.run_outcome import RunOutcome
from app.models.summarizer import ArticleDraft
from app.models.tip import TipDraft
from app.services.evidence.clustering import cluster_records
from app.services.evidence.postedit import feedback_for, recheck_published_text
from app.services.evidence.ranking import RankedCluster, rank_clusters
from app.services.evidence.runlog import RunLog
from app.services.evidence.reviewer import EvidenceReviewer, max_regenerations
from app.services.evidence.store import EvidenceStore
from app.services.evidence.synthesizer import SynthesizerAgent
from app.services.evidence.writers import ArticleWriter, TipWriter
from app.services.research.http import ResearchRequestError

LOGGER = logging.getLogger(__name__)

__all__ = [
    "EvidencePipeline",
    "EvidencePipelineResult",
    "evidence_pipeline_enabled",
]


def evidence_pipeline_enabled() -> bool:
    """Whether the evidence path replaces the legacy prose path."""

    raw = (os.getenv("LIVEON_EVIDENCE_PIPELINE") or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


class SupportsAcquisition(Protocol):
    """Fetches records for a set of queries. Normally the PubMed client."""

    def search_records(self, query: str, *, max_results: int = 20) -> list[EvidenceRecord]:
        ...


class SupportsExtraction(Protocol):
    def extract(self, record: EvidenceRecord, *, force: bool = False) -> EvidenceRecord:
        ...


@dataclass(slots=True)
class EvidencePipelineResult:
    """What one run did, and why it stopped where it did."""

    outcome: RunOutcome
    run_id: str | None = None
    bundle: EvidenceBundle | None = None
    decision: ReviewDecision | None = None
    draft: Any = None
    acquired: int = 0
    considered: int = 0
    attempts: int = 0
    violations: list[Violation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def published(self) -> bool:
        return self.outcome is RunOutcome.PUBLISHED


@dataclass(slots=True)
class EvidencePipeline:
    """Coordinate acquisition through publication for one content product."""

    store: EvidenceStore
    acquirer: SupportsAcquisition | None
    extractor: SupportsExtraction
    synthesizer: SynthesizerAgent
    reviewer: EvidenceReviewer
    queries: Sequence[str] = ()
    max_results_per_query: int = 10
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    #: Optional. Without one the pipeline behaves identically but leaves no diary.
    runlog: RunLog | None = None
    run_id: str | None = None

    # -- stages --------------------------------------------------------

    def acquire(self) -> int:
        """Fetch new records into the store. Returns how many are newly known.

        A record already in the store is not re-fetched or re-stored: acquisition is
        additive, and the alias table is what makes "already known" a lookup rather than a
        guess.
        """

        if self.acquirer is None or not self.queries:
            return 0

        added = 0
        for query in self.queries:
            for record in self.acquirer.search_records(
                query, max_results=self.max_results_per_query
            ):
                if self.store.resolve(record.source_key) is not None:
                    continue
                self.store.upsert_record(record)
                added += 1

        LOGGER.info(
            "Acquired %s new record(s)",
            added,
            extra={"event": "evidence_pipeline.acquired", "records": added},
        )
        return added

    def extract_pending(self, *, limit: int = 20) -> int:
        """Extract every acquired record, promoting each to ``approved``.

        Approval here is the deterministic layers' verdict on the *record* — it was
        acquired from a primary source, classified from indexed metadata, and its extracted
        fields are span-anchored. Whether anything may be *said* with it is the reviewer's
        question, one stage later.
        """

        pending = self.store.records_in_state("acquired", limit=limit)
        for record in pending:
            extracted = self.extractor.extract(record)
            extracted.state = "approved"
            self.store.upsert_record(extracted)

        if pending:
            LOGGER.info(
                "Extracted %s record(s)",
                len(pending),
                extra={"event": "evidence_pipeline.extracted", "records": len(pending)},
            )
        return len(pending)

    def candidates(self, *, limit: int = 200) -> list[RankedCluster]:
        """Cluster and rank the approved evidence, best topic first."""

        approved = self.store.records_in_state("approved", limit=limit)
        clusters = cluster_records(approved)
        used = frozenset(
            key
            for cluster in clusters
            for key in (record.source_key for record in cluster.records)
            if self.store.usage_for_source(key)
        )
        return rank_clusters(
            clusters,
            last_used_at=self.store.last_used_at,
            used_source_keys=used,
            now=self.now(),
        )

    # -- the run -------------------------------------------------------

    def run(
        self,
        *,
        write: Callable[[EvidenceBundle, Mapping[str, EvidenceRecord]], Any],
        body_of: Callable[[Any], str],
        publish: Callable[[Any, EvidenceBundle, Mapping[str, EvidenceRecord]], Any],
        content_type: str,
        max_candidates: int = 3,
    ) -> EvidencePipelineResult:
        """Run the pipeline for one product.

        ``write`` and ``publish`` are supplied by the caller because an article and a tip
        differ only there; everything before is shared.
        """

        notes: list[str] = []
        run_id = self._begin(content_type)

        try:
            acquired = self.acquire()
        except ResearchRequestError as exc:
            LOGGER.warning(
                "Acquisition failed: %s",
                exc,
                extra={"event": "evidence_pipeline.acquire_failed"},
            )
            return self._finish(
                EvidencePipelineResult(
                    outcome=RunOutcome.RETRIEVAL_FAILED, run_id=run_id, notes=[str(exc)]
                )
            )

        try:
            self.extract_pending()
        except Exception as exc:  # noqa: BLE001 - a failed extraction is a failed run
            LOGGER.exception(
                "Extraction failed", extra={"event": "evidence_pipeline.extract_failed"}
            )
            return self._finish(
                EvidencePipelineResult(
                    outcome=RunOutcome.MODEL_FAILED,
                    run_id=run_id,
                    acquired=acquired,
                    notes=[str(exc)],
                )
            )

        ranked = self.candidates()
        self._event(
            "rank",
            "candidates",
            [
                {
                    "topic": candidate.cluster.key,
                    "topic_key": candidate.topic_key,
                    "grade": candidate.grade,
                    "score": round(candidate.score, 3),
                    "components": {
                        name: round(value, 3)
                        for name, value in candidate.components.items()
                    },
                    "sources": [record.source_key for record in candidate.cluster.records],
                }
                for candidate in ranked[:10]
            ],
        )

        if not ranked:
            return self._finish(
                EvidencePipelineResult(
                    outcome=RunOutcome.NO_NEW_EVIDENCE,
                    run_id=run_id,
                    acquired=acquired,
                    notes=["No approved evidence to write about."],
                )
            )

        last_result: EvidencePipelineResult | None = None

        # Work down the ranking: a topic refused for repetition or thin evidence should
        # not end the run while better-supported candidates are still waiting.
        for candidate in ranked[: max(1, max_candidates)]:
            result = self._attempt(
                candidate,
                write=write,
                body_of=body_of,
                publish=publish,
                content_type=content_type,
            )
            result.acquired = acquired
            result.considered = len(ranked)
            result.run_id = run_id
            result.notes = [*notes, *result.notes]

            if result.outcome is RunOutcome.PUBLISHED:
                return self._finish(result)
            if result.outcome in (RunOutcome.MODEL_FAILED, RunOutcome.SOURCE_UNAVAILABLE):
                return self._finish(result)
            last_result = result

        return self._finish(
            last_result
            or EvidencePipelineResult(
                outcome=RunOutcome.NO_NEW_EVIDENCE, run_id=run_id, acquired=acquired
            )
        )

    def _attempt(
        self,
        candidate: RankedCluster,
        *,
        write: Callable[[EvidenceBundle, Mapping[str, EvidenceRecord]], Any],
        body_of: Callable[[Any], str],
        publish: Callable[[Any, EvidenceBundle, Mapping[str, EvidenceRecord]], Any],
        content_type: str,
    ) -> EvidencePipelineResult:
        cluster = candidate.cluster
        records = {record.source_key: record for record in cluster.records}

        try:
            bundle = self.synthesizer.synthesize(cluster.records, topic_key=cluster.topic_key)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception(
                "Synthesis failed", extra={"event": "evidence_pipeline.synthesis_failed"}
            )
            return EvidencePipelineResult(outcome=RunOutcome.MODEL_FAILED, notes=[str(exc)])

        if not bundle.claims:
            return EvidencePipelineResult(
                outcome=RunOutcome.EVIDENCE_INSUFFICIENT,
                bundle=bundle,
                notes=["Synthesis produced no claims."],
            )

        decision = self.reviewer.review(
            bundle, records, last_used_at=self.store.last_used_at(cluster.topic_key)
        )
        self.store.save_bundle(bundle)
        self._event(
            "review",
            "decision",
            {
                "bundle_id": bundle.bundle_id,
                "topic_key": bundle.topic_key,
                "status": decision.status,
                "grade": decision.grade,
                "rationale": decision.rationale,
                "violations": [violation.to_document() for violation in decision.violations],
                "model_id": decision.model_id,
                "prompt_version": decision.prompt_version,
            },
        )

        if not decision.is_approved:
            return EvidencePipelineResult(
                outcome=(
                    RunOutcome.REVIEW_REJECTED
                    if decision.status in ("rejected", "regenerate")
                    else RunOutcome.EVIDENCE_INSUFFICIENT
                ),
                bundle=bundle,
                decision=decision,
                violations=list(decision.violations),
                notes=[decision.notes] if decision.notes else [],
            )

        return self._write_and_publish(
            bundle,
            records,
            write=write,
            body_of=body_of,
            publish=publish,
            content_type=content_type,
            decision=decision,
        )

    def _write_and_publish(
        self,
        bundle: EvidenceBundle,
        records: Mapping[str, EvidenceRecord],
        *,
        write: Callable[[EvidenceBundle, Mapping[str, EvidenceRecord]], Any],
        body_of: Callable[[Any], str],
        publish: Callable[[Any, EvidenceBundle, Mapping[str, EvidenceRecord]], Any],
        content_type: str,
        decision: ReviewDecision,
    ) -> EvidencePipelineResult:
        attempts = 0
        violations: list[Violation] = []
        draft: Any = None

        for attempt in range(1, max_regenerations() + 2):
            attempts = attempt
            try:
                draft = write(bundle, records)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception(
                    "Writing failed", extra={"event": "evidence_pipeline.write_failed"}
                )
                return EvidencePipelineResult(
                    outcome=RunOutcome.MODEL_FAILED,
                    bundle=bundle,
                    decision=decision,
                    attempts=attempt,
                    notes=[str(exc)],
                )

            violations = recheck_published_text(body_of(draft), bundle, records)
            self._event(
                "write",
                "recheck",
                {
                    "attempt": attempt,
                    "passed": not violations,
                    "violations": [violation.to_document() for violation in violations],
                },
            )
            if not violations:
                break

            LOGGER.info(
                "Draft failed the post-edit re-check on attempt %s",
                attempt,
                extra={
                    "event": "evidence_pipeline.recheck_failed",
                    "bundle_id": bundle.bundle_id,
                    "attempt": attempt,
                },
            )

        if violations:
            # The evidence was fine; the prose kept drifting off it. Nothing publishes.
            return EvidencePipelineResult(
                outcome=RunOutcome.REVIEW_REJECTED,
                bundle=bundle,
                decision=decision,
                draft=draft,
                attempts=attempts,
                violations=violations,
                notes=[feedback_for(violations)],
            )

        try:
            published = publish(draft, bundle, records)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception(
                "Publishing failed", extra={"event": "evidence_pipeline.publish_failed"}
            )
            return EvidencePipelineResult(
                outcome=RunOutcome.SOURCE_UNAVAILABLE,
                bundle=bundle,
                decision=decision,
                draft=draft,
                attempts=attempts,
                notes=[str(exc)],
            )

        content_id = str(getattr(published, "id", None) or getattr(published, "slug", "") or "")
        self.store.record_usage(
            source_keys=bundle.source_keys(),
            content_type=content_type,
            content_id=content_id,
            bundle_id=bundle.bundle_id,
            topic_key=bundle.topic_key,
            used_at=self.now(),
        )

        self._event(
            "publish",
            "stored",
            {
                "content_type": content_type,
                "content_id": content_id,
                "bundle_id": bundle.bundle_id,
                "grade": bundle.grade,
                "sources": bundle.source_keys(),
            },
        )

        LOGGER.info(
            "Published %s from bundle %s (%s)",
            content_type,
            bundle.bundle_id,
            bundle.grade,
            extra={
                "event": "evidence_pipeline.published",
                "content_type": content_type,
                "bundle_id": bundle.bundle_id,
                "grade": bundle.grade,
            },
        )
        return EvidencePipelineResult(
            outcome=RunOutcome.PUBLISHED,
            bundle=bundle,
            decision=decision,
            draft=draft,
            attempts=attempts,
        )


    # -- run log -------------------------------------------------------

    def _begin(self, job: str) -> str | None:
        """Open a run in the log, if one is configured."""

        if self.runlog is None:
            return None
        self.run_id = self.runlog.start(job, now=self.now())
        self._event("acquire", "started", {"queries": list(self.queries)})
        return self.run_id

    def _event(self, stage: str, event: str, data: Any = None) -> None:
        if self.runlog is not None and self.run_id:
            self.runlog.event(self.run_id, stage, event, data, now=self.now())

    def _finish(self, result: "EvidencePipelineResult") -> "EvidencePipelineResult":
        """Close the run, recording what it concluded and what produced it."""

        if self.runlog is not None and self.run_id:
            self.runlog.finish(
                self.run_id,
                result.outcome.value,
                model_id=self.reviewer.model_id,
                prompt_versions={
                    "extraction": getattr(self.extractor, "prompt_version", ""),
                    "synthesis": getattr(self.synthesizer, "prompt_version", ""),
                    "review": getattr(self.reviewer, "prompt_version", ""),
                },
                data={
                    "acquired": result.acquired,
                    "considered": result.considered,
                    "attempts": result.attempts,
                    "bundle_id": result.bundle.bundle_id if result.bundle else None,
                    "grade": result.bundle.grade if result.bundle else None,
                    "notes": result.notes,
                },
                now=self.now(),
            )
        return result


# ----------------------------------------------------------------------
# Product wiring
# ----------------------------------------------------------------------


def run_article(
    pipeline: EvidencePipeline,
    writer: ArticleWriter,
    publish: Callable[[ArticleDraft, EvidenceBundle, Mapping[str, EvidenceRecord]], Any],
) -> EvidencePipelineResult:
    """Run the pipeline for an article."""

    return pipeline.run(
        write=writer.write,
        body_of=lambda draft: f"{draft.title}\n\n{draft.summary}\n\n{draft.body}",
        publish=publish,
        content_type="article",
    )


def run_tip(
    pipeline: EvidencePipeline,
    writer: TipWriter,
    publish: Callable[[TipDraft, EvidenceBundle, Mapping[str, EvidenceRecord]], Any],
) -> EvidencePipelineResult:
    """Run the pipeline for a tip."""

    return pipeline.run(
        write=writer.write,
        body_of=lambda draft: f"{draft.title}\n\n{draft.body}",
        publish=publish,
        content_type="tip",
    )
