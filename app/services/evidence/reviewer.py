"""The evidence reviewer: code decides, the model advises.

This is the publication gate, and with no human behind it the arrangement matters more
than the prompt does. Three layers run in order:

1. **Deterministic gates** (:mod:`app.services.evidence.gates`) — authoritative. If they
   refuse, the model is never asked, because there is nothing for it to add.
2. **The grade rubric** (:mod:`app.services.evidence.grading`) — computed from what the
   sources are, not from what anything says about them.
3. **An advisory model pass** — asked only what code cannot compute: is the draft
   overstating its sources, is contradicting evidence being ignored, is the stated
   applicability honest.

The asymmetry in layer 3 is the whole design. A reviewer that can be talked into approving
is not a reviewer, so the model is wired so that its only available moves are the safe
ones.

**The model does not choose the verdict.** It answers three closed questions and code
computes the status from the answers. An earlier version let it return a status of its own
alongside free-text "concerns", and a real run showed exactly why that fails: it returned a
publishing status while objecting that the claims contradicted the studies behind them, and
listed as a third "concern" the observation that the population was described correctly. A
field that mixes objections with observations, and a verdict the objector picks, is not a
control. Questions have answers; answers can be acted on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
from typing import Any, Callable, Mapping, Protocol

from app.models.evidence import (
    EvidenceBundle,
    EvidenceRecord,
    ReviewDecision,
    Violation,
    clamp_grade,
)
from app.services.evidence.gates import GATE_SEVERITY, run_gates
from app.services.evidence.grading import compute_grade
from app.utils.json_repair import invoke_json_object
from app.utils.langchain_compat import BaseMessage, ChatPromptTemplate

LOGGER = logging.getLogger(__name__)

__all__ = [
    "REVIEW_PROMPT_VERSION",
    "REVIEW_QUESTIONS",
    "EvidenceReviewer",
    "max_regenerations",
]

#: Bumped: the advisory pass now answers closed questions instead of returning a verdict.
REVIEW_PROMPT_VERSION = "2"

#: Gates whose failure is about the *evidence*: no rewrite can fix a retracted paper, a
#: topic published last week, or a source nobody can classify.
_EVIDENCE_FAILURES = frozenset({"G6", "G9", "G10"})

#: What the advisory pass is asked, and what a "yes" means for the draft. Each is a
#: judgement the deterministic layers genuinely cannot make; each has an answer rather
#: than an opinion; and each, answered yes, means the prose is wrong rather than the
#: evidence, so the response is to rewrite.
REVIEW_QUESTIONS: dict[str, str] = {
    "overstates_evidence": "A claim says more than its sources support.",
    "ignores_contradicting_evidence": "Disagreeing evidence is hidden or averaged away.",
    "applicability_misleading": "The population described is not the population studied.",
}


class SupportsInvoke(Protocol):
    def invoke(self, input: Any, **kwargs: Any) -> BaseMessage | str:  # pragma: no cover
        ...


def max_regenerations() -> int:
    raw = (os.getenv("LIVEON_MAX_REGENERATIONS") or "").strip()
    try:
        return max(0, int(raw)) if raw else 2
    except ValueError:
        LOGGER.warning("Ignoring invalid LIVEON_MAX_REGENERATIONS=%r", raw)
        return 2


DEFAULT_SYSTEM_PROMPT = (
    "You are a sceptical evidence reviewer for a health publication. Automated checks have "
    "already verified the citations, the numbers, the study designs and the grade. Your job "
    "is the judgement they cannot make: whether the writing overstates what the studies "
    "found. You answer specific questions. You do not decide whether anything is published."
)

DEFAULT_HUMAN_PROMPT = """
Answer three questions about the claims below. Return valid JSON:

{{
  "overstates_evidence": true or false,
  "ignores_contradicting_evidence": true or false,
  "applicability_misleading": true or false,
  "grade": "high" | "moderate" | "low" | "preliminary" | "insufficient",
  "notes": "one or two sentences of reasoning"
}}

- overstates_evidence: does any claim say more than its sources support?
- ignores_contradicting_evidence: is disagreeing evidence hidden, or averaged into a
  middle position?
- applicability_misleading: is the population the claims describe the population that was
  actually studied?

Answer true only when it is actually so. Every true answer sends the draft back to be
rewritten, so an answer of true about wording you merely dislike costs a rewrite and
changes nothing.

