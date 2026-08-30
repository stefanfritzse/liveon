"""Deterministic publication gates.

These are the controls that stand in for a human reviewer. They are pure functions over
``(bundle, records)`` returning :class:`Violation` lists, they contain no model call, and
each is individually tested — because a gate whose behaviour depends on a prompt is not a
gate (invariant I6).

The full set:

* **G1** — every cited key resolves to an approved record in the store.
* **G2** — every number in published prose traces to a verifying span in a source.
* **G3** — a claim supported only by animal or in-vitro work may not speak about people.
* **G4** — causal language requires a randomised design.
* **G5** — a surrogate endpoint may not be stated as clinical benefit.
* **G6** — nothing cites a retracted paper or one under an expression of concern.
* **G7** — small or unreported sample sizes cap the grade.
* **G8** — the claim ceiling (:mod:`app.services.evidence.claim_policy`).
* **G9** — a topic published inside the cooldown window is not republished.
* **G10** — a source whose design or subject is unknown caps the bundle at ``insufficient``.

Gates differ in what their violation *means*. Most refuse publication; two only limit how
strongly the finding may be graded. :data:`GATE_SEVERITY` is the single place that says
which is which, and the grader in :mod:`app.services.evidence.grading` reads it rather
than re-deriving the policy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import re
from typing import Mapping

from app.models.evidence import Claim, EvidenceBundle, EvidenceRecord, Violation
from app.services.evidence.claim_policy import check_claim_ceiling

LOGGER = logging.getLogger(__name__)

__all__ = [
    "APPROVED_STATES",
    "CAUSAL_LANGUAGE",
    "GATE_SEVERITY",
    "CAP_GRADES",
    "HEDGED_LANGUAGE",
    "RANDOMISED_DESIGNS",
    "g1_sources_resolve",
    "g2_numbers_traceable",
    "g3_subject_consistency",
    "g4_causal_language",
    "g5_surrogate_endpoints",
    "g6_no_retracted_sources",
    "g7_sample_size_floor",
    "g8_claim_ceiling",
    "g9_topic_cooldown",
    "g10_unknown_ceiling",
    "normalise_number",
    "numeric_tokens",
    "run_gates",
]

#: Record states a claim may cite. Anything earlier has not been through review.
APPROVED_STATES: frozenset[str] = frozenset({"approved"})

#: What a violation from each gate does. ``reject`` refuses publication; ``cap`` allows it
#: but limits the grade. G3 rejects rather than downgrades: a claim that says "people"
#: about mouse evidence is not a weaker claim, it is a different and untrue one, and the
#: fix is to regenerate it rather than to publish it with a lower badge.
GATE_SEVERITY: dict[str, str] = {
    "G1": "reject",
    "G2": "reject",
    "G3": "reject",
    "G4": "reject",
    "G5": "cap",
    "G6": "reject",
    "G7": "cap",
    "G8": "reject",
    "G9": "reject",
    "G10": "reject",
}

#: The ceiling a capping gate imposes.
CAP_GRADES: dict[str, str] = {
    "G5": "low",
    "G7": "preliminary",
}

#: Gates whose failure means the evidence itself is insufficient, as opposed to the
#: writing being wrong. These force the grade to the floor (improvements.md item 3).
INSUFFICIENT_GATES: frozenset[str] = frozenset({"G1", "G2", "G6", "G10"})

_COOLDOWN_ENV = "LIVEON_TOPIC_COOLDOWN_DAYS"
_DEFAULT_COOLDOWN_DAYS = 30

#: Language that asserts a mechanism rather than an association. Public because the
#: post-edit re-check applies the same rule to the final prose, and two definitions of
#: "causal language" would mean the editor could reintroduce what the gate refused.
CAUSAL_LANGUAGE = re.compile(
    r"\b(causes?|caused|causing|reduces?|reduced|lowers?|lowered|raises?|raised|"
    r"increases?|increased|improves?|improved|boosts?|boosted|prevents?|prevented|"
    r"leads? to|results? in|makes? you|drives?|triggers?)\b",
    re.IGNORECASE,
)

#: Hedged framings that keep a causal verb honest: "may reduce", "was associated with".
HEDGED_LANGUAGE = re.compile(
    r"\b(may|might|could|appears? to|seems? to|is associated with|are associated with|"
    r"was associated with|were associated with|linked to|correlated with|suggests?)\b",
    re.IGNORECASE,
)

#: Private aliases so the gate bodies below keep reading naturally.
_CAUSAL_LANGUAGE = CAUSAL_LANGUAGE
_HEDGED = HEDGED_LANGUAGE

#: Designs that carry a randomised comparison, and so license causal language.
RANDOMISED_DESIGNS: frozenset[str] = frozenset({"rct", "meta_analysis"})

#: Subjects that are not evidence of human benefit, whatever the study reports.
NON_HUMAN_SUBJECTS: frozenset[str] = frozenset({"animal", "in_vitro", "in_silico"})

#: Words that describe people. A claim built only on animal work may not use them.
_HUMAN_LANGUAGE = re.compile(
    r"\b(people|persons?|adults?|men|women|patients?|humans?|participants?|"
    r"you|your|readers?|everyone|anyone)\b",
    re.IGNORECASE,
)

#: Outcomes a reader would actually notice, as opposed to a biomarker moving.
_CLINICAL_LANGUAGE = re.compile(
    r"\b(live[sd]? longer|lifespan|longevity|mortality|death|died|survival|"
    r"heart attacks?|strokes?|fractures?|dementia|disability|hospitalisations?|"
    r"hospitalizations?|quality of life|healthspan)\b",
    re.IGNORECASE,
)

#: Numbers in prose: 12, 3.4, 1,200, 45%, 0.83. Deliberately greedy about separators so a
#: figure cannot slip past G2 by being written with a comma.
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%?")

#: Four-digit values in this range are read as years and exempted from G2. Years identify
#: a study rather than quantify a finding, and requiring a span for "a 2023 trial" would
#: push writers toward vaguer prose, not more honest prose.
_YEAR_RANGE = range(1900, 2101)


def numeric_tokens(text: str) -> list[str]:
    """Return the quantitative tokens in ``text``, with years excluded."""

    tokens: list[str] = []
    for match in _NUMBER_RE.finditer(text or ""):
        token = match.group(0)
        if _is_year(token):
            continue
        tokens.append(token)
    return tokens


def _is_year(token: str) -> bool:
    if token.endswith("%") or "." in token or "," in token or len(token) != 4:
        return False
    try:
        return int(token) in _YEAR_RANGE
    except ValueError:
        return False


def normalise_number(token: str) -> str:
    """Reduce a figure to a canonical digit string for comparison.

    "1,200" and "1200" are the same number, and so are ``18.0`` and "18": an effect size
    extracted as a float arrives as ``18.0`` while the prose says "18 percent", and
    without this they would not match — quietly stripping the reference from an honest
    claim and failing it at G2.

    Shared with the synthesizer, which builds number references using the same rule this
    gate checks them with. Two different notions of "the same number" would be a hole.
    """

    digits = "".join(char for char in str(token) if char.isdigit() or char == ".")
    if not digits:
        return ""

    try:
        value = float(digits)
    except ValueError:
        # A quote containing several numbers ("95% CI 2.1 to 6.3") is not one figure;
        # leave its digits alone so the containment check below can still find a match.
        return digits

    if value.is_integer():
        return str(int(value))
    return f"{value:f}".rstrip("0").rstrip(".")


#: Kept as a private alias so existing call sites read naturally.
_normalise_number = normalise_number


# ----------------------------------------------------------------------
# Gates
# ----------------------------------------------------------------------


def g1_sources_resolve(
    bundle: EvidenceBundle,
    records: Mapping[str, EvidenceRecord],
    *,
    allowed_states: frozenset[str] = APPROVED_STATES,
) -> list[Violation]:
    """Every cited key must exist in the store and have cleared review.

    This is the gate that makes an invented citation impossible rather than unlikely: the
    key a writer emits is looked up, and a key nobody acquired resolves to nothing.
    """

    violations: list[Violation] = []
    for claim in bundle.claims:
        if not claim.evidence_keys:
            violations.append(
                Violation(
                    gate="G1",
                    detail="Claim cites no evidence.",
                    claim_text=claim.text,
                )
            )
            continue

        for key in claim.evidence_keys:
            record = records.get(key)
            if record is None:
                violations.append(
                    Violation(
                        gate="G1",
                        detail=f"Unknown evidence key {key!r}: no such record in the store.",
                        claim_text=claim.text,
                        source_key=key,
                    )
                )
            elif record.state not in allowed_states:
                violations.append(
                    Violation(
                        gate="G1",
                        detail=(
                            f"Evidence {key!r} is in state {record.state!r}; "
                            f"expected one of {sorted(allowed_states)}."
                        ),
                        claim_text=claim.text,
                        source_key=key,
                    )
                )
    return violations


def g2_numbers_traceable(
    bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]
) -> list[Violation]:
    """Every number in a claim must be carried by a NumberRef whose span still holds.

    Three things have to line up: the claim declares the number, the span it points at
    verifies against the stored document, and the number actually appears inside the
    quoted text. Checking only the first would let a correct-looking citation carry a
    figure that is nowhere in the paper.
    """

    violations: list[Violation] = []
    for claim in bundle.claims:
        available = _verified_numbers(claim, records, violations)

        for token in numeric_tokens(claim.text):
            if _normalise_number(token) not in available:
                violations.append(
                    Violation(
                        gate="G2",
                        detail=f"Number {token!r} is not traceable to any source span.",
                        claim_text=claim.text,
                    )
                )
    return violations


def _verified_numbers(
    claim: Claim,
    records: Mapping[str, EvidenceRecord],
    violations: list[Violation],
) -> set[str]:
    """Return the normalised numbers this claim may legitimately use."""

    available: set[str] = set()
    for number in claim.numbers:
        record = records.get(number.source_key)
        if record is None:
            violations.append(
                Violation(
                    gate="G2",
                    detail=f"Number {number.text!r} cites unknown source {number.source_key!r}.",
                    claim_text=claim.text,
                    source_key=number.source_key,
                )
            )
            continue

        if not number.span.verify(record.document_text):
            violations.append(
                Violation(
                    gate="G2",
                    detail=(
                        f"Span for {number.text!r} no longer matches the stored document "
                        f"for {number.source_key!r}."
                    ),
                    claim_text=claim.text,
                    source_key=number.source_key,
                )
            )
            continue

        normalised = _normalise_number(number.text)
        if normalised and normalised not in _normalise_number(number.span.quote):
            violations.append(
                Violation(
                    gate="G2",
                    detail=(
                        f"Number {number.text!r} does not appear in the quoted source text: "
                        f"{number.span.quote!r}"
                    ),
                    claim_text=claim.text,
                    source_key=number.source_key,
                )
            )
            continue

        available.add(normalised)
    return available


def g3_subject_consistency(
    bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]
) -> list[Violation]:
    """A claim resting only on animal or in-vitro work may not speak about people.

    The subject comes from PubMed's indexing, never from the model, so this cannot be
    argued around by a confident abstract. "Mice given the compound lived longer" is
    publishable; "people who take it live longer" from the same source is not.
    """

    violations: list[Violation] = []
    for claim in bundle.claims:
        cited = [records[key] for key in claim.evidence_keys if key in records]
        if not cited:
            continue

        subjects = {record.classification.subject for record in cited}
        if not subjects or not subjects <= NON_HUMAN_SUBJECTS:
            continue

        human_word = _HUMAN_LANGUAGE.search(claim.text)
        if human_word:
            violations.append(
                Violation(
                    gate="G3",
                    detail=(
                        f"Claim says {human_word.group(0)!r} but its only evidence is "
                        f"{', '.join(sorted(subjects))}."
                    ),
                    claim_text=claim.text,
                )
            )
    return violations


def g4_causal_language(
    bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]
) -> list[Violation]:
    """Causal language requires a randomised design.

    Two things are checked, because the label and the prose can disagree: a claim typed
    ``causal`` needs randomised evidence, and so does a claim whose *text* asserts a
    mechanism regardless of how it was typed. Hedging ("may reduce", "was associated
    with") keeps an observational finding honest and is left alone.
    """

    violations: list[Violation] = []
    for claim in bundle.claims:
        cited = [records[key] for key in claim.evidence_keys if key in records]
        if not cited:
            continue

        designs = {record.classification.design for record in cited}
        randomised = bool(designs & RANDOMISED_DESIGNS)
        if randomised:
            continue

        if claim.claim_type == "causal":
            violations.append(
                Violation(
                    gate="G4",
                    detail=(
                        f"Claim is typed causal but its evidence is {', '.join(sorted(designs))}."
                    ),
                    claim_text=claim.text,
                )
            )
            continue

        causal_word = _CAUSAL_LANGUAGE.search(claim.text)
        if causal_word and not _HEDGED.search(claim.text):
            violations.append(
                Violation(
                    gate="G4",
                    detail=(
                        f"Unhedged causal verb {causal_word.group(0)!r} on "
                        f"{', '.join(sorted(designs))} evidence."
                    ),
                    claim_text=claim.text,
                )
            )
    return violations


def g5_surrogate_endpoints(
    bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]
) -> list[Violation]:
    """A biomarker that moved is not a life that improved.

    Caps rather than rejects: reporting the biomarker is legitimate and useful, so the
    finding is publishable at a lower grade. What is not legitimate is presenting it as a
    clinical outcome the reader would notice.
    """

    violations: list[Violation] = []
    for claim in bundle.claims:
        cited = [records[key] for key in claim.evidence_keys if key in records]
        if not cited:
            continue

        outcomes = [outcome for record in cited for outcome in record.outcomes]
        known = [outcome.is_surrogate for outcome in outcomes if outcome.is_surrogate.is_known]
        if not known or not all(flag.value for flag in known):
            continue

        clinical_word = _CLINICAL_LANGUAGE.search(claim.text)
        if clinical_word:
            violations.append(
                Violation(
                    gate="G5",
                    detail=(
                        f"Claim speaks of {clinical_word.group(0)!r} but every measured "
                        "endpoint behind it is a surrogate marker."
                    ),
                    claim_text=claim.text,
                )
            )
    return violations


def g6_no_retracted_sources(
    bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]
) -> list[Violation]:
    """Nothing may cite a retracted paper, or one under an expression of concern.

    A correction is not a block: corrected work remains valid, and the correction notice
    is carried through to the reader instead.
    """

    violations: list[Violation] = []
    for claim in bundle.claims:
        for key in claim.evidence_keys:
            record = records.get(key)
            if record is None or not record.is_retracted:
                continue
            violations.append(
                Violation(
                    gate="G6",
                    detail=(
                        f"Evidence {key!r} is marked {record.retraction_state!r}"
                        + (f": {record.retraction_notes[0]}" if record.retraction_notes else "")
                    ),
                    claim_text=claim.text,
                    source_key=key,
                )
            )
    return violations


def g7_sample_size_floor(
    bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]
) -> list[Violation]:
    """Small or unreported human samples cap the grade at ``preliminary``.

    An unreported sample size caps too. That is invariant I3 doing its job: "we do not
    know how many people were studied" is not the same as "enough people were studied",
    and only one of them can be treated as reassuring.
    """

    violations: list[Violation] = []
    for claim in bundle.claims:
        human = [
            records[key]
            for key in claim.evidence_keys
            if key in records and records[key].classification.is_human
        ]
        if not human:
            continue

        largest = max(
            (record.sample_size.value or 0 for record in human if record.sample_size.is_known),
            default=None,
        )
        if largest is None:
            violations.append(
                Violation(
                    gate="G7",
                    detail="No cited human study reports its sample size.",
                    claim_text=claim.text,
                )
            )
        elif largest < 30:
            violations.append(
                Violation(
                    gate="G7",
                    detail=f"Largest human sample is {largest} participants.",
                    claim_text=claim.text,
                )
            )
    return violations


def g8_claim_ceiling(
    bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]
) -> list[Violation]:
    """Apply the claim ceiling to every claim in the bundle (see :mod:`claim_policy`)."""

    violations: list[Violation] = []
    for claim in bundle.claims:
        violations.extend(check_claim_ceiling(claim.text, grade=bundle.grade, claim_text=claim.text))
    return violations


def g9_topic_cooldown(
    bundle: EvidenceBundle,
    records: Mapping[str, EvidenceRecord],
    *,
    last_used_at: datetime | None = None,
    now: datetime | None = None,
    cooldown_days: int | None = None,
) -> list[Violation]:
    """Refuse a topic published again inside the cooldown window.

    The tip editor used to reject drafts as repetitive while the generator could not see
    what had already been published, so the loop could never converge. This moves that
    judgement out of the prompt and into a date comparison the store can answer.
    """

    if last_used_at is None or not bundle.topic_key:
        return []

    window = timedelta(days=cooldown_days if cooldown_days is not None else _cooldown_days())
    moment = now or datetime.now(timezone.utc)
    if moment - last_used_at >= window:
        return []

    days_ago = max(0, (moment - last_used_at).days)
    return [
        Violation(
            gate="G9",
            detail=(
                f"Topic {bundle.topic_key!r} was published {days_ago} day(s) ago; "
                f"the cooldown is {window.days} days."
            ),
        )
    ]


def _cooldown_days() -> int:
    raw = (os.getenv(_COOLDOWN_ENV) or "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_COOLDOWN_DAYS
    except ValueError:
        LOGGER.warning("Ignoring invalid %s=%r", _COOLDOWN_ENV, raw)
        return _DEFAULT_COOLDOWN_DAYS
    return max(0, value)


def g10_unknown_ceiling(
    bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]
) -> list[Violation]:
    """A source we cannot classify cannot support a published claim.

    Design and subject decide how strongly a finding may be stated, so "we do not know
    what kind of study this was" is a blocking answer, not a permissive one (I3).
    """

    violations: list[Violation] = []
    for claim in bundle.claims:
        for key in claim.evidence_keys:
            record = records.get(key)
            if record is None:
                continue  # G1 already reported this; do not double-count.

            classification = record.classification
            missing = [
                name
                for name, value in (("design", classification.design), ("subject", classification.subject))
                if value == "unknown"
            ]
            if missing:
                violations.append(
                    Violation(
                        gate="G10",
                        detail=f"Evidence {key!r} has unknown {' and '.join(missing)}.",
                        claim_text=claim.text,
                        source_key=key,
                    )
                )
    return violations


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def run_gates(
    bundle: EvidenceBundle,
    records: Mapping[str, EvidenceRecord],
    *,
    allowed_states: frozenset[str] = APPROVED_STATES,
    last_used_at: datetime | None = None,
    now: datetime | None = None,
    skip_gates: frozenset[str] = frozenset(),
) -> list[Violation]:
    """Run every gate and return all violations found.

    Every gate runs even after one fails: the reviewer's rationale and the benchmark are
    both more useful when they see the whole picture rather than the first refusal.

    ``skip_gates`` exists for one real ordering problem. G8's certainty rule consults the
    bundle's grade, and the grade is not known until the other gates have run, so the
    reviewer runs everything except G8, grades, then runs G8 against that grade.
    """

    violations: list[Violation] = []

    def _run(gate: str, produce) -> None:
        if gate not in skip_gates:
            violations.extend(produce())

    _run("G1", lambda: g1_sources_resolve(bundle, records, allowed_states=allowed_states))
    _run("G2", lambda: g2_numbers_traceable(bundle, records))
    _run("G3", lambda: g3_subject_consistency(bundle, records))
    _run("G4", lambda: g4_causal_language(bundle, records))
    _run("G5", lambda: g5_surrogate_endpoints(bundle, records))
    _run("G6", lambda: g6_no_retracted_sources(bundle, records))
    _run("G7", lambda: g7_sample_size_floor(bundle, records))
    _run("G8", lambda: g8_claim_ceiling(bundle, records))
    _run("G9", lambda: g9_topic_cooldown(bundle, records, last_used_at=last_used_at, now=now))
    _run("G10", lambda: g10_unknown_ceiling(bundle, records))

    if violations:
        LOGGER.info(
            "Evidence gates rejected %s item(s) in bundle %s",
            len(violations),
            bundle.bundle_id,
            extra={
                "event": "evidence.gate_violations",
                "bundle_id": bundle.bundle_id,
                "gates": ",".join(sorted({violation.gate for violation in violations})),
            },
        )
    return violations
