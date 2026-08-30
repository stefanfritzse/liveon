"""Tests for the maintenance sweep.

With nobody reviewing what this system publishes, automatic correction is the only
correction it has. These tests are about the case that matters: a paper the site has
already written from is retracted afterwards, and the site has to notice and say so.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.content import Article, Tip
from app.models.evidence import (
    Claim,
    Classification,
    EvidenceBundle,
    EvidenceRecord,
)
from app.models.run_outcome import RunOutcome
from app.services.evidence.maintenance import MaintenanceSweep, retraction_policy
from app.services.evidence.store import EvidenceStore
from app.services.sqlite_repo import LocalSQLiteContentRepository

NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)
KEY = "pmid:38412345"
DOCUMENT = "We randomised 412 adults and mortality fell by 4.2 percent."


class _Refetcher:
    """Returns records as PubMed would now describe them."""

    def __init__(self, records: list[EvidenceRecord] | None = None, error: Exception | None = None):
        self.records = records or []
        self.error = error
        self.asked: list[list[str]] = []

    def fetch(self, pmids):
        self.asked.append(list(pmids))
        if self.error is not None:
            raise self.error
        return self.records


def _record(key: str = KEY, *, retraction: str = "none", notes=()) -> EvidenceRecord:
    return EvidenceRecord(
        source_key=key,
        title="A trial",
        aliases=[key],
        document_text=DOCUMENT,
        classification=Classification(design="rct", subject="human"),
        retraction_state=retraction,
        retraction_notes=list(notes),
        state="approved",
    )


@pytest.fixture()
def store(tmp_path: Path) -> EvidenceStore:
    with EvidenceStore(tmp_path / "content.db") as opened:
        yield opened


@pytest.fixture()
def repository(tmp_path: Path) -> LocalSQLiteContentRepository:
    return LocalSQLiteContentRepository(tmp_path / "content.db")


def _published(store: EvidenceStore, repository: LocalSQLiteContentRepository) -> str:
    """Publish an article from a source, exactly as the pipeline would."""

    store.upsert_record(_record())
    article = repository.save_article(
        Article(
            title="What the trial found",
            content_body="Mortality fell by 4.2 percent.",
            published_date=NOW,
            evidence_bundle_id="bundle-1",
            evidence_keys=[KEY],
            evidence_grade="moderate",
        )
    )
    store.record_usage(
        source_keys=[KEY],
        content_type="article",
        content_id=article.id,
        bundle_id="bundle-1",
        topic_key="fasting|mortality",
        used_at=NOW,
    )
    return article.id


# -- policy ------------------------------------------------------------


def test_annotating_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A silently vanished article is indistinguishable from one that never existed."""

    monkeypatch.delenv("LIVEON_RETRACTION_POLICY", raising=False)
    assert retraction_policy() == "annotate"

    monkeypatch.setenv("LIVEON_RETRACTION_POLICY", "unpublish")
    assert retraction_policy() == "unpublish"

    monkeypatch.setenv("LIVEON_RETRACTION_POLICY", "nonsense")
    assert retraction_policy() == "annotate"


# -- the retraction sweep ----------------------------------------------


def test_a_retraction_reaches_the_article_written_from_it(
    store: EvidenceStore, repository: LocalSQLiteContentRepository
) -> None:
    """The whole point of the sweep, end to end."""

    article_id = _published(store, repository)
    sweep = MaintenanceSweep(
        store=store,
        repository=repository,
        acquirer=_Refetcher([_record(retraction="retracted", notes=["RetractionIn: JAMA"])]),
        now=lambda: NOW,
    )

    report = sweep.run()

    assert report.retracted == [KEY]
    assert store.get_record(KEY).is_retracted is True

    article = repository.get_article(article_id)
    assert "has since been retracted" in article.correction_notice
    assert article.withdrawn is False  # annotate is the default


