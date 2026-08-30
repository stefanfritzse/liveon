"""End-to-end flow through the evidence layer, with stub models and a real store.

Every stage is unit-tested elsewhere. What this file checks is that they compose: that a
record acquired from PubMed XML survives storage, extraction, synthesis and review with
its spans intact, and that the two headline failures — an invented number and a mouse
study written up as a human benefit — are stopped by the time they reach publication.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pytest
from langchain_core.messages import AIMessage

from app.services.evidence.extractor import ExtractorAgent
from app.services.evidence.reviewer import EvidenceReviewer
from app.services.evidence.store import EvidenceStore
from app.services.evidence.synthesizer import SynthesizerAgent
from app.services.research.pubmed import parse_pubmed_articles

NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)

TRIAL_XML = """<?xml version="1.0" ?>
<PubmedArticleSet><PubmedArticle>
  <MedlineCitation>
    <PMID Version="1">38412345</PMID>
    <Article>
      <Journal><Title>JAMA</Title>
        <JournalIssue><PubDate><Year>2026</Year><Month>Jan</Month></PubDate></JournalIssue>
      </Journal>
      <ArticleTitle>Time-restricted eating and mortality</ArticleTitle>
      <Abstract>
        <AbstractText Label="METHODS">We randomised 412 adults to an eight-hour window.</AbstractText>
        <AbstractText Label="RESULTS">Ten-year mortality fell by 4.2 percent.</AbstractText>
      </Abstract>
      <PublicationTypeList>
        <PublicationType>Randomized Controlled Trial</PublicationType>
      </PublicationTypeList>
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

MOUSE_XML = """<?xml version="1.0" ?>
<PubmedArticleSet><PubmedArticle>
  <MedlineCitation>
    <PMID Version="1">39000001</PMID>
    <Article>
      <ArticleTitle>Rapamycin extends lifespan in mice</ArticleTitle>
      <Abstract><AbstractText>Treated mice lived 18 percent longer.</AbstractText></Abstract>
      <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
    </Article>
    <MeshHeadingList>
      <MeshHeading><DescriptorName>Animals</DescriptorName></MeshHeading>
      <MeshHeading><DescriptorName>Mice</DescriptorName></MeshHeading>
    </MeshHeadingList>
  </MedlineCitation>
  <PubmedData><ArticleIdList>
    <ArticleId IdType="pubmed">39000001</ArticleId>
  </ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>
"""

