"""Tests for the bundle writers and the post-edit re-check.

A writer works inside walls: it may arrange and explain, never add. What these tests
check is that the walls hold when the model pushes on them — an invented handle, an
invented figure, a causal verb the evidence does not license.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

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
from app.services.evidence.citations import citation_url, strip_handles
from app.services.evidence.postedit import feedback_for, recheck_published_text
from app.services.evidence.writers import ArticleWriter, TipWriter, evidence_fields

KEY = "doi:10.1001/jama.2026.1"
DOCUMENT = "We randomised 412 adults and mortality fell by 4.2 percent."


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


def _span(quote: str) -> Span:
    span = Span.locate(DOCUMENT, quote)
    assert span is not None
    return span


def _record(design: str = "rct", subject: str = "human") -> EvidenceRecord:
    return EvidenceRecord(
        source_key=KEY,
        title="Time-restricted eating and mortality",
        document_text=DOCUMENT,
        classification=Classification(design=design, subject=subject),
        sample_size=Extracted.found(412, _span("412 adults")),
        state="approved",
    )


def _bundle(grade: str = "moderate", **overrides) -> EvidenceBundle:
    claim = overrides.pop(
        "claim",
        Claim(
            text="Mortality fell by 4.2 percent.",
            claim_type="causal",
            evidence_keys=[KEY],
            numbers=[NumberRef(text="4.2", source_key=KEY, span=_span("4.2 percent"))],
            limitations=["single trial"],
        ),
    )
    defaults = dict(bundle_id="b1", topic_key="tre|mortality", claims=[claim], grade=grade)
    defaults.update(overrides)
    return EvidenceBundle(**defaults)


def _records() -> dict[str, EvidenceRecord]:
    return {KEY: _record()}


# -- provenance --------------------------------------------------------


def test_evidence_fields_are_built_from_the_bundle_not_the_model() -> None:
    fields = evidence_fields(_bundle(), _records())

    assert fields["evidence_bundle_id"] == "b1"
    assert fields["evidence_keys"] == [KEY]
    assert fields["evidence_grade"] == "moderate"
    assert fields["evidence_summary"] == "Moderate — 1 human randomised trial"
    assert fields["source_urls"] == ["https://doi.org/10.1001/jama.2026.1"]


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("doi:10.1/x", "https://doi.org/10.1/x"),
        ("pmid:38412345", "https://pubmed.ncbi.nlm.nih.gov/38412345/"),
        ("pmcid:PMC1", "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1/"),
        ("nct:NCT01234567", "https://clinicaltrials.gov/study/NCT01234567"),
        ("not-a-key", ""),
    ],
)
def test_citation_urls_are_derived_from_the_identifier(key: str, expected: str) -> None:
    assert citation_url(key) == expected


# -- the article writer ------------------------------------------------


def test_an_article_is_written_from_the_claims_with_code_supplied_sources() -> None:
    stub = StubLLM(
        {
            "title": "What an eight-hour window does",
            "summary": "A trial found lower mortality.",
            "body": "Mortality fell by 4.2 percent [E1]. The trial was small.",
            "takeaways": ["Mortality fell by 4.2 percent [E1]"],
            "tags": ["nutrition"],
        }
    )

    draft = ArticleWriter(llm=stub).write(_bundle(), _records())

    assert draft.title == "What an eight-hour window does"
    assert "[E1]" not in draft.body
    assert "4.2 percent" in draft.body
    assert draft.sources == ["https://doi.org/10.1001/jama.2026.1"]
    assert draft.takeaways == ["Mortality fell by 4.2 percent"]


def test_a_writer_supplied_url_is_ignored_entirely() -> None:
    """Sources come from the bundle. There is no path for a model to add one."""

    stub = StubLLM(
        {
            "title": "T",
            "body": "Body.",
            "sources": ["https://invented.example.com/study"],
        }
    )

    draft = ArticleWriter(llm=stub).write(_bundle(), _records())

    assert draft.sources == ["https://doi.org/10.1001/jama.2026.1"]


def test_the_writer_prompt_carries_claims_and_handles_but_no_urls() -> None:
    stub = StubLLM({"title": "T", "body": "B"})

    ArticleWriter(llm=stub).write(_bundle(), _records())

    prompt = " ".join(str(getattr(message, "content", message)) for message in stub.calls[0])
    assert "Mortality fell by 4.2 percent." in prompt
    assert "[E1]" in prompt
    assert "single trial" in prompt
    assert "http" not in prompt
    assert "doi:" not in prompt


def test_an_invented_handle_is_removed_rather_than_repaired() -> None:
    stub = StubLLM({"title": "T", "body": "A claim [E1] and another [E7]."})

    draft = ArticleWriter(llm=stub).write(_bundle(), _records())

    assert "E7" not in draft.body
    assert "E1" not in draft.body


# -- the tip writer ----------------------------------------------------


def test_a_tip_carries_the_same_provenance_as_an_article() -> None:
    stub = StubLLM(
        {
            "title": "Try an eight-hour eating window",
            "body": "Keeping meals inside eight hours was studied in a trial [E1].",
            "tags": ["nutrition"],
        }
    )

    draft = TipWriter(llm=stub).write(_bundle(), _records())

    assert draft.evidence_bundle_id == "b1"
    assert draft.evidence_keys == [KEY]
    assert draft.evidence_grade == "moderate"
    assert draft.evidence_summary == "Moderate — 1 human randomised trial"
    assert draft.source_urls == ["https://doi.org/10.1001/jama.2026.1"]
    assert "[E1]" not in draft.body


def test_both_writers_read_the_same_bundle() -> None:
    """Item 5: an article and a tip can no longer disagree about a finding."""

    article = ArticleWriter(llm=StubLLM({"title": "T", "body": "Mortality fell by 4.2 percent."}))
    tip = TipWriter(llm=StubLLM({"title": "T", "body": "Mortality fell by 4.2 percent."}))
    bundle, records = _bundle(), _records()

    article_draft = article.write(bundle, records)
    tip_draft = tip.write(bundle, records)

    assert tip_draft.evidence_keys == evidence_fields(bundle, records)["evidence_keys"]
    assert article_draft.sources == tip_draft.source_urls


# -- the post-edit re-check --------------------------------------------


def test_clean_edited_text_passes() -> None:
    text = "Mortality fell by 4.2 percent in a randomised trial."

    assert recheck_published_text(text, _bundle(), _records()) == []


def test_a_number_the_editor_introduced_is_caught() -> None:
    """Editing is exactly where a figure acquires a decimal place it never had."""

    violations = recheck_published_text(
        "Mortality fell by 4.25 percent.", _bundle(), _records()
    )

    assert violations and violations[0].gate == "G2"
    assert "4.25" in violations[0].detail


def test_a_causal_verb_reintroduced_by_editing_is_caught() -> None:
    """The claim-level gate saw the draft; this sees what the reader will."""

    records = {KEY: _record(design="prospective_cohort")}
    bundle = _bundle(
        claim=Claim(
            text="Time-restricted eating was associated with lower mortality.",
            claim_type="associative",
            evidence_keys=[KEY],
        )
    )

    violations = recheck_published_text(
        "Time-restricted eating reduces mortality.", bundle, records
    )

    assert violations and violations[0].gate == "G4"


def test_hedged_prose_on_observational_evidence_still_passes() -> None:
    records = {KEY: _record(design="prospective_cohort")}
    bundle = _bundle(claim=Claim(text="An association.", evidence_keys=[KEY]))

    text = "Time-restricted eating was associated with lower mortality."

    assert recheck_published_text(text, bundle, records) == []


def test_causal_prose_is_allowed_when_the_evidence_is_randomised() -> None:
    text = "The schedule reduces mortality."

    assert recheck_published_text(text, _bundle(), _records()) == []


def test_the_claim_ceiling_applies_to_the_final_text() -> None:
    violations = recheck_published_text(
        "Take 500 mg of magnesium daily.", _bundle(), _records()
    )

    # Both fire, correctly: the dose is a ceiling breach *and* a figure the bundle
    # never anchored.
    assert {violation.gate for violation in violations} == {"G2", "G8"}
    assert any("dosing" in violation.detail for violation in violations)


def test_the_ceiling_uses_the_bundle_grade() -> None:
    text = "The benefit is proven."

    assert recheck_published_text(text, _bundle(grade="moderate"), _records()) != []
    assert recheck_published_text(text, _bundle(grade="high"), _records()) == []


def test_years_in_edited_prose_are_not_treated_as_findings() -> None:
    assert recheck_published_text("A 2026 trial found this.", _bundle(), _records()) == []


def test_feedback_names_what_went_wrong() -> None:
    violations = recheck_published_text("Mortality fell by 9.9 percent.", _bundle(), _records())

    feedback = feedback_for(violations)

    assert "9.9" in feedback
    assert "Rewrite" in feedback
    assert feedback_for([]) == ""


# -- handle stripping --------------------------------------------------


def test_stripping_handles_tidies_the_gaps_it_leaves() -> None:
    assert strip_handles("Glucose fell [E1] . Next  sentence [E2].") == (
        "Glucose fell. Next sentence."
    )


def test_stripping_handles_preserves_paragraphs() -> None:
    assert strip_handles("One [E1].\n\nTwo [E2].") == "One.\n\nTwo."
