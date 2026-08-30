"""Tests for provenance as identity.

Two mechanisms are covered: the opaque handles a writer cites through (which it cannot
invent), and the persistence path that used to drop a tip's sources at the database
boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.models.content import Article, Tip
from app.models.editor import allowlisted_sources, rejected_sources
from app.models.tip import TipDraft
from app.services.evidence.citations import (
    EvidenceHandles,
    allowlisted_evidence,
    rejected_evidence,
)
from app.services.sqlite_repo import LocalSQLiteContentRepository
from app.services.tip_publisher import TipPublisher

KEYS = ["doi:10.1001/jama.2024.1234", "pmid:38412345"]


# -- handles -----------------------------------------------------------


def test_handles_are_issued_in_order_and_resolve_back_to_keys() -> None:
    handles = EvidenceHandles.for_keys(KEYS)

    assert handles.by_handle == {"E1": KEYS[0], "E2": KEYS[1]}
    assert handles.resolve("E1") == KEYS[0]
    assert handles.resolve("[E2]") == KEYS[1]
    assert handles.handle_for(KEYS[1]) == "E2"


def test_duplicate_and_blank_keys_are_dropped_when_issuing() -> None:
    handles = EvidenceHandles.for_keys([KEYS[0], KEYS[0], "", "  ", KEYS[1]])

    assert handles.source_keys == KEYS


def test_a_handle_nobody_issued_resolves_to_nothing() -> None:
    """This is what makes an invented citation structurally impossible."""

    handles = EvidenceHandles.for_keys(KEYS)

    assert handles.resolve("E9") is None

    resolved, unknown = handles.resolve_all("Fasting helps [E1], and so does exercise [E9].")

    assert resolved == [KEYS[0]]
    assert unknown == ["E9"]


def test_handles_are_collected_in_order_of_first_appearance() -> None:
    handles = EvidenceHandles.for_keys(KEYS)

    assert handles.found_in("[E2] then [E1] then [E2] again") == ["E2", "E1"]
    assert handles.found_in("") == []


def test_the_prompt_block_carries_titles_but_never_urls() -> None:
    """A model that never sees a URL cannot echo a mangled version of one."""

    handles = EvidenceHandles.for_keys(KEYS)

    block = handles.prompt_block({KEYS[0]: "Time-restricted eating and cardiometabolic risk"})

    assert "[E1] Time-restricted eating and cardiometabolic risk" in block
    assert "http" not in block
    assert EvidenceHandles.for_keys([]).prompt_block() == "No evidence available."


# -- allowlists --------------------------------------------------------


def test_only_issued_keys_survive_the_allowlist() -> None:
    kept = allowlisted_evidence(KEYS, ["doi:10.9999/invented", KEYS[1]])

    assert kept == KEYS
    assert rejected_evidence(KEYS, ["doi:10.9999/invented"]) == ["doi:10.9999/invented"]


def test_issued_keys_are_preserved_even_when_the_model_omits_them() -> None:
    assert allowlisted_evidence(KEYS, []) == KEYS


def test_the_url_allowlist_still_ignores_a_trailing_slash() -> None:
    """The editor's URL rule is now a specialisation of the same function."""

    feed = ["https://example.com/study"]

    assert allowlisted_sources(feed, ["https://example.com/study/"]) == feed
    assert rejected_sources(feed, ["https://example.com/study/"]) == []
    assert rejected_sources(feed, ["https://example.com/invented"]) == [
        "https://example.com/invented"
    ]


# -- persistence -------------------------------------------------------


def _repository(tmp_path: Path) -> LocalSQLiteContentRepository:
    return LocalSQLiteContentRepository(tmp_path / "content.db")


