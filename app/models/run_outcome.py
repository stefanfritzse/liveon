"""What a pipeline run concluded, and what the scheduler should do about it.

The runners used to return ``bool``, and one boolean cannot carry six meanings. "Nothing
new today" stamped the cadence exactly like a successful publication, while "the feeds are
down" retried every hour forever — the two failure modes that most need telling apart were
the two the signature could not express.

Each outcome names one situation and carries its own policy:

* ``stamp`` — treat the cadence as satisfied. A quiet day is a *successful* day: the system
  looked, found nothing publishable, and correctly published nothing.
* ``retry`` — the run never reached a conclusion, so back off and try again rather than
  letting the cadence slide past.

Nothing here maps to "publish anyway" (invariant I5).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["OUTCOME_POLICY", "OutcomePolicy", "RunOutcome", "coerce_outcome", "policy_for"]


class RunOutcome(StrEnum):
    """How a pipeline run ended."""

    #: Content was written and stored.
    PUBLISHED = "published"
    #: Everything worked; there was simply nothing new worth publishing.
    NO_NEW_EVIDENCE = "no_new_evidence"
    #: Evidence was found but could not support publication at any grade.
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    #: The reviewer refused, or regeneration ran out of attempts.
    REVIEW_REJECTED = "review_rejected"
    #: A research source could not be reached.
    RETRIEVAL_FAILED = "retrieval_failed"
    #: A dependency (database, publisher target) was unavailable.
    SOURCE_UNAVAILABLE = "source_unavailable"
    #: The model failed, timed out, or the run exceeded its budget.
    MODEL_FAILED = "model_failed"


@dataclass(frozen=True, slots=True)
class OutcomePolicy:
    """What the scheduler does with an outcome."""

    stamp: bool
    retry: bool
    #: Log level name, so a fail-closed refusal does not read like a crash.
    level: str
    note: str


OUTCOME_POLICY: dict[RunOutcome, OutcomePolicy] = {
    RunOutcome.PUBLISHED: OutcomePolicy(
        stamp=True, retry=False, level="info", note="Published."
    ),
    RunOutcome.NO_NEW_EVIDENCE: OutcomePolicy(
        stamp=True,
        retry=False,
        level="info",
        note="Nothing new to publish; the cadence is satisfied.",
    ),
    RunOutcome.EVIDENCE_INSUFFICIENT: OutcomePolicy(
        stamp=True,
        retry=False,
        level="info",
        note="Evidence did not meet the publication bar. This is the gate working.",
    ),
    RunOutcome.REVIEW_REJECTED: OutcomePolicy(
        stamp=True,
        retry=False,
        level="warning",
        note="Review refused every candidate. Repeated occurrences suggest a regression.",
    ),
    RunOutcome.RETRIEVAL_FAILED: OutcomePolicy(
        stamp=False, retry=True, level="warning", note="Could not reach a research source."
    ),
    RunOutcome.SOURCE_UNAVAILABLE: OutcomePolicy(
        stamp=False, retry=True, level="warning", note="A dependency was unavailable."
    ),
    RunOutcome.MODEL_FAILED: OutcomePolicy(
        stamp=False, retry=True, level="error", note="The model failed or the run timed out."
    ),
}


def policy_for(outcome: RunOutcome) -> OutcomePolicy:
    """Return the policy for ``outcome``, defaulting to the cautious one."""

    return OUTCOME_POLICY.get(outcome, OUTCOME_POLICY[RunOutcome.MODEL_FAILED])


def coerce_outcome(value: object) -> RunOutcome:
    """Accept an outcome, a legacy boolean, or a string, and return an outcome.

    Runners predating this module returned ``True``/``False``; both still work, with
    ``False`` treated as a retryable failure rather than as a quiet success.
    """

    if isinstance(value, RunOutcome):
        return value
    if isinstance(value, bool):
        return RunOutcome.PUBLISHED if value else RunOutcome.MODEL_FAILED
    if isinstance(value, str):
        try:
            return RunOutcome(value.strip().lower())
        except ValueError:
            return RunOutcome.MODEL_FAILED
    return RunOutcome.MODEL_FAILED