def test_unpublishing_takes_it_off_the_site_without_deleting_it(
    store: EvidenceStore, repository: LocalSQLiteContentRepository
) -> None:
    article_id = _published(store, repository)
    sweep = MaintenanceSweep(
        store=store,
        repository=repository,
        acquirer=_Refetcher([_record(retraction="retracted")]),
        now=lambda: NOW,
        policy="unpublish",
    )

    report = sweep.run()

    assert report.withdrawn == [article_id]
    article = repository.get_article(article_id)
    assert article.withdrawn is True
    assert article.correction_notice  # the record of what was said survives
    assert repository.browse_articles().total == 0


def test_an_expression_of_concern_annotates_but_never_withdraws(
    store: EvidenceStore, repository: LocalSQLiteContentRepository
) -> None:
    """A caveat is not a reason to erase what was said."""

    article_id = _published(store, repository)
    sweep = MaintenanceSweep(
        store=store,
        repository=repository,
        acquirer=_Refetcher([_record(retraction="concern")]),
        now=lambda: NOW,
        policy="unpublish",
    )

    sweep.run()

    article = repository.get_article(article_id)
    assert "expression of concern" in article.correction_notice
    assert article.withdrawn is False


def test_a_correction_is_noted_more_softly(
    store: EvidenceStore, repository: LocalSQLiteContentRepository
) -> None:
    article_id = _published(store, repository)
    sweep = MaintenanceSweep(
        store=store,
        repository=repository,
        acquirer=_Refetcher([_record(retraction="corrected")]),
        now=lambda: NOW,
    )

    report = sweep.run()

    assert report.corrected == [KEY]
    assert "corrected by its authors" in repository.get_article(article_id).correction_notice


def test_an_unchanged_source_changes_nothing(
    store: EvidenceStore, repository: LocalSQLiteContentRepository
) -> None:
    article_id = _published(store, repository)
    sweep = MaintenanceSweep(
        store=store, repository=repository, acquirer=_Refetcher([_record()]), now=lambda: NOW
    )

    report = sweep.run()

    assert report.acted is False
    assert repository.get_article(article_id).correction_notice is None


def test_only_cited_sources_are_re_checked(
    store: EvidenceStore, repository: LocalSQLiteContentRepository
) -> None:
    """There is no point re-querying a paper nothing was ever written from."""

    _published(store, repository)
    store.upsert_record(_record("pmid:99999999"))
    refetcher = _Refetcher([_record()])

    MaintenanceSweep(store=store, repository=repository, acquirer=refetcher, now=lambda: NOW).run()

    assert refetcher.asked == [["38412345"]]


def test_an_unreachable_source_is_not_treated_as_a_retraction(
    store: EvidenceStore, repository: LocalSQLiteContentRepository
) -> None:
    """Silence from PubMed says nothing about the paper."""

    article_id = _published(store, repository)
    sweep = MaintenanceSweep(
        store=store,
        repository=repository,
        acquirer=_Refetcher(error=RuntimeError("network down")),
        now=lambda: NOW,
    )

    report = sweep.run()

    assert report.errors
    assert report.retracted == []
    assert repository.get_article(article_id).correction_notice is None


def test_the_sweep_is_idempotent(
    store: EvidenceStore, repository: LocalSQLiteContentRepository
) -> None:
    article_id = _published(store, repository)
    sweep = MaintenanceSweep(
        store=store,
        repository=repository,
        acquirer=_Refetcher([_record(retraction="retracted")]),
        now=lambda: NOW,
    )

    sweep.run()
    second = sweep.run()

    assert second.retracted == []
    assert second.annotated == []
    assert repository.get_article(article_id).correction_notice


def test_a_tip_is_corrected_too(
    store: EvidenceStore, repository: LocalSQLiteContentRepository
) -> None:
    store.upsert_record(_record())
    tip = repository.save_tip(
        Tip(title="A tip", content_body="Do this.", published_date=NOW, evidence_keys=[KEY])
    )
    store.record_usage(
        source_keys=[KEY], content_type="tip", content_id=tip.id, topic_key="t", used_at=NOW
    )

    MaintenanceSweep(
        store=store,
        repository=repository,
        acquirer=_Refetcher([_record(retraction="retracted")]),
        now=lambda: NOW,
    ).run()

    assert repository.get_tip(tip.id).correction_notice


