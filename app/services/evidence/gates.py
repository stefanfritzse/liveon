"""Deterministic publication gates.

These are the controls that stand in for a human reviewer. They are pure functions over
``(bundle, records)`` returning :class:`Violation` lists, they contain no model call, and
each is individually tested — because a gate whose behaviour depends on a prompt is not a
gate (invariant I6).

Slice 1 implements the four that make the spine safe:

* **G1** — every cited key resolves to an approved record in the store.
* **G2** — every number in published prose traces to a verifying span in a source.
* **G6** — nothing cites a retracted paper or one under an expression of concern.
* **G10** — a source whose design or subject is unknown caps the bundle at ``insufficient``.

G3, G4, G5, G7, G8 and G9 arrive with the reviewer in slice 2. ``run_gates`` is the single
entry point so callers do not need to know which exist yet.
"""

from __future__ import annotations

import logging
import re
from typing import Mapping, Sequence

from app.models.evidence import Claim, EvidenceBundle, EvidenceRecord, Violation

LOGGER = logging.getLogger(__name__)

__all__ = [
    "APPROVED_STATES",
    "g1_sources_resolve",
    "g2_numbers_traceable",
    "g6_no_retracted_sources",
    "g10_unknown_ceiling",
    "numeric_tokens",
    "run_gates",
]

#: Record states a claim may cite. Anything earlier has not been through review.
APPROVED_STATES: frozenset[str] = frozenset({"approved"})

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


def _normalise_number(token: str) -> str:
    """Compare numbers by their digits, so "1,200" and "1200" are the same figure."""

    return "".join(char for char in token if char.isdigit() or char == ".")


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

#: The gates implemented so far, in reporting order.
GATES: tuple = (
    g1_sources_resolve,
    g2_numbers_traceable,
    g6_no_retracted_sources,
    g10_unknown_ceiling,
)


def run_gates(
    bundle: EvidenceBundle,
    records: Mapping[str, EvidenceRecord],
    *,
    allowed_states: frozenset[str] = APPROVED_STATES,
) -> list[Violation]:
    """Run every implemented gate and return all violations found."""

    violations: list[Violation] = []
    violations.extend(g1_sources_resolve(bundle, records, allowed_states=allowed_states))
    violations.extend(g2_numbers_traceable(bundle, records))
    violations.extend(g6_no_retracted_sources(bundle, records))
    violations.extend(g10_unknown_ceiling(bundle, records))

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


def blocking_gates(violations: Sequence[Violation]) -> set[str]:
    """The distinct gates that fired, for logging and for the reviewer's rationale."""

    return {violation.gate for violation in violations}
