"""Tests for run outcomes and the scheduler policy they drive.

The behaviour under test is the one a single boolean could not express: a quiet day
satisfies the cadence, an outage backs off, and neither ever results in publishing
something the pipeline is not sure about.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.run_outcome import (
    OUTCOME_POLICY,
    RunOutcome,
    coerce_outcome,
    policy_for,
)
from app.services.pipeline_scheduler import (
    JobConfig,
    PipelineScheduleStore,
    PipelineScheduler,
)

NOW = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path: Path) -> PipelineScheduleStore:
    return PipelineScheduleStore(tmp_path / "schedule.db")


def _job(outcome, name: str = "tips") -> JobConfig:
    return JobConfig(name=name, runner=lambda _now: outcome, interval_days=1)


def _run(scheduler: PipelineScheduler, job: JobConfig, now: datetime) -> None:
    asyncio.run(scheduler._execute(job, now))


def _scheduler(store: PipelineScheduleStore, job: JobConfig) -> PipelineScheduler:
    return PipelineScheduler(store, [job], check_interval_sec=3600)


# -- the policy table --------------------------------------------------


def test_every_outcome_has_a_policy() -> None:
    assert set(OUTCOME_POLICY) == set(RunOutcome)


@pytest.mark.parametrize(
    "outcome",
    [
        RunOutcome.PUBLISHED,
        RunOutcome.NO_NEW_EVIDENCE,
        RunOutcome.EVIDENCE_INSUFFICIENT,
        RunOutcome.REVIEW_REJECTED,
    ],
)
def test_a_completed_run_satisfies_the_cadence(outcome: RunOutcome) -> None:
    """Including the refusals: the system looked and correctly published nothing."""

    policy = policy_for(outcome)

    assert policy.stamp is True
    assert policy.retry is False


@pytest.mark.parametrize(
    "outcome",
    [RunOutcome.RETRIEVAL_FAILED, RunOutcome.SOURCE_UNAVAILABLE, RunOutcome.MODEL_FAILED],
)
def test_an_incomplete_run_backs_off_instead_of_sliding_the_cadence(outcome: RunOutcome) -> None:
    policy = policy_for(outcome)

    assert policy.stamp is False
    assert policy.retry is True


def test_legacy_boolean_runners_still_work() -> None:
    """False becomes a retryable failure, not a quiet success."""

    assert coerce_outcome(True) is RunOutcome.PUBLISHED
    assert coerce_outcome(False) is RunOutcome.MODEL_FAILED
    assert coerce_outcome("no_new_evidence") is RunOutcome.NO_NEW_EVIDENCE
    assert coerce_outcome("nonsense") is RunOutcome.MODEL_FAILED
    assert coerce_outcome(None) is RunOutcome.MODEL_FAILED


def test_an_unknown_outcome_is_treated_cautiously() -> None:
    assert policy_for("not-an-outcome") is OUTCOME_POLICY[RunOutcome.MODEL_FAILED]


# -- scheduler behaviour -----------------------------------------------


def test_a_quiet_day_advances_the_cadence(store: PipelineScheduleStore) -> None:
    job = _job(RunOutcome.NO_NEW_EVIDENCE)

    _run(_scheduler(store, job), job, NOW)

    assert store.get_last_run("tips") == NOW
    assert store.get_retry_at("tips") is None
    assert store.is_due(job, NOW + timedelta(hours=1)) is False


def test_a_refused_draft_advances_the_cadence(store: PipelineScheduleStore) -> None:
    """Review rejecting everything is the gate working, not a failed run."""

    job = _job(RunOutcome.REVIEW_REJECTED)

    _run(_scheduler(store, job), job, NOW)

    assert store.get_last_run("tips") == NOW


def test_a_retrieval_failure_does_not_advance_the_cadence(store: PipelineScheduleStore) -> None:
    job = _job(RunOutcome.RETRIEVAL_FAILED)

    _run(_scheduler(store, job), job, NOW)

    assert store.get_last_run("tips") is None
    assert store.get_failure_count("tips") == 1


def test_a_failing_job_is_held_until_its_backoff_expires(store: PipelineScheduleStore) -> None:
    """Otherwise a source outage means knocking on its door every hour, forever."""

    job = _job(RunOutcome.RETRIEVAL_FAILED)
    _run(_scheduler(store, job), job, NOW)

    retry_at = store.get_retry_at("tips")
    assert retry_at is not None
    assert store.is_due(job, NOW + timedelta(minutes=5)) is False
    assert store.is_due(job, retry_at) is True


def test_backoff_grows_with_consecutive_failures(store: PipelineScheduleStore) -> None:
    first = store.record_failure("tips", now=NOW, cap_seconds=86400)
    second = store.record_failure("tips", now=NOW, cap_seconds=86400)
    third = store.record_failure("tips", now=NOW, cap_seconds=86400)

    assert (first - NOW) == timedelta(minutes=15)
    assert (second - NOW) == timedelta(minutes=30)
    assert (third - NOW) == timedelta(minutes=60)
    assert store.get_failure_count("tips") == 3


def test_backoff_never_exceeds_the_jobs_own_interval(store: PipelineScheduleStore) -> None:
    """A daily job should not end up waiting a week because it failed six times."""

    for _ in range(8):
        retry_at = store.record_failure("tips", now=NOW, cap_seconds=86400)

    assert retry_at - NOW <= timedelta(days=1)


def test_a_success_clears_the_backoff(store: PipelineScheduleStore) -> None:
    failing = _job(RunOutcome.RETRIEVAL_FAILED)
    _run(_scheduler(store, failing), failing, NOW)

    recovering = _job(RunOutcome.PUBLISHED)
    _run(_scheduler(store, recovering), recovering, NOW + timedelta(hours=1))

    assert store.get_failure_count("tips") == 0
    assert store.get_retry_at("tips") is None
    assert store.get_last_run("tips") == NOW + timedelta(hours=1)


def test_a_first_run_that_fails_still_retries_soon(store: PipelineScheduleStore) -> None:
    """A failed first run must not look like a successful one and wait a full day."""

    job = _job(RunOutcome.MODEL_FAILED)

    _run(_scheduler(store, job), job, NOW)

    assert store.get_last_run("tips") is None
    retry_at = store.get_retry_at("tips")
    assert retry_at is not None and retry_at - NOW == timedelta(minutes=15)


def test_a_raising_runner_is_treated_as_a_model_failure(store: PipelineScheduleStore) -> None:
    def _explode(_now: datetime):
        raise RuntimeError("boom")

    job = JobConfig(name="tips", runner=_explode, interval_days=1)

    _run(_scheduler(store, job), job, NOW)

    assert store.get_last_run("tips") is None
    assert store.get_failure_count("tips") == 1


def test_a_boolean_returning_runner_still_schedules(store: PipelineScheduleStore) -> None:
    job = JobConfig(name="tips", runner=lambda _now: True, interval_days=1)

    _run(_scheduler(store, job), job, NOW)

    assert store.get_last_run("tips") == NOW
