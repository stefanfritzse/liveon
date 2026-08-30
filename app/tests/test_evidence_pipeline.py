"""Tests for the evidence pipeline: acquisition through publication.

These run the whole path with stub models and a real store, because the value of this
module is entirely in how the stages hand off to each other — and in what happens when one
of them says no.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pytest
from langchain_core.messages import AIMessage

from app.models.evidence import EvidenceRecord
from app.models.run_outcome import RunOutcome
from app.services.evidence.extractor import ExtractorAgent
from app.services.evidence.reviewer import EvidenceReviewer
from app.services.evidence.store import EvidenceStore
from app.services.evidence.synthesizer import SynthesizerAgent
from app.services.evidence.writers import ArticleWriter, TipWriter
from app.services.evidence_pipeline import (
    EvidencePipeline,
    evidence_pipeline_enabled,
    run_article,
    run_tip,
)
from app.services.research.http import ResearchRequestError
from app.services.research.pubmed import parse_pubmed_articles

NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)

TRIAL_XML = """<?xml version="1.0" ?>
<PubmedArticleSet><PubmedArticle>
  <MedlineCitation>
    <PMID Version="1">38412345</PMID>
    <Article>
      <ArticleTitle>Time-restricted eating and mortality</ArticleTitle>
      <Abstract>
        <AbstractText Label="METHODS">We randomised 412 adults to an eight-hour window.</AbstractText>
        <AbstractText Label="RESULTS">Ten-year mortality fell by 4.2 percent.</AbstractText>
      </Abstract>
      <PublicationTypeList>
        <PublicationType>Randomized Controlled Trial</PublicationType>
      </PublicationTypeList>
      <ArticleDate DateType="Electronic"><Year>2026</Year><Month>08</Month><Day>01</Day></ArticleDate>
    </Article>
    <MeshHeadingList>
      <MeshHeading><DescriptorName>Humans</DescriptorName></MeshHeading>
    </MeshHeadingList>
  </MedlineCitation>
  <PubmedData><ArticleIdList>
    <ArticleId IdType="pubmed">38412345</ArticleId>
    <ArticleId IdType="doi">10.1001/jama.2026.1</ArticleId>
  </ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>
