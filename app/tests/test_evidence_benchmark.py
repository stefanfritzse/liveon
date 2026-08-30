"""The evidence benchmark: the invariants a model or prompt change must not break.

improvements.md item 11. These are not unit tests of a function — every one of them is a
property of the system as a whole, asserted over the checked-in corpus in
``app/tests/fixtures/corpus``. The suite runs offline with stub models, so it is
deterministic: a failure here means the *behaviour* changed, never that a model had an
off day.

This is what stands in for a human reviewer at release time. A prompt edit that quietly
lowers scientific integrity should fail the build rather than reach readers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from app.models.content import Tip
from app.models.evidence import (
    Claim,
    EvidenceBundle,
    EvidenceRecord,
    NumberRef,
    clamp_grade,
)
from app.models.run_outcome import OUTCOME_POLICY, RunOutcome
from app.models.tip import TipDraft
from app.services.evidence.extractor import ExtractorAgent
from app.services.evidence.gates import run_gates
from app.services.evidence.grading import compute_grade
from app.services.evidence.postedit import recheck_published_text
from app.services.evidence.reviewer import REVIEW_QUESTIONS, EvidenceReviewer
from app.services.evidence.store import EvidenceStore
from app.services.evidence.synthesizer import SynthesizerAgent
from app.services.research.pubmed import parse_pubmed_articles
from app.services.sqlite_repo import LocalSQLiteContentRepository
from app.services.tip_publisher import TipPublisher

CORPUS = Path(__file__).parent / "fixtures" / "corpus"

#: An advisory reply that raises no objection.
_CLEAN_REVIEW = {name: False for name in REVIEW_QUESTIONS}
NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# Corpus loading
# ----------------------------------------------------------------------


def _case_names() -> list[str]:
    return sorted(path.name for path in CORPUS.iterdir() if path.is_dir())


def load_case(name: str) -> tuple[EvidenceRecord, dict[str, Any]]:
    """Return the parsed record and its expected labels."""

    folder = CORPUS / name
    record = parse_pubmed_articles((folder / "record.xml").read_text(encoding="utf-8"))[0]
    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
    return record, expected


class StubLLM:
    def __init__(self, payload: Any) -> None:
        self._payload = payload if isinstance(payload, str) else json.dumps(payload)

    def invoke(self, _input: Any, **_kwargs: Any) -> AIMessage:
        return AIMessage(content=self._payload)


def _approved(name: str, **overrides: Any) -> EvidenceRecord:
    record, _ = load_case(name)
    record.state = "approved"
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


def _extract(record: EvidenceRecord, payload: dict[str, Any]) -> EvidenceRecord:
    extracted = ExtractorAgent(llm=StubLLM(payload), model_id="benchmark").extract(record)
    extracted.state = "approved"
    return extracted


def _bundle(*claims: Claim, grade: str = "insufficient", topic: str = "topic") -> EvidenceBundle:
    return EvidenceBundle(
        bundle_id="benchmark", topic_key=topic, claims=list(claims), grade=grade
    )


def _review(bundle: EvidenceBundle, *records: EvidenceRecord, llm: Any = None):
    reviewer = EvidenceReviewer(llm=llm, model_id="benchmark", now=lambda: NOW)
    return reviewer.review(bundle, {record.source_key: record for record in records})


# ----------------------------------------------------------------------
# The corpus itself
# ----------------------------------------------------------------------


def test_the_corpus_is_present_and_covers_the_named_failure_modes() -> None:
    """A corpus that only holds cases the system already handles stops being a test."""

    names = set(_case_names())

    assert {
        "strong_human_rct",
        "observational_association",
        "animal_study",
        "in_vitro",
        "systematic_review",
        "meta_analysis",
        "tiny_exploratory",
        "preprint",
        "surrogate_endpoint",
        "contradicting_trial",
        "retracted",
        "corrected",
        "trivial_effect",
        "unclassifiable",
    } <= names


@pytest.mark.parametrize("name", _case_names())
def test_every_corpus_case_classifies_as_expected(name: str) -> None:
    """Classification comes from indexed metadata, so it must be exactly reproducible."""

    record, expected = load_case(name)

    assert record.source_key == expected["source_key"]
    assert record.classification.design == expected["design"]
    assert record.classification.subject == expected["subject"]
    assert record.retraction_state == expected["retraction_state"]


# ----------------------------------------------------------------------
# Invariant 1 — an unacquired source never reaches publication (G1)
# ----------------------------------------------------------------------


def test_invariant_1_an_unknown_source_key_cannot_be_published() -> None:
    record = _approved("strong_human_rct")
    bundle = _bundle(Claim(text="A claim.", evidence_keys=["doi:10.9999/invented"]))

    decision = _review(bundle, record)

    assert decision.is_approved is False
    assert any(violation.gate == "G1" for violation in decision.violations)


# ----------------------------------------------------------------------
# Invariant 2 — every published number resolves to a verifying span (G2, I2)
# ----------------------------------------------------------------------


def test_invariant_2_a_number_without_a_span_cannot_be_published() -> None:
    record = _approved("strong_human_rct")
    bundle = _bundle(
        Claim(text="Mortality fell by 4.2 percent.", evidence_keys=[record.source_key])
    )

    decision = _review(bundle, record)

    assert decision.is_approved is False
    assert any(violation.gate == "G2" for violation in decision.violations)


def test_invariant_2_an_anchored_number_survives_and_still_verifies() -> None:
    record = _extract(
        _approved("strong_human_rct"),
        {"sample_size": {"value": 1240, "quote": "1240 adults"}},
    )
    span = record.sample_size.span
    assert span is not None

    bundle = _bundle(
        Claim(
            text="The trial randomised 1240 adults.",
            evidence_keys=[record.source_key],
            numbers=[NumberRef(text="1240", source_key=record.source_key, span=span)],
        )
    )

    assert not any(v.gate == "G2" for v in run_gates(bundle, {record.source_key: record}))
    assert span.verify(record.document_text)


# ----------------------------------------------------------------------
# Invariant 3 — animal evidence stays animal evidence (G3)
# ----------------------------------------------------------------------


def test_invariant_3_a_mouse_study_cannot_speak_about_people() -> None:
    record = _approved("animal_study")
    bundle = _bundle(
        Claim(text="People who take it live 18 percent longer.", evidence_keys=[record.source_key])
    )

    decision = _review(bundle, record)

    assert decision.is_approved is False
    assert any(violation.gate == "G3" for violation in decision.violations)


def test_invariant_3_the_same_finding_publishes_when_stated_honestly() -> None:
    record = _approved("animal_study")
    bundle = _bundle(
        Claim(text="Treated mice lived longer than controls.", evidence_keys=[record.source_key])
    )

    decision = _review(bundle, record)

    assert decision.is_approved is True
    assert decision.grade == "preliminary"


# ----------------------------------------------------------------------
# Invariant 4 — observational evidence never becomes causal (G4)
# ----------------------------------------------------------------------


def test_invariant_4_a_cohort_cannot_carry_causal_language() -> None:
    record = _approved("observational_association")
    bundle = _bundle(
        Claim(
            text="Brisk walking reduces mortality.",
            claim_type="causal",
            evidence_keys=[record.source_key],
        )
    )

    decision = _review(bundle, record)

    assert decision.is_approved is False
    assert any(violation.gate == "G4" for violation in decision.violations)


def test_invariant_4_causal_language_reintroduced_by_editing_is_caught() -> None:
    """The claim-level gate saw the draft; this is what the reader would have got."""

    record = _approved("observational_association")
    bundle = _bundle(
        Claim(
            text="Brisk walking was associated with lower mortality.",
            evidence_keys=[record.source_key],
        )
    )

    violations = recheck_published_text(
        "Brisk walking reduces mortality.", bundle, {record.source_key: record}
    )

    assert any(violation.gate == "G4" for violation in violations)


# ----------------------------------------------------------------------
# Invariant 5 — a retraction blocks publication, and reaches what used it (G6)
# ----------------------------------------------------------------------


def test_invariant_5_a_retracted_source_blocks_publication() -> None:
    record = _approved("retracted")
    bundle = _bundle(Claim(text="A claim.", evidence_keys=[record.source_key]))

    decision = _review(bundle, record)

    assert decision.is_approved is False
    assert decision.grade == "insufficient"
    assert any(violation.gate == "G6" for violation in decision.violations)


def test_invariant_5_usage_can_find_content_that_already_cited_it(tmp_path: Path) -> None:
    """The maintenance sweep (item 12) is not built; the mechanism it needs is."""

    record = _approved("retracted")
    with EvidenceStore(tmp_path / "content.db") as store:
        store.upsert_record(record)
        store.record_usage(
            source_keys=[record.source_key],
            content_type="article",
            content_id="article-1",
            topic_key="t",
            used_at=NOW,
        )
        store.set_retraction(record.source_key, "retracted")

        affected = store.usage_for_source(record.source_key)

    assert [entry["content_id"] for entry in affected] == ["article-1"]


def test_invariant_5_a_correction_is_not_a_block() -> None:
    record = _approved("corrected")
    bundle = _bundle(
        Claim(
            text="Seven hours of sleep was associated with better cognition.",
            evidence_keys=[record.source_key],
        )
    )

    assert _review(bundle, record).is_approved is True


# ----------------------------------------------------------------------
# Invariant 6 — unknown design or subject yields insufficient (G10, I3)
# ----------------------------------------------------------------------


def test_invariant_6_an_unclassifiable_source_cannot_support_a_claim() -> None:
    record = _approved("unclassifiable")
    bundle = _bundle(Claim(text="A claim.", evidence_keys=[record.source_key]))

    decision = _review(bundle, record)

    assert decision.grade == "insufficient"
    assert decision.is_approved is False
    assert any(violation.gate == "G10" for violation in decision.violations)


def test_invariant_6_an_unextractable_field_stays_unknown() -> None:
    """A quote that is not in the document does not become a value (I3)."""

    record = _extract(
        _approved("strong_human_rct"),
        {"sample_size": {"value": 5000, "quote": "we enrolled 5000 volunteers"}},
    )

    assert record.sample_size.status == "not_extractable"
    assert record.sample_size.value is None


# ----------------------------------------------------------------------
# Invariant 7 — a reviewer rejection prevents publication (I5)
# ----------------------------------------------------------------------


def test_invariant_7_a_reviewer_refusal_stops_publication() -> None:
    record = _approved("strong_human_rct")
    bundle = _bundle(
        Claim(text="The trial reported lower mortality.", evidence_keys=[record.source_key])
    )
    objecting = StubLLM({**_CLEAN_REVIEW, "overstates_evidence": True})

    decision = _review(bundle, record, llm=objecting)

    assert decision.status == "regenerate"
    assert decision.is_approved is False


# ----------------------------------------------------------------------
# Invariant 8 — a model may not raise a grade (I4)
# ----------------------------------------------------------------------


def test_invariant_8_a_model_grade_above_the_computed_one_is_discarded() -> None:
    record = _approved("observational_association")
    bundle = _bundle(
        Claim(
            text="Brisk walking was associated with lower mortality.",
            evidence_keys=[record.source_key],
        )
    )
    optimistic = StubLLM({**_CLEAN_REVIEW, "grade": "high"})

    decision = _review(bundle, record, llm=optimistic)

    assert decision.grade != "high"
    assert clamp_grade("high", decision.grade) == decision.grade


# ----------------------------------------------------------------------
# Invariant 9 — tip provenance survives persistence (item 4)
# ----------------------------------------------------------------------


def test_invariant_9_a_published_tip_can_still_name_its_sources(tmp_path: Path) -> None:
    repository = LocalSQLiteContentRepository(tmp_path / "content.db")
    draft = TipDraft(
        title="A tip",
        body="A short, practical suggestion.",
        source_urls=["https://doi.org/10.1001/corpus.rct"],
        evidence_bundle_id="benchmark",
        evidence_keys=["doi:10.1001/corpus.rct"],
        evidence_grade="moderate",
        evidence_summary="Moderate — 1 human randomised trial",
    )

    stored = TipPublisher(repository=repository).publish(draft, published_at=NOW).tip
    reloaded: Tip | None = repository.get_tip(stored.id)

    assert reloaded is not None
    assert reloaded.evidence_keys == ["doi:10.1001/corpus.rct"]
    assert reloaded.evidence_grade == "moderate"
    assert reloaded.source_urls == ["https://doi.org/10.1001/corpus.rct"]


# ----------------------------------------------------------------------
# Invariant 10 — contradictions are surfaced, not averaged (item 2b)
# ----------------------------------------------------------------------


def test_invariant_10_disagreement_is_recorded_and_caps_the_grade() -> None:
    positive = _approved("strong_human_rct")
    null = _approved("contradicting_trial")

    agent = SynthesizerAgent(
        llm=StubLLM(
            {
                "claims": [
                    {
                        "text": "One trial reported lower mortality.",
                        "claim_type": "causal",
                        "evidence": ["E1"],
                        "contradicts": ["E2"],
                    }
                ]
            }
        ),
        model_id="benchmark",
        bundle_id_factory=lambda: "benchmark",
    )

    bundle = agent.synthesize([positive, null])
    records = {record.source_key: record for record in (positive, null)}
    grade, rationale = compute_grade(bundle, records)

    assert bundle.claims[0].contradicted_by == [null.source_key]
    assert grade != "high"
    assert any("not settled" in line for line in rationale)


# ----------------------------------------------------------------------
# Invariant 11 — the claim ceiling holds at any grade (G8)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Take 500 mg of magnesium daily.",
        "This cures type 2 diabetes.",
        "Stop taking your statins once your numbers improve.",
        "If you have diabetes, you should avoid this.",
    ],
)
@pytest.mark.parametrize("case", ["meta_analysis", "animal_study"])
def test_invariant_11_ceiling_breaches_are_refused_at_every_grade(text: str, case: str) -> None:
    record = _approved(case)
    bundle = _bundle(Claim(text=text, evidence_keys=[record.source_key]))

    decision = _review(bundle, record)

    assert decision.is_approved is False
    assert any(violation.gate == "G8" for violation in decision.violations)


# ----------------------------------------------------------------------
# Invariant 12 — strength beats recency (item 7)
# ----------------------------------------------------------------------


def test_invariant_12_a_meta_analysis_outranks_a_newer_mouse_study() -> None:
    from datetime import timedelta

    from app.services.evidence.clustering import cluster_records
    from app.services.evidence.ranking import rank_clusters

    meta = _extract(
        _approved("meta_analysis", source_published_at=NOW - timedelta(days=2)),
        {
            "intervention": {"value": "resistance training", "quote": "training"},
            # A clinical endpoint, so this is genuinely the strong case the invariant
            # is about. Without one the rubric grades it moderate, correctly.
            "outcomes": [
                {
                    "name": "all-cause mortality",
                    "is_surrogate": {"value": False, "quote": "All-cause mortality"},
                    "magnitude": {"value": 12, "quote": "12 percent lower"},
                }
            ],
        },
    )
    mouse = _extract(
        _approved("animal_study", source_published_at=NOW),
        {"intervention": {"value": "rapamycin", "quote": "Rapamycin"}},
    )

    ranked = rank_clusters(cluster_records([mouse, meta]), now=NOW)

    assert ranked[0].cluster.records[0].source_key == meta.source_key
    assert ranked[0].grade == "high"


# ----------------------------------------------------------------------
# Invariant 13 — every failure state publishes nothing (item 9, I5)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("outcome", list(RunOutcome))
def test_invariant_13_no_outcome_other_than_published_means_published(
    outcome: RunOutcome,
) -> None:
    policy = OUTCOME_POLICY[outcome]

    if outcome is RunOutcome.PUBLISHED:
        assert policy.stamp is True
    else:
        # Nothing here is a route to publishing: the outcome either satisfies the
        # cadence having published nothing, or backs off to try again.
        assert policy.stamp is not None
        assert policy.retry is (policy.stamp is False)


def test_invariant_13_a_failed_run_publishes_nothing(tmp_path: Path) -> None:
    from app.services.evidence.reviewer import EvidenceReviewer as Reviewer
    from app.services.evidence_pipeline import EvidencePipeline, run_article
    from app.services.research.http import ResearchRequestError

    class Failing:
        def search_records(self, query: str, *, max_results: int = 20):
            raise ResearchRequestError("source down")

    published: list[Any] = []

    with EvidenceStore(tmp_path / "content.db") as store:
        pipeline = EvidencePipeline(
            store=store,
            acquirer=Failing(),
            extractor=ExtractorAgent(llm=StubLLM({}), model_id="benchmark"),
            synthesizer=SynthesizerAgent(llm=StubLLM({"claims": []}), model_id="benchmark"),
            reviewer=Reviewer(model_id="benchmark", now=lambda: NOW),
            queries=["anything"],
            now=lambda: NOW,
        )
        result = run_article(
            pipeline,
            _writer(),
            lambda draft, bundle, records: published.append(draft),
        )

    assert result.outcome is RunOutcome.RETRIEVAL_FAILED
    assert published == []


def _writer():
    from app.services.evidence.writers import ArticleWriter

    return ArticleWriter(llm=StubLLM({"title": "T", "body": "B"}), model_id="benchmark")
