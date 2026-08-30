"""Tests for the job seam: configuration, publishing adapters, scheduler dispatch.

This is where the evidence layer meets the rest of the application, so the questions are
about wiring: does the flag actually switch paths, does provenance survive the adapter
into a stored row, and does a failure here still fail closed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from app.models.evidence import (
    Claim,
    Classification,
    EvidenceBundle,
    EvidenceRecord,
    Extracted,
    NumberRef,
    Span,
)
from app.models.run_outcome import RunOutcome
from app.models.summarizer import ArticleDraft
from app.models.tip import TipDraft
from app.services import evidence_jobs
from app.services.evidence_jobs import (
    DEFAULT_QUERIES,
    _publish_article,
    _publish_tip,
    research_queries,
)
from app.services.sqlite_repo import LocalSQLiteContentRepository
from app.services.tip_publisher import TipPublisher

KEY = "doi:10.1001/jama.2026.1"
DOCUMENT = "We randomised 412 adults and mortality fell by 4.2 percent."
NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


def _span(quote: str) -> Span:
    span = Span.locate(DOCUMENT, quote)
    assert span is not None
    return span


def _record() -> EvidenceRecord:
    return EvidenceRecord(
        source_key=KEY,
        title="Time-restricted eating and mortality",
        document_text=DOCUMENT,
        classification=Classification(design="rct", subject="human"),
        sample_size=Extracted.found(412, _span("412 adults")),
        state="approved",
    )


def _bundle() -> EvidenceBundle:
    return EvidenceBundle(
        bundle_id="bundle-1",
        topic_key="tre|mortality",
        grade="moderate",
        review_status="approved",
        claims=[
            Claim(
                text="Mortality fell by 4.2 percent.",
                claim_type="causal",
                evidence_keys=[KEY],
                numbers=[NumberRef(text="4.2", source_key=KEY, span=_span("4.2 percent"))],
            )
        ],
    )


# -- configuration -----------------------------------------------------


def test_the_default_queries_filter_by_publication_type() -> None:
    """Discovery is what decides whether the rubric ever sees gradable evidence."""

    assert research_queries() == list(DEFAULT_QUERIES)
    assert all("[pt]" in query for query in DEFAULT_QUERIES)


def test_queries_can_be_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_RESEARCH_QUERIES", json.dumps(["sleep AND longevity"]))

    assert research_queries() == ["sleep AND longevity"]


def test_queries_accept_the_object_form(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "LIVEON_RESEARCH_QUERIES", json.dumps([{"name": "sleep", "query": "sleep AND aging"}])
    )

    assert research_queries() == ["sleep AND aging"]


@pytest.mark.parametrize("raw", ["not json", "[]", '{"query": "x"}'])
def test_invalid_query_configuration_falls_back_to_the_defaults(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("LIVEON_RESEARCH_QUERIES", raw)

    assert research_queries() == list(DEFAULT_QUERIES)


# -- publishing adapters -----------------------------------------------


def test_an_article_keeps_its_evidence_through_the_adapter(tmp_path: Path) -> None:
    repository = LocalSQLiteContentRepository(tmp_path / "content.db")
    draft = ArticleDraft(
        title="An eight-hour window",
        summary="A trial found lower mortality.",
        body="Mortality fell by 4.2 percent.",
        takeaways=["Lower mortality"],
        sources=["https://doi.org/10.1001/jama.2026.1"],
        tags=["nutrition"],
    )

    stored = _publish_article(
        draft, _bundle(), {KEY: _record()}, repository=repository, published_at=NOW
    )

    reloaded = repository.get_article(stored.id)
    assert reloaded is not None
    assert reloaded.evidence_bundle_id == "bundle-1"
    assert reloaded.evidence_keys == [KEY]
    assert reloaded.evidence_grade == "moderate"
    assert reloaded.evidence_summary == "Moderate — 1 human randomised trial"
    assert reloaded.source_urls == ["https://doi.org/10.1001/jama.2026.1"]
    assert reloaded.published_date == NOW


def test_a_tip_keeps_its_evidence_through_the_adapter(tmp_path: Path) -> None:
    repository = LocalSQLiteContentRepository(tmp_path / "content.db")
    draft = TipDraft(
        title="Try an eight-hour window",
        body="Keeping meals inside eight hours was studied in a trial.",
        tags=["nutrition"],
        source_urls=["https://doi.org/10.1001/jama.2026.1"],
        evidence_bundle_id="bundle-1",
        evidence_keys=[KEY],
        evidence_grade="moderate",
        evidence_summary="Moderate — 1 human randomised trial",
    )

    stored = _publish_tip(
        draft,
        _bundle(),
        {KEY: _record()},
        publisher=TipPublisher(repository),
        published_at=NOW,
    )

    reloaded = repository.get_tip(stored.id)
    assert reloaded is not None
    assert reloaded.evidence_keys == [KEY]
    assert reloaded.evidence_grade == "moderate"
    assert reloaded.source_urls == ["https://doi.org/10.1001/jama.2026.1"]


# -- scheduler dispatch ------------------------------------------------


def test_the_scheduler_uses_the_legacy_path_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import pipeline_scheduler

    monkeypatch.delenv("LIVEON_EVIDENCE_PIPELINE", raising=False)
    called: list[str] = []
    monkeypatch.setattr(
        evidence_jobs, "run_article_job", lambda run_at: called.append("evidence")
    )

    # The legacy path builds a real pipeline; failing to is still "not the evidence path".
    pipeline_scheduler._run_article_pipeline(NOW)

    assert called == []


@pytest.mark.parametrize(
    ("runner", "job_name"),
    [("_run_article_pipeline", "run_article_job"), ("_run_tip_pipeline", "run_tip_job")],
)
def test_the_flag_switches_the_scheduler_to_the_evidence_path(
    monkeypatch: pytest.MonkeyPatch, runner: str, job_name: str
) -> None:
    from app.services import pipeline_scheduler

    monkeypatch.setenv("LIVEON_EVIDENCE_PIPELINE", "1")
    calls: list[datetime] = []

    def _job(run_at: datetime) -> RunOutcome:
        calls.append(run_at)
        return RunOutcome.PUBLISHED

    monkeypatch.setattr(evidence_jobs, job_name, _job)

    outcome = getattr(pipeline_scheduler, runner)(NOW)

    assert calls == [NOW]
    assert outcome is RunOutcome.PUBLISHED


@pytest.mark.parametrize(
    ("runner", "job_name"),
    [("_run_article_pipeline", "run_article_job"), ("_run_tip_pipeline", "run_tip_job")],
)
def test_an_evidence_job_that_raises_fails_closed(
    monkeypatch: pytest.MonkeyPatch, runner: str, job_name: str
) -> None:
    """A crash must back off and publish nothing, not stamp the cadence."""

    from app.models.run_outcome import policy_for
    from app.services import pipeline_scheduler

    monkeypatch.setenv("LIVEON_EVIDENCE_PIPELINE", "1")

    def _explode(run_at: datetime) -> RunOutcome:
        raise RuntimeError("boom")

    monkeypatch.setattr(evidence_jobs, job_name, _explode)

    outcome = getattr(pipeline_scheduler, runner)(NOW)

    assert outcome is RunOutcome.MODEL_FAILED
    assert policy_for(outcome).stamp is False


def test_the_pipeline_is_built_from_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Constructing it must not reach the network, even to build a client."""

    from app.services.evidence.store import EvidenceStore

    class StubLLM:
        model = "stub-model"

        def invoke(self, *_args: Any, **_kwargs: Any) -> AIMessage:  # pragma: no cover
            return AIMessage(content="{}")

    monkeypatch.setenv("LIVEON_RESEARCH_QUERIES", json.dumps(["sleep AND aging"]))
    store = EvidenceStore(tmp_path / "content.db")

    pipeline = evidence_jobs.build_evidence_pipeline(store=store, llm=StubLLM())

    assert pipeline.queries == ["sleep AND aging"]
    assert pipeline.extractor.model_id == "stub-model"
    assert pipeline.reviewer.model_id == "stub-model"
    assert pipeline.store is store
    pipeline.acquirer.http.close()
    store.close()


def test_the_model_identity_falls_back_to_the_class_name() -> None:
    class Anonymous:
        pass

    assert evidence_jobs._model_id(Anonymous()) == "Anonymous"


def test_discovery_covers_the_subject_rather_than_one_corner_of_it() -> None:
    """The first live runs all landed on one intervention, because one query asked for it."""

    queries = research_queries()

    assert len(queries) >= 6
    joined = " ".join(queries).lower()
    for area in ("longevity", "resistance training", "sleep", "diet", "fasting", "vitamin d"):
        assert area in joined


def test_every_query_filters_for_gradable_evidence() -> None:
    """Filtering at discovery is cheaper than grading weak work and refusing it."""

    assert all("[pt]" in query for query in research_queries())


def test_the_per_query_result_count_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.evidence_jobs import max_results_per_query

    assert max_results_per_query() == 8

    monkeypatch.setenv("LIVEON_MAX_RESULTS_PER_QUERY", "3")
    assert max_results_per_query() == 3

    monkeypatch.setenv("LIVEON_MAX_RESULTS_PER_QUERY", "nonsense")
    assert max_results_per_query() == 8