def test_the_sweep_runs_without_a_source_configured(store: EvidenceStore) -> None:
    report = MaintenanceSweep(store=store, now=lambda: NOW).run()

    assert report.checked == 0
    assert report.acted is False


# -- supersession ------------------------------------------------------


def _bundle(bundle_id: str, *, topic: str = "fasting|weight", created: datetime = NOW) -> EvidenceBundle:
    return EvidenceBundle(
        bundle_id=bundle_id,
        topic_key=topic,
        grade="moderate",
        review_status="approved",
        claims=[Claim(text="A claim.", evidence_keys=[KEY])],
        created_at=created,
    )


def test_an_older_bundle_is_superseded_by_a_newer_one(store: EvidenceStore) -> None:
    from datetime import timedelta

    store.save_bundle(_bundle("old", created=NOW - timedelta(days=30)))
    store.save_bundle(_bundle("new", created=NOW))

    report = MaintenanceSweep(store=store, now=lambda: NOW).run()

    assert report.superseded == ["old"]
    assert store.get_bundle("old").superseded_by == "new"
    assert store.get_bundle("new").superseded_by is None


def test_a_superseded_bundle_is_not_what_the_coach_answers_from(store: EvidenceStore) -> None:
    from datetime import timedelta

    store.save_bundle(_bundle("old", created=NOW - timedelta(days=30)))
    store.save_bundle(_bundle("new", created=NOW))
    MaintenanceSweep(store=store, now=lambda: NOW).run()

    offered = store.approved_bundles("fasting", limit=5)

    assert [bundle.bundle_id for bundle in offered] == ["new"]


def test_supersession_does_not_delete_the_older_bundle(store: EvidenceStore) -> None:
    """It is what was believed at the time, and the run log points at it."""

    from datetime import timedelta

    store.save_bundle(_bundle("old", created=NOW - timedelta(days=30)))
    store.save_bundle(_bundle("new", created=NOW))

    MaintenanceSweep(store=store, now=lambda: NOW).run()

    assert store.get_bundle("old") is not None


def test_a_lone_bundle_is_not_superseded(store: EvidenceStore) -> None:
    store.save_bundle(_bundle("only"))

    assert MaintenanceSweep(store=store, now=lambda: NOW).run().superseded == []


def test_supersession_is_idempotent(store: EvidenceStore) -> None:
    from datetime import timedelta

    store.save_bundle(_bundle("old", created=NOW - timedelta(days=30)))
    store.save_bundle(_bundle("new", created=NOW))
    sweep = MaintenanceSweep(store=store, now=lambda: NOW)

    sweep.run()

    assert sweep.run().superseded == []


# -- contradiction -----------------------------------------------------


def test_a_topic_with_recorded_disagreement_is_reopened(store: EvidenceStore) -> None:
    bundle = _bundle("b1")
    bundle.claims = [
        Claim(text="A claim.", evidence_keys=[KEY], contradicted_by=["pmid:99999999"])
    ]
    store.save_bundle(bundle)

    report = MaintenanceSweep(store=store, now=lambda: NOW).run()

    assert report.contradicted_topics == ["fasting|weight"]


def test_agreement_reopens_nothing(store: EvidenceStore) -> None:
    store.save_bundle(_bundle("b1"))

    assert MaintenanceSweep(store=store, now=lambda: NOW).run().contradicted_topics == []


# -- the scheduled job -------------------------------------------------


def test_the_job_reports_an_outcome_the_scheduler_understands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import pipeline_scheduler

    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))

    outcome = pipeline_scheduler._run_maintenance(NOW)

    # Nothing published, nothing to check: a quiet sweep satisfies the cadence.
    assert outcome is RunOutcome.NO_NEW_EVIDENCE