def test_tip_provenance_survives_persistence(tmp_path: Path) -> None:
    """Tips used to lose their sources entirely between the draft and the database."""

    repository = _repository(tmp_path)
    draft = TipDraft(
        title="Eat within an eight-hour window",
        body="Keeping meals inside one eight-hour window supported glucose control in a trial.",
        tags=["nutrition"],
        source_urls=["https://doi.org/10.1001/jama.2024.1234"],
        evidence_bundle_id="bundle-1",
        evidence_keys=KEYS,
        evidence_grade="moderate",
        evidence_summary="one human RCT",
    )

    result = TipPublisher(repository=repository).publish(
        draft, published_at=datetime(2026, 8, 30, tzinfo=timezone.utc)
    )

    stored = repository.get_tip(result.tip.id)
    assert stored is not None
    assert stored.evidence_keys == KEYS
    assert stored.evidence_grade == "moderate"
    assert stored.evidence_bundle_id == "bundle-1"
    assert stored.evidence_summary == "one human RCT"
    assert stored.source_urls == ["https://doi.org/10.1001/jama.2024.1234"]
    assert stored.evidence_assessed is True


def test_article_provenance_survives_persistence(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    article = Article(
        title="What an eight-hour eating window does",
        content_body="A randomised trial reported lower fasting glucose.",
        summary="A trial of time-restricted eating.",
        source_urls=["https://doi.org/10.1001/jama.2024.1234"],
        evidence_bundle_id="bundle-1",
        evidence_keys=KEYS,
        evidence_grade="moderate",
        evidence_summary="one human RCT plus supporting cohort evidence",
    )

    saved = repository.save_article(article)
    stored = repository.get_article(saved.id)

    assert stored is not None
    assert stored.evidence_keys == KEYS
    assert stored.evidence_grade == "moderate"
    assert stored.evidence_assessed is True


def test_content_published_before_the_evidence_layer_is_unassessed_not_graded() -> None:
    """Legacy rows are grandfathered with a badge, never retro-graded on no information."""

    legacy_article = Article.from_document({"title": "Old", "content_body": "text"})
    legacy_tip = Tip.from_document({"title": "Old", "content_body": "text"})

    assert legacy_article.evidence_assessed is False
    assert legacy_article.evidence_keys == []
    assert legacy_article.evidence_grade is None
    assert legacy_tip.evidence_assessed is False
    assert legacy_tip.source_urls == []


def test_the_draft_normaliser_trims_provenance_fields() -> None:
    draft = TipDraft(
        title=" Tip ",
        body=" body ",
        source_urls=["  https://example.com/study  ", "", "   "],
        evidence_keys=[" doi:10.1/x ", ""],
        evidence_grade="  moderate ",
        evidence_bundle_id="   ",
    ).with_defaults()

    assert draft.source_urls == ["https://example.com/study"]
    assert draft.evidence_keys == ["doi:10.1/x"]
    assert draft.evidence_grade == "moderate"
    assert draft.evidence_bundle_id is None


# -- stored strings are readable ---------------------------------------


def test_invisible_characters_are_stripped_from_tags() -> None:
    """A zero-width space renders as an empty box beside the tag on the site."""

    article = Article.from_document(
        {"title": "T", "content_body": "B", "tags": ["longevity​", "▪ healthy aging"]}
    )

    assert article.tags == ["longevity", "healthy aging"]


def test_a_model_bullet_is_not_part_of_the_tag() -> None:
    article = Article.from_document(
        {"title": "T", "content_body": "B", "tags": ["- posture", "* aging", "• sleep"]}
    )

    assert article.tags == ["posture", "aging", "sleep"]


def test_cleaning_leaves_hyphenated_words_and_identifiers_alone() -> None:
    """The rule is about decoration, and a DOI may legitimately end in a hyphen."""

    article = Article.from_document(
        {
            "title": "T",
            "content_body": "B",
            "tags": ["annual check-ups", "omega-3"],
            "source_urls": ["https://doi.org/10.1016/s0140-6736(97)11096-0"],
            "evidence_keys": ["doi:10.1016/s0140-6736(97)11096-0"],
        }
    )

    assert article.tags == ["annual check-ups", "omega-3"]
    assert article.source_urls == ["https://doi.org/10.1016/s0140-6736(97)11096-0"]
    assert article.evidence_keys == ["doi:10.1016/s0140-6736(97)11096-0"]


def test_a_tag_that_was_only_decoration_disappears() -> None:
    article = Article.from_document({"title": "T", "content_body": "B", "tags": ["•", "  ", "ok"]})

    assert article.tags == ["ok"]
