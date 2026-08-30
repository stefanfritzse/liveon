"""Re-check the text that will actually be published.

The gates in :mod:`app.services.evidence.gates` run over *claims* — the structured output
of synthesis. Between there and the reader sits a writer and an editor, and editorial
rewriting is exactly where "was associated with" becomes "reduces" and where a number
acquires a decimal place it never had.

So three gates run again, on the final prose:

* **G2** — every number in the body must be one the bundle already anchored.
* **G4** — causal language still requires randomised evidence.
* **G8** — the claim ceiling, evaluated at the bundle's grade.

This is a different question from the one the reviewer answered. The reviewer asked
whether the claims are supportable; this asks whether the sentences still say those
claims. A failure here is editorial, not scientific: the bundle stands, and the draft is
rewritten.
"""

from __future__ import annotations

import logging
from typing import Mapping, Sequence

from app.models.evidence import EvidenceBundle, EvidenceRecord, Violation
from app.services.evidence.claim_policy import check_claim_ceiling, sentences
from app.services.evidence.gates import (
    CAUSAL_LANGUAGE,
    HEDGED_LANGUAGE,
    RANDOMISED_DESIGNS,
    normalise_number,
    numeric_tokens,
)

LOGGER = logging.getLogger(__name__)

__all__ = ["recheck_published_text"]


def recheck_published_text(
    text: str,
    bundle: EvidenceBundle,
    records: Mapping[str, EvidenceRecord],
) -> list[Violation]:
    """Return violations in the final text of an article or tip."""

    violations: list[Violation] = []
    violations.extend(_numbers(text, bundle))
    violations.extend(_causal(text, bundle, records))
    violations.extend(check_claim_ceiling(text, grade=bundle.grade))

    if violations:
        LOGGER.info(
            "Post-edit re-check rejected the draft (%s)",
            ", ".join(sorted({violation.gate for violation in violations})),
            extra={
                "event": "evidence.postedit_rejected",
                "bundle_id": bundle.bundle_id,
                "violations": len(violations),
            },
        )
    return violations


def _anchored_numbers(bundle: EvidenceBundle) -> set[str]:
    """Every figure the bundle can vouch for, normalised."""

    return {
        normalise_number(number.text)
        for claim in bundle.claims
        for number in claim.numbers
        if normalise_number(number.text)
    }


def _numbers(text: str, bundle: EvidenceBundle) -> list[Violation]:
    available = _anchored_numbers(bundle)

    violations: list[Violation] = []
    for token in numeric_tokens(text):
        if normalise_number(token) not in available:
            violations.append(
                Violation(
                    gate="G2",
                    detail=(
                        f"Edited text contains {token!r}, which is not among the figures "
                        "the bundle anchored."
                    ),
                    claim_text=_containing_sentence(text, token),
                )
            )
    return violations


def _causal(
    text: str, bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]
) -> list[Violation]:
    """Unhedged causal language needs randomised evidence somewhere in the bundle.

    This is coarser than the claim-level G4, which knows which sources back which
    sentence. Once the text has been rewritten that mapping is gone, so the check falls
    back to the strongest design available — deliberately permissive, because the precise
    version already ran before the editor touched it.
    """

    designs = {
        records[key].classification.design
        for key in bundle.source_keys()
        if key in records
    }
    if designs & RANDOMISED_DESIGNS:
        return []

    violations: list[Violation] = []
    for sentence in sentences(text):
        causal_word = CAUSAL_LANGUAGE.search(sentence)
        if causal_word and not HEDGED_LANGUAGE.search(sentence):
            violations.append(
                Violation(
                    gate="G4",
                    detail=(
                        f"Edited text asserts {causal_word.group(0)!r} on "
                        f"{', '.join(sorted(designs)) or 'unclassified'} evidence."
                    ),
                    claim_text=sentence,
                )
            )
    return violations


def _containing_sentence(text: str, token: str) -> str:
    for sentence in sentences(text):
        if token in sentence:
            return sentence
    return ""


def feedback_for(violations: Sequence[Violation]) -> str:
    """Turn violations into instructions a writer can act on.

    Regeneration only helps if the next attempt knows what went wrong, and the gate detail
    is already a plain sentence about a specific problem.
    """

    if not violations:
        return ""

    lines = [f"- {violation.detail}" for violation in violations[:6]]
    return (
        "The previous draft failed the publication checks:\n"
        + "\n".join(lines)
        + "\nRewrite it using only the claims and figures you were given."
    )
