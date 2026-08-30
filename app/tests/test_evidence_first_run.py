"""Tests for the things the first live run depends on.

The checklist in ledger.md is only useful if each step can actually be performed. These
assert exactly that: the flag can be limited to one job, the run log opens the database the
application is using, a dry run leaves no trace that would block the real one, and the
inspection commands work.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from app.models.run_outcome import RunOutcome
from app.services.evidence.extractor import ExtractorAgent
from app.services.evidence.reviewer import EvidenceReviewer
from app.services.evidence.runlog import RunLog
from app.services.evidence.store import EvidenceStore, resolve_db_path
from app.services.evidence.synthesizer import SynthesizerAgent
from app.services.evidence.writers import ArticleWriter
from app.services.evidence_pipeline import (
    EvidencePipeline,
    evidence_pipeline_enabled,
    run_article,
)
from app.scripts import run_evidence_pipeline as cli
from app.tests.test_evidence_pipeline import (
    ARTICLE,
    CLAIMS,
    EXTRACTION,
    Publisher,
    StubAcquirer,
)

NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


class StubLLM:
    def __init__(self, payload: Any) -> None:
        self._payload = json.dumps(payload)

    def invoke(self, _input: Any, **_kwargs: Any) -> AIMessage:
        return AIMessage(content=self._payload)


def _pipeline(store: EvidenceStore, **overrides: Any) -> EvidencePipeline:
    defaults = dict(
        store=store,
        acquirer=StubAcquirer(),
        extractor=ExtractorAgent(llm=StubLLM(EXTRACTION), model_id="stub"),
        synthesizer=SynthesizerAgent(
            llm=StubLLM(CLAIMS), model_id="stub", bundle_id_factory=lambda: "bundle-1"
        ),
        reviewer=EvidenceReviewer(model_id="stub", now=lambda: NOW),
        queries=["longevity"],
        now=lambda: NOW,
    )
    defaults.update(overrides)
    return EvidencePipeline(**defaults)


# -- step 1: one job at a time -----------------------------------------


def test_the_flag_can_be_limited_to_one_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """A first run should not switch both products at once."""

    monkeypatch.setenv("LIVEON_EVIDENCE_PIPELINE", "1")
    monkeypatch.setenv("LIVEON_EVIDENCE_PIPELINE_JOBS", "tips")

    assert evidence_pipeline_enabled("tips") is True
    assert evidence_pipeline_enabled("articles") is False


def test_an_empty_job_list_means_every_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_EVIDENCE_PIPELINE", "1")
    monkeypatch.delenv("LIVEON_EVIDENCE_PIPELINE_JOBS", raising=False)

    assert evidence_pipeline_enabled("tips") is True
    assert evidence_pipeline_enabled("articles") is True


def test_the_job_list_does_nothing_without_the_master_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIVEON_EVIDENCE_PIPELINE", raising=False)
    monkeypatch.setenv("LIVEON_EVIDENCE_PIPELINE_JOBS", "tips")

    assert evidence_pipeline_enabled("tips") is False


def test_the_scheduler_honours_the_job_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import evidence_jobs, pipeline_scheduler

    monkeypatch.setenv("LIVEON_EVIDENCE_PIPELINE", "1")
    monkeypatch.setenv("LIVEON_EVIDENCE_PIPELINE_JOBS", "tips")

    called: list[str] = []
    monkeypatch.setattr(
        evidence_jobs,
        "run_tip_job",
        lambda run_at: called.append("tips") or RunOutcome.PUBLISHED,
    )
    monkeypatch.setattr(
        evidence_jobs,
        "run_article_job",
        lambda run_at: called.append("articles") or RunOutcome.PUBLISHED,
    )

    pipeline_scheduler._run_tip_pipeline(NOW)

    assert called == ["tips"]


# -- step 3: the log opens the right database --------------------------


def test_the_store_and_log_follow_the_configured_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise an operator reads an empty file and concludes nothing ever ran."""

    configured = tmp_path / "configured.db"
    monkeypatch.setenv("LIVEON_DB_PATH", str(configured))

    assert resolve_db_path() == configured

    with RunLog() as log:
        run_id = log.start("articles", now=NOW)
    with RunLog() as reopened:
        assert reopened.get(run_id) is not None

    assert configured.exists()