"grade" may repeat the computed grade or name a lower one; a higher one is discarded.
"notes" is recorded for the log and decides nothing. Do not return a verdict: you are
answering questions, not deciding whether this is published.

Do not check citations, arithmetic, study design or the grade. Those are already verified.

Computed grade: {grade}
Rationale: {rationale}

Claims:
{claims}

Evidence:
{evidence}
""".strip()


def _default_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [("system", DEFAULT_SYSTEM_PROMPT), ("human", DEFAULT_HUMAN_PROMPT)]
    )


@dataclass(slots=True)
class EvidenceReviewer:
    """Decide whether a bundle may be published, and at what grade."""

    llm: SupportsInvoke | None = None
    prompt: ChatPromptTemplate = field(default_factory=_default_prompt)
    model_id: str = "unknown"
    prompt_version: str = REVIEW_PROMPT_VERSION
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def review(
        self,
        bundle: EvidenceBundle,
        records: Mapping[str, EvidenceRecord],
        *,
        last_used_at: datetime | None = None,
    ) -> ReviewDecision:
        """Review ``bundle`` and record the decision on it.

        The bundle is updated in place — grade, rationale, violations, review status — so
        that whatever persists it stores the reviewed version rather than the draft.
        """

        moment = self.now()

        # Grade first, ceiling second: G8 permits certainty language only in a "high"
        # bundle, and until the other gates have run there is no grade to consult.
        violations = list(
            run_gates(bundle, records, last_used_at=last_used_at, now=moment, skip_gates=frozenset({"G8"}))
        )
        grade, rationale = compute_grade(bundle, records, violations)
        bundle.grade = grade
        violations.extend(run_gates(bundle, records, skip_gates=_ALL_BUT_CEILING))

        grade, rationale = compute_grade(bundle, records, violations)
        decision = self._decide(bundle, records, violations, grade, rationale, moment)
        self._apply(bundle, decision)
        return decision

    # -- layers --------------------------------------------------------

    def _decide(
        self,
        bundle: EvidenceBundle,
        records: Mapping[str, EvidenceRecord],
        violations: list[Violation],
        grade: str,
        rationale: list[str],
        moment: datetime,
    ) -> ReviewDecision:
        rejects = [v for v in violations if GATE_SEVERITY.get(v.gate) == "reject"]

        if rejects:
            gates = {violation.gate for violation in rejects}
            status = "rejected" if gates & _EVIDENCE_FAILURES else "regenerate"
            LOGGER.info(
                "Evidence review refused a bundle",
                extra={
                    "event": "evidence.review_refused",
                    "bundle_id": bundle.bundle_id,
                    "status": status,
                    "gates": ",".join(sorted(gates)),
                },
            )
            return ReviewDecision(
                status=status,
                grade="insufficient" if gates & _EVIDENCE_FAILURES else grade,
                rationale=rationale,
                violations=violations,
                notes=f"Refused by {', '.join(sorted(gates))}.",
                reviewed_at=moment,
                model_id=self.model_id,
                prompt_version=self.prompt_version,
            )

        if grade == "insufficient":
            return ReviewDecision(
                status="rejected",
                grade=grade,
                rationale=rationale,
                violations=violations,
                notes="The evidence does not support publication at any grade.",
                reviewed_at=moment,
                model_id=self.model_id,
                prompt_version=self.prompt_version,
            )

        if self.llm is None:
            # Deterministic-only review is a valid configuration; it is what the offline
            # benchmark runs, and it is strictly more conservative than adding a model.
            return ReviewDecision(
                status="approved",
                grade=grade,
                rationale=rationale,
                violations=violations,
                notes="Deterministic review only; no advisory model configured.",
                reviewed_at=moment,
                model_id=None,
                prompt_version=self.prompt_version,
            )

        return self._advisory(bundle, records, violations, grade, rationale, moment)

    def _advisory(
        self,
        bundle: EvidenceBundle,
        records: Mapping[str, EvidenceRecord],
        violations: list[Violation],
        grade: str,
        rationale: list[str],
        moment: datetime,
    ) -> ReviewDecision:
        try:
            payload = invoke_json_object(
                self.llm,
                self.prompt.format_messages(
                    grade=grade,
                    rationale="; ".join(rationale) or "none recorded",
                    claims=_render_claims(bundle),
                    evidence=_render_evidence(bundle, records),
                ),
                label="EvidenceReviewer",
                logger=LOGGER,
            )
        except Exception as exc:  # noqa: BLE001 - a review we cannot complete is a refusal
            LOGGER.warning(
                "Advisory review failed; refusing rather than publishing unreviewed",
                extra={"event": "evidence.review_unavailable", "bundle_id": bundle.bundle_id},
            )
            return ReviewDecision(
                status="regenerate",
                grade=grade,
                rationale=rationale,
                violations=violations,
                notes=f"Advisory review unavailable: {exc}",
                reviewed_at=moment,
                model_id=self.model_id,
                prompt_version=self.prompt_version,
            )

        answers = {name: _as_bool(payload.get(name)) for name in REVIEW_QUESTIONS}

        if all(answer is None for answer in answers.values()):
            # It replied, but not to what was asked. An unanswered review is not a pass.
            LOGGER.info(
                "Advisory reviewer answered none of the questions put to it",
                extra={
                    "event": "evidence.review_unanswered",
                    "bundle_id": bundle.bundle_id,
                },
            )
            return ReviewDecision(
                status="regenerate",
                grade=grade,
                rationale=rationale,
                violations=violations,
                notes="Advisory review did not answer the questions asked.",
                reviewed_at=moment,
                model_id=self.model_id,
                prompt_version=self.prompt_version,
            )

        flagged = [
            Violation(gate="ADVISORY", detail=REVIEW_QUESTIONS[name])
            for name, answer in answers.items()
            if answer is True
        ]

        # I4: the model may lower the grade, never raise it.
        proposed_grade = str(payload.get("grade") or "").strip().lower()
        final_grade = clamp_grade(proposed_grade, grade)
        if proposed_grade and proposed_grade != final_grade:
            LOGGER.info(
                "Discarded a reviewer grade above the computed one",
                extra={
                    "event": "evidence.review_grade_overruled",
                    "bundle_id": bundle.bundle_id,
                    "proposed": proposed_grade,
                    "computed": grade,
                },
            )

        # The verdict is computed, not chosen. Every question, answered yes, says the
        # prose is wrong rather than the evidence — so the draft is rewritten.
        if flagged:
            status = "regenerate"
        elif final_grade != grade:
            status = "downgraded"
        else:
            status = "approved"
        if final_grade == "insufficient":
            status = "rejected"

        notes = str(payload.get("notes") or "").strip()
        return ReviewDecision(
            status=status,
            grade=final_grade,
            rationale=[*rationale, *(f"Reviewer: {item.detail}" for item in flagged)],
            violations=[*violations, *flagged],
            notes=notes,
            reviewed_at=moment,
            model_id=self.model_id,
            prompt_version=self.prompt_version,
        )

    @staticmethod
    def _apply(bundle: EvidenceBundle, decision: ReviewDecision) -> None:
        bundle.grade = decision.grade
        bundle.grade_rationale = list(decision.rationale)
        bundle.violations = list(decision.violations)
        bundle.review_status = decision.status
        bundle.review = decision


def _as_bool(value: Any) -> bool | None:
    """Read an answer, or ``None`` when the model did not give one.

    Local models answer yes/no questions with strings as often as with booleans, and the
    difference between "no" and "did not answer" matters here: one is a pass, the other is
    a review that did not happen.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in ("true", "yes", "y"):
            return True
        if cleaned in ("false", "no", "n"):
            return False
    return None


#: Everything except G8, for the second pass.
_ALL_BUT_CEILING = frozenset(GATE_SEVERITY) - {"G8"}


def _render_claims(bundle: EvidenceBundle) -> str:
    lines = []
    for index, claim in enumerate(bundle.claims, start=1):
        lines.append(f"{index}. [{claim.claim_type}] {claim.text}")
        if claim.population_scope:
            lines.append(f"   population: {claim.population_scope}")
        if claim.contradicted_by:
            lines.append(f"   contradicted by: {', '.join(claim.contradicted_by)}")
    return "\n".join(lines) or "No claims."


def _render_evidence(bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]) -> str:
    """Summarise the evidence for the prompt without exposing raw documents or URLs."""

    lines = []
    for key in bundle.source_keys():
        record = records.get(key)
        if record is None:
            continue
        classification = record.classification
        size = record.sample_size.value if record.sample_size.is_known else "not reported"
        lines.append(
            f"- {record.title or key}: {classification.design}, {classification.subject}, "
            f"n={size}"
        )
    return "\n".join(lines) or "No evidence resolved."
