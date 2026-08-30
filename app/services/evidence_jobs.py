"""Build and run the evidence pipeline as a scheduled job.

This is the seam between the evidence layer and the rest of the application: it reads the
environment, constructs the agents, and adapts the drafts the writers produce to the
publishers that already exist. Everything it touches on the way out — `EditedArticle`,
`TipPublisher`, the SQLite repository — is unchanged.

The scheduler calls in here only when ``LIVEON_EVIDENCE_PIPELINE`` is set. Until then the
legacy prose path runs exactly as before.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, Mapping

from app.models.editor import EditedArticle
from app.models.evidence import EvidenceBundle, EvidenceRecord
from app.models.run_outcome import RunOutcome
from app.models.summarizer import ArticleDraft
from app.models.tip import TipDraft
from app.services.evidence.extractor import ExtractorAgent
from app.services.evidence.reviewer import EvidenceReviewer
from app.services.evidence.runlog import RunLog
from app.services.evidence.store import EvidenceStore
from app.services.evidence.synthesizer import SynthesizerAgent
from app.services.evidence.writers import ArticleWriter, TipWriter, evidence_fields
from app.services.evidence_pipeline import EvidencePipeline, run_article, run_tip
from app.services.llm_factory import create_chat_model
from app.services.research.pubmed import build_pubmed_client
from app.services.sqlite_repo import create_repository
from app.services.tip_publisher import TipPublisher

LOGGER = logging.getLogger(__name__)

__all__ = ["build_evidence_pipeline", "research_queries", "run_article_job", "run_tip_job"]

#: A deliberately small default. Discovery is filtered by publication type so that what
#: arrives is already the kind of evidence the rubric can grade highly.
DEFAULT_QUERIES: tuple[str, ...] = (
    '("longevity"[tiab] OR "healthy aging"[tiab] OR "healthspan"[tiab]) '
    "AND (randomized controlled trial[pt] OR meta-analysis[pt] OR systematic review[pt])",
    '("caloric restriction"[tiab] OR "time-restricted eating"[tiab] OR "physical activity"[tiab]) '
    "AND (randomized controlled trial[pt] OR meta-analysis[pt])",
)


def research_queries() -> list[str]:
    """The literature queries this deployment discovers with."""

    raw = (os.getenv("LIVEON_RESEARCH_QUERIES") or "").strip()
    if not raw:
        return list(DEFAULT_QUERIES)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.warning("Ignoring invalid JSON in LIVEON_RESEARCH_QUERIES")
        return list(DEFAULT_QUERIES)

    queries: list[str] = []
    for entry in payload if isinstance(payload, list) else []:
        if isinstance(entry, str) and entry.strip():
            queries.append(entry.strip())
        elif isinstance(entry, dict) and str(entry.get("query") or "").strip():
            queries.append(str(entry["query"]).strip())

    return queries or list(DEFAULT_QUERIES)


def _db_path() -> str | None:
    return (os.getenv("LIVEON_DB_PATH") or "").strip() or None


def build_evidence_pipeline(
    *,
    store: EvidenceStore | None = None,
    llm: Any = None,
) -> EvidencePipeline:
    """Construct the pipeline from configuration.

    One model serves every stage by default. Extraction and review are the JSON-heavy
    stages and benefit most from a larger model, which is what the per-agent
    ``LIVEON_<AGENT>_MODEL`` variables are for.
    """

    evidence_store = store or EvidenceStore(_db_path())
    extraction_llm = llm or create_chat_model(agent_label="extractor", json_mode=True)
    synthesis_llm = llm or create_chat_model(agent_label="synthesizer", json_mode=True)
    review_llm = llm or create_chat_model(agent_label="reviewer", json_mode=True)

    return EvidencePipeline(
        store=evidence_store,
        runlog=RunLog(_db_path()),
        acquirer=build_pubmed_client(),
        extractor=ExtractorAgent(llm=extraction_llm, model_id=_model_id(extraction_llm)),
        synthesizer=SynthesizerAgent(llm=synthesis_llm, model_id=_model_id(synthesis_llm)),
        reviewer=EvidenceReviewer(llm=review_llm, model_id=_model_id(review_llm)),
        queries=research_queries(),
    )


def _model_id(llm: Any) -> str:
    """Best-effort model identity, recorded on every extraction and review."""

    for attribute in ("model", "model_name", "model_id"):
        value = getattr(llm, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return type(llm).__name__


# ----------------------------------------------------------------------
# Publishing adapters
# ----------------------------------------------------------------------


def _publish_article(
    draft: ArticleDraft,
    bundle: EvidenceBundle,
    records: Mapping[str, EvidenceRecord],
    *,
    repository: Any,
    published_at: datetime,
) -> Any:
    """Store an article, carrying its evidence through to the stored row."""

    edited = EditedArticle.from_draft(draft)
    article = edited.to_article()
    article.published_date = published_at

    fields = evidence_fields(bundle, records)
    article.evidence_bundle_id = fields["evidence_bundle_id"]
    article.evidence_keys = list(fields["evidence_keys"])
    article.evidence_grade = fields["evidence_grade"]
    article.evidence_summary = fields["evidence_summary"]
    article.evidence_limitations = list(fields["evidence_limitations"])
    article.source_urls = list(fields["source_urls"])

    return repository.save_article(article)


def _publish_tip(
    draft: TipDraft,
    bundle: EvidenceBundle,
    records: Mapping[str, EvidenceRecord],
    *,
    publisher: TipPublisher,
    published_at: datetime,
) -> Any:
    """Store a tip. The draft already carries its evidence fields from the writer."""

    result = publisher.publish(draft, published_at=published_at)
    return result.tip


# ----------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------


def run_article_job(run_at: datetime | None = None) -> RunOutcome:
    """Produce one evidence-backed article, or explain why not."""

    moment = run_at or datetime.now(timezone.utc)
    pipeline = build_evidence_pipeline()
    repository = create_repository()

    result = run_article(
        pipeline,
        ArticleWriter(llm=pipeline.synthesizer.llm, model_id=pipeline.synthesizer.model_id),
        lambda draft, bundle, records: _publish_article(
            draft, bundle, records, repository=repository, published_at=moment
        ),
    )
    _log_result("article", result)
    return result.outcome


def run_tip_job(run_at: datetime | None = None) -> RunOutcome:
    """Produce one evidence-backed tip, or explain why not."""

    moment = run_at or datetime.now(timezone.utc)
    pipeline = build_evidence_pipeline()
    publisher = TipPublisher(create_repository())

    result = run_tip(
        pipeline,
        TipWriter(llm=pipeline.synthesizer.llm, model_id=pipeline.synthesizer.model_id),
        lambda draft, bundle, records: _publish_tip(
            draft, bundle, records, publisher=publisher, published_at=moment
        ),
    )
    _log_result("tip", result)
    return result.outcome


def _log_result(kind: str, result: Any) -> None:
    LOGGER.info(
        "Evidence %s run finished: %s (%s candidate topic(s), %s acquired)",
        kind,
        result.outcome.value,
        result.considered,
        result.acquired,
        extra={
            "event": "evidence_jobs.finished",
            "kind": kind,
            "outcome": result.outcome.value,
        },
    )
    for note in result.notes:
        if note:
            LOGGER.info("Evidence %s note: %s", kind, note)