"""

EXTRACTION = {
    "sample_size": {"value": 412, "quote": "412 adults"},
    "intervention": {"value": "eight-hour window", "quote": "eight-hour window"},
    "outcomes": [
        {
            "name": "mortality",
            "direction": {"value": "decrease", "quote": "mortality fell"},
            "is_surrogate": {"value": False, "quote": "Ten-year mortality"},
            "magnitude": {"value": 4.2, "quote": "4.2 percent"},
        }
    ],
}

CLAIMS = {
    "claims": [
        {
            "text": "Ten-year mortality fell by 4.2 percent.",
            "claim_type": "causal",
            "evidence": ["E1"],
            "population_scope": "adults",
        }
    ]
}

ARTICLE = {
    "title": "An eight-hour window and ten-year mortality",
    "summary": "A randomised trial reported lower mortality.",
    "body": "Ten-year mortality fell by 4.2 percent [E1].",
    "takeaways": ["Mortality fell by 4.2 percent"],
    "tags": ["nutrition"],
}

TIP = {
    "title": "Try an eight-hour eating window",
    "body": "Keeping meals inside eight hours was studied in a randomised trial.",
    "tags": ["nutrition"],
}


class StubLLM:
    def __init__(self, *responses: Any) -> None:
        self._responses = [
            response if isinstance(response, str) else json.dumps(response)
            for response in responses
        ]
        self.calls: list[Sequence[Any]] = []

    def invoke(self, input: Any, **_: Any) -> AIMessage:
        self.calls.append(input if isinstance(input, list) else [input])
        return AIMessage(content=self._responses[min(len(self.calls) - 1, len(self._responses) - 1)])


class StubAcquirer:
    def __init__(self, *, records=None, error: Exception | None = None) -> None:
        self._records = records if records is not None else parse_pubmed_articles(TRIAL_XML)
        self._error = error
        self.queries: list[str] = []

    def search_records(self, query: str, *, max_results: int = 20) -> list[EvidenceRecord]:
        self.queries.append(query)
        if self._error is not None:
            raise self._error
        return [
            EvidenceRecord.from_document(record.to_document()) for record in self._records
        ]


class Publisher:
    """Records what was published and hands back something with an id."""

    def __init__(self, error: Exception | None = None) -> None:
        self.published: list[Any] = []
        self._error = error

    def __call__(self, draft, bundle, records):
        if self._error is not None:
            raise self._error
        self.published.append((draft, bundle))
        return type("Stored", (), {"id": f"content-{len(self.published)}"})()


@pytest.fixture()
def store(tmp_path: Path) -> EvidenceStore:
    with EvidenceStore(tmp_path / "content.db") as opened:
        yield opened


def _pipeline(store: EvidenceStore, *, acquirer=None, claims=CLAIMS, reviewer_llm=None):
    return EvidencePipeline(
        store=store,
        acquirer=acquirer if acquirer is not None else StubAcquirer(),
        extractor=ExtractorAgent(llm=StubLLM(EXTRACTION), model_id="stub"),
        synthesizer=SynthesizerAgent(
            llm=StubLLM(claims), model_id="stub", bundle_id_factory=lambda: "bundle-1"
        ),
        reviewer=EvidenceReviewer(llm=reviewer_llm, model_id="stub", now=lambda: NOW),
        queries=["longevity"],
        now=lambda: NOW,
    )


# -- the flag ----------------------------------------------------------


def test_the_pipeline_is_off_until_switched_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVEON_EVIDENCE_PIPELINE", raising=False)
    assert evidence_pipeline_enabled() is False

    monkeypatch.setenv("LIVEON_EVIDENCE_PIPELINE", "1")
    assert evidence_pipeline_enabled() is True

    monkeypatch.setenv("LIVEON_EVIDENCE_PIPELINE", "no")
    assert evidence_pipeline_enabled() is False


# -- the happy path ----------------------------------------------------


def test_an_article_is_published_from_acquired_evidence(store: EvidenceStore) -> None:
    publisher = Publisher()

    result = run_article(_pipeline(store), ArticleWriter(llm=StubLLM(ARTICLE)), publisher)

    assert result.outcome is RunOutcome.PUBLISHED
    assert result.acquired == 1
    assert publisher.published
    draft, bundle = publisher.published[0]
    assert bundle.grade == "moderate"
    assert draft.sources == ["https://doi.org/10.1001/jama.2026.1"]


def test_a_tip_is_published_from_the_same_evidence(store: EvidenceStore) -> None:
    publisher = Publisher()

    result = run_tip(_pipeline(store), TipWriter(llm=StubLLM(TIP)), publisher)

    assert result.outcome is RunOutcome.PUBLISHED
    draft, _ = publisher.published[0]
    assert draft.evidence_keys == ["doi:10.1001/jama.2026.1"]
    assert draft.evidence_grade == "moderate"


def test_publishing_records_usage_so_the_topic_is_not_repeated(store: EvidenceStore) -> None:
    run_article(_pipeline(store), ArticleWriter(llm=StubLLM(ARTICLE)), Publisher())

    usage = store.usage_for_source("doi:10.1001/jama.2026.1")

    assert len(usage) == 1
    assert usage[0]["content_type"] == "article"
    assert store.last_used_at("eight-hour-window|mortality") is not None


def test_a_second_run_refuses_the_topic_it_just_covered(store: EvidenceStore) -> None:
    """G9 in its natural habitat: the repetition the tip editor could never see."""

    run_article(_pipeline(store), ArticleWriter(llm=StubLLM(ARTICLE)), Publisher())

    second = run_article(_pipeline(store), ArticleWriter(llm=StubLLM(ARTICLE)), Publisher())

    assert second.outcome is RunOutcome.REVIEW_REJECTED
    assert any(violation.gate == "G9" for violation in second.violations)


def test_acquisition_does_not_re_store_a_record_it_already_has(store: EvidenceStore) -> None:
    pipeline = _pipeline(store)

    assert pipeline.acquire() == 1
    assert pipeline.acquire() == 0


def test_extraction_promotes_records_to_approved(store: EvidenceStore) -> None:
    pipeline = _pipeline(store)
    pipeline.acquire()

    assert pipeline.extract_pending() == 1
    assert store.records_in_state("acquired") == []
    assert len(store.records_in_state("approved")) == 1


def test_candidates_are_ranked_before_anything_is_written(store: EvidenceStore) -> None:
    pipeline = _pipeline(store)
    pipeline.acquire()
    pipeline.extract_pending()

    candidates = pipeline.candidates()

    assert candidates
    assert candidates[0].grade == "moderate"
    assert candidates[0].cluster.key == "eight-hour-window"


# -- failure paths -----------------------------------------------------


def test_a_retrieval_failure_publishes_nothing_and_asks_for_a_retry(
    store: EvidenceStore,
) -> None:
    pipeline = _pipeline(store, acquirer=StubAcquirer(error=ResearchRequestError("down")))
    publisher = Publisher()

    result = run_article(pipeline, ArticleWriter(llm=StubLLM(ARTICLE)), publisher)

    assert result.outcome is RunOutcome.RETRIEVAL_FAILED
    assert publisher.published == []


def test_an_empty_store_is_a_quiet_day(store: EvidenceStore) -> None:
    pipeline = _pipeline(store, acquirer=StubAcquirer(records=[]))

    result = run_article(pipeline, ArticleWriter(llm=StubLLM(ARTICLE)), Publisher())

    assert result.outcome is RunOutcome.NO_NEW_EVIDENCE


def test_synthesis_producing_no_claims_publishes_nothing(store: EvidenceStore) -> None:
    pipeline = _pipeline(store, claims={"claims": []})

    result = run_article(pipeline, ArticleWriter(llm=StubLLM(ARTICLE)), Publisher())

    assert result.outcome is RunOutcome.EVIDENCE_INSUFFICIENT


def test_a_review_refusal_publishes_nothing(store: EvidenceStore) -> None:
    """An unhedged causal claim on a cohort: the reviewer refuses, the run ends empty."""

    claims = {
        "claims": [
            {
                "text": "Ten-year mortality fell by 99 percent.",
                "claim_type": "causal",
                "evidence": ["E1"],
            }
        ]
    }
    publisher = Publisher()

    result = run_article(
        _pipeline(store, claims=claims), ArticleWriter(llm=StubLLM(ARTICLE)), publisher
    )

    assert result.outcome is RunOutcome.REVIEW_REJECTED
    assert publisher.published == []
    assert any(violation.gate == "G2" for violation in result.violations)


def test_a_draft_that_keeps_failing_the_recheck_is_abandoned(store: EvidenceStore) -> None:
    """The evidence was fine; the prose kept drifting off it. Nothing publishes."""

    drifting = StubLLM({"title": "T", "summary": "S", "body": "Mortality fell by 41 percent."})
    publisher = Publisher()

    result = run_article(_pipeline(store), ArticleWriter(llm=drifting), publisher)

    assert result.outcome is RunOutcome.REVIEW_REJECTED
    assert result.attempts > 1
    assert publisher.published == []
    assert any(violation.gate == "G2" for violation in result.violations)


def test_a_publisher_failure_is_reported_as_a_dependency_problem(store: EvidenceStore) -> None:
    publisher = Publisher(error=RuntimeError("database is locked"))

    result = run_article(_pipeline(store), ArticleWriter(llm=StubLLM(ARTICLE)), publisher)

    assert result.outcome is RunOutcome.SOURCE_UNAVAILABLE


def test_a_writer_failure_is_a_model_failure(store: EvidenceStore) -> None:
    class Exploding:
        def invoke(self, *_args, **_kwargs):
            raise RuntimeError("model is down")

    result = run_article(_pipeline(store), ArticleWriter(llm=Exploding()), Publisher())

    assert result.outcome is RunOutcome.MODEL_FAILED


def test_the_run_moves_on_to_the_next_candidate_when_one_is_refused(
    store: EvidenceStore,
) -> None:
    """A repeated topic should not block a run while better candidates are waiting."""

    run_article(_pipeline(store), ArticleWriter(llm=StubLLM(ARTICLE)), Publisher())

    second_xml = TRIAL_XML.replace("38412345", "39999999").replace(
        "10.1001/jama.2026.1", "10.1001/jama.2026.2"
    ).replace("eight-hour window", "daily walking")
    pipeline = _pipeline(
        store,
        acquirer=StubAcquirer(records=parse_pubmed_articles(second_xml)),
        claims={
            "claims": [
                {
                    "text": "Ten-year mortality fell by 4.2 percent.",
                    "claim_type": "causal",
                    "evidence": ["E1"],
                }
            ]
        },
    )
    pipeline.extractor = ExtractorAgent(
        llm=StubLLM(
            {
                "sample_size": {"value": 412, "quote": "412 adults"},
                "intervention": {"value": "daily walking", "quote": "daily walking"},
                "outcomes": EXTRACTION["outcomes"],
            }
        ),
        model_id="stub",
    )
    publisher = Publisher()

    result = run_article(pipeline, ArticleWriter(llm=StubLLM(ARTICLE)), publisher)

    assert result.outcome is RunOutcome.PUBLISHED
    assert publisher.published


def test_the_advisory_reviewer_can_stop_a_publication(store: EvidenceStore) -> None:
    reviewer_llm = StubLLM({"status": "rejected", "grade": "moderate", "concerns": ["overstated"]})
    publisher = Publisher()

    result = run_article(
        _pipeline(store, reviewer_llm=reviewer_llm),
        ArticleWriter(llm=StubLLM(ARTICLE)),
        publisher,
    )

    assert result.outcome is RunOutcome.REVIEW_REJECTED
    assert publisher.published == []


def test_the_bundle_is_stored_whether_or_not_it_publishes(store: EvidenceStore) -> None:
    """The run log needs the refused bundle as much as the published one."""

    claims = {"claims": [{"text": "Mortality fell by 99 percent.", "evidence": ["E1"]}]}

    run_article(_pipeline(store, claims=claims), ArticleWriter(llm=StubLLM(ARTICLE)), Publisher())

    stored = store.get_bundle("bundle-1")
    assert stored is not None
    assert stored.review_status in ("regenerate", "rejected")