TRIAL_EXTRACTION = {
    "sample_size": {"value": 412, "quote": "412 adults"},
    "population": {"value": "adults", "quote": "412 adults"},
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

MOUSE_EXTRACTION = {
    "sample_size": "not_reported",
    "outcomes": [
        {
            "name": "lifespan",
            "direction": {"value": "increase", "quote": "lived 18 percent longer"},
            "magnitude": {"value": 18, "quote": "18 percent"},
        }
    ],
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


@pytest.fixture()
def store(tmp_path: Path) -> EvidenceStore:
    with EvidenceStore(tmp_path / "content.db") as opened:
        yield opened


def _acquire(store: EvidenceStore, xml: str, extraction: dict[str, Any]):
    """Run one source through acquisition, storage, extraction and approval."""

    record = parse_pubmed_articles(xml)[0]
    store.upsert_record(record)

    extracted = ExtractorAgent(llm=StubLLM(extraction), model_id="stub").extract(
        store.get_record(record.source_key)
    )
    store.upsert_record(extracted)
    store.set_state(record.source_key, "approved")
    return store.get_record(record.source_key)


def _synthesize(records, claims: list[dict[str, Any]]):
    agent = SynthesizerAgent(
        llm=StubLLM({"claims": claims}),
        model_id="stub",
        now=lambda: NOW,
        bundle_id_factory=lambda: "bundle-1",
    )
    return agent.synthesize(records)


def _review(bundle, records, **kwargs):
    reviewer = EvidenceReviewer(model_id="stub", now=lambda: NOW)
    return reviewer.review(bundle, {record.source_key: record for record in records}, **kwargs)


# -- the happy path ----------------------------------------------------


def test_a_trial_travels_from_pubmed_xml_to_an_approved_bundle(store: EvidenceStore) -> None:
    record = _acquire(store, TRIAL_XML, TRIAL_EXTRACTION)

    assert record.classification.design == "rct"
    assert record.sample_size.value == 412

    bundle = _synthesize(
        [record],
        [
            {
                "text": "An eight-hour eating window cut ten-year mortality by 4.2 percent.",
                "claim_type": "causal",
                "evidence": ["E1"],
                "population_scope": "adults",
            }
        ],
    )

    decision = _review(bundle, [record])

    assert decision.status == "approved"
    assert decision.grade == "moderate"
    assert bundle.claims[0].evidence_keys == ["doi:10.1001/jama.2026.1"]
    assert bundle.claims[0].numbers[0].span.verify(record.document_text)


def test_the_published_claim_traces_back_to_the_source_document(store: EvidenceStore) -> None:
    """published claim -> reviewed claim -> evidence record -> original span, no LLM."""

    record = _acquire(store, TRIAL_XML, TRIAL_EXTRACTION)
    bundle = _synthesize(
        [record],
        [{"text": "Mortality fell by 4.2 percent.", "claim_type": "causal", "evidence": ["E1"]}],
    )
    _review(bundle, [record])
    store.save_bundle(bundle)

    reloaded = store.get_bundle("bundle-1")
    assert reloaded is not None
    number = reloaded.claims[0].numbers[0]
    source = store.get_record(number.source_key)

    assert source is not None
    assert number.span.verify(source.document_text)
    assert "4.2 percent" in number.span.quote


# -- the failures it exists to stop -------------------------------------


def test_an_invented_number_never_reaches_publication(store: EvidenceStore) -> None:
    record = _acquire(store, TRIAL_XML, TRIAL_EXTRACTION)

    bundle = _synthesize(
        [record],
        [{"text": "Mortality fell by 41 percent.", "claim_type": "causal", "evidence": ["E1"]}],
    )
    decision = _review(bundle, [record])

    assert bundle.claims[0].numbers == []
    assert decision.status == "regenerate"
    assert any(violation.gate == "G2" for violation in decision.violations)


def test_a_mouse_study_cannot_be_written_up_as_a_human_benefit(store: EvidenceStore) -> None:
    record = _acquire(store, MOUSE_XML, MOUSE_EXTRACTION)

    assert record.classification.subject == "animal"

    bundle = _synthesize(
        [record],
        [{"text": "People taking it lived 18 percent longer.", "claim_type": "causal", "evidence": ["E1"]}],
    )
    decision = _review(bundle, [record])

    assert decision.status == "regenerate"
    assert {v.gate for v in decision.violations} >= {"G3", "G4"}


def test_the_same_mouse_finding_is_publishable_when_stated_honestly(store: EvidenceStore) -> None:
    """The gate is about the sentence, not about refusing preclinical work outright."""

    record = _acquire(store, MOUSE_XML, MOUSE_EXTRACTION)

    bundle = _synthesize(
        [record],
        [
            {
                "text": "Treated mice lived 18 percent longer.",
                "claim_type": "descriptive",
                "evidence": ["E1"],
            }
        ],
    )
    decision = _review(bundle, [record])

    assert decision.status == "approved"
    assert decision.grade == "preliminary"


def test_a_retraction_discovered_later_blocks_a_previously_fine_claim(store: EvidenceStore) -> None:
    record = _acquire(store, TRIAL_XML, TRIAL_EXTRACTION)
    bundle = _synthesize(
        [record],
        [{"text": "Mortality fell by 4.2 percent.", "claim_type": "causal", "evidence": ["E1"]}],
    )
    assert _review(bundle, [record]).status == "approved"

    store.set_retraction(record.source_key, "retracted", notes=["RetractionIn: JAMA 2027"])
    retracted = store.get_record(record.source_key)

    decision = _review(bundle, [retracted])

    assert decision.status == "rejected"
    assert decision.grade == "insufficient"


def test_a_source_that_was_never_acquired_cannot_be_cited(store: EvidenceStore) -> None:
    record = _acquire(store, TRIAL_XML, TRIAL_EXTRACTION)
    bundle = _synthesize(
        [record], [{"text": "A claim.", "claim_type": "descriptive", "evidence": ["E1"]}]
    )
    # Simulate a citation that resolves to nothing, as an invented one would.
    bundle.claims[0].evidence_keys = ["doi:10.9999/invented"]

    decision = _review(bundle, [record])

    assert decision.status == "regenerate"
    assert any(violation.gate == "G1" for violation in decision.violations)


def test_extraction_is_not_repeated_for_a_record_already_extracted(store: EvidenceStore) -> None:
    """The cache is what keeps a re-run from paying for every abstract twice."""

    record = parse_pubmed_articles(TRIAL_XML)[0]
    store.upsert_record(record)
    stub = StubLLM(TRIAL_EXTRACTION)
    agent = ExtractorAgent(llm=stub, model_id="stub")

    first = agent.extract(store.get_record(record.source_key))
    store.upsert_record(first)
    agent.extract(store.get_record(record.source_key))

    assert len(stub.calls) == 1