def test_an_explicit_path_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "configured.db"))
    explicit = tmp_path / "explicit.db"

    assert resolve_db_path(explicit) == explicit


# -- step 2: the dry run leaves no trace -------------------------------


def test_a_dry_run_does_not_put_the_topic_in_the_cooldown(tmp_path: Path) -> None:
    """A rehearsal must not block the real run it exists to de-risk."""

    with EvidenceStore(tmp_path / "content.db") as store:
        pipeline = _pipeline(store, dry_run=True)
        result = run_article(pipeline, ArticleWriter(llm=StubLLM(ARTICLE)), Publisher())

        assert result.outcome is RunOutcome.PUBLISHED
        assert store.usage_for_source("doi:10.1001/jama.2026.1") == []
        assert store.last_used_at("eight-hour-window|mortality") is None


def test_a_real_run_does_record_usage(tmp_path: Path) -> None:
    with EvidenceStore(tmp_path / "content.db") as store:
        pipeline = _pipeline(store)
        run_article(pipeline, ArticleWriter(llm=StubLLM(ARTICLE)), Publisher())

        assert store.usage_for_source("doi:10.1001/jama.2026.1") != []


def test_a_dry_run_still_refuses_what_a_real_run_would(tmp_path: Path) -> None:
    """The rehearsal is only informative if every gate still bites."""

    bad_claims = {
        "claims": [
            {
                "text": "Mortality fell by 99 percent.",
                "claim_type": "causal",
                "evidence": ["E1"],
            }
        ]
    }

    with EvidenceStore(tmp_path / "content.db") as store:
        pipeline = _pipeline(
            store,
            dry_run=True,
            synthesizer=SynthesizerAgent(
                llm=StubLLM(bad_claims), model_id="stub", bundle_id_factory=lambda: "b"
            ),
        )
        result = run_article(pipeline, ArticleWriter(llm=StubLLM(ARTICLE)), Publisher())

    assert result.outcome is RunOutcome.REVIEW_REJECTED
    assert any(violation.gate == "G2" for violation in result.violations)


# -- the inspection commands -------------------------------------------


def test_show_runs_reports_an_empty_log_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))

    assert cli.main(["--show-runs"]) == 0
    assert "No runs recorded yet" in capsys.readouterr().out


def test_show_runs_lists_what_happened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))
    with RunLog() as log:
        run_id = log.start("articles", now=NOW)
        log.finish(run_id, "no_new_evidence", now=NOW)

    assert cli.main(["--show-runs"]) == 0

    output = capsys.readouterr().out
    assert run_id in output
    assert "no_new_evidence" in output


def test_show_run_prints_the_events_behind_a_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))
    with RunLog() as log:
        run_id = log.start("articles", now=NOW)
        log.event(run_id, "review", "decision", {"status": "rejected", "grade": "low"}, now=NOW)
        log.finish(run_id, "review_rejected", model_id="qwen2.5:14b", now=NOW)

    assert cli.main(["--show-run", run_id]) == 0

    output = capsys.readouterr().out
    assert "review_rejected" in output
    assert "qwen2.5:14b" in output
    assert "rejected" in output


def test_show_run_reports_an_unknown_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))

    assert cli.main(["--show-run", "nope"]) == 1
    assert "No run with id" in capsys.readouterr().out


def test_the_cli_defaults_are_the_cautious_ones() -> None:
    args = cli._parse_args([])

    assert args.job == "articles"
    assert args.dry_run is False
    assert args.show_runs is False
