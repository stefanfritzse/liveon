"""Re-check the text that will actually be published.

The gates in :mod:`app.services.evidence.gates` run over *claims* — the structured output
of synthesis. Between there and the reader sits a writer and an editor, and editorial
rewriting is exactly where "was associated with" becomes "reduces" and where a number
acquires a decimal place it never had.

So three gates run again, on the final prose:

* **G2** — every number that *reports a finding* is either one the bundle anchored or one
  that appears verbatim in a cited source. Practical quantities in a suggestion — "spend
  ten minutes a day" — are not claims about the evidence and are left alone.
* **G4** — causal language still requires randomised evidence.
* **G5** — a surrogate marker is still not a clinical benefit.
* **G8** — the claim ceiling, evaluated at the bundle's grade.

This is a different question from the one the reviewer answered. The reviewer asked
whether the claims are supportable; this asks whether the sentences still say those
claims. A failure here is editorial, not scientific: the bundle stands, and the draft is
rewritten.
"""

from __future__ import annotations

import logging
import re
from typing import Mapping, Sequence

from app.models.evidence import EvidenceBundle, EvidenceRecord, Violation
from app.services.evidence.claim_policy import check_claim_ceiling, sentences
from app.services.evidence.gates import (
    CAUSAL_LANGUAGE,
    CLINICAL_LANGUAGE,
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
    violations.extend(_numbers(text, bundle, records))
    violations.extend(_causal(text, bundle, records))
    violations.extend(_surrogate(text, bundle, records))
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
    """Figures the bundle vouches for, each carrying span-level provenance."""

    return {
        normalise_number(number.text)
        for claim in bundle.claims
        for number in claim.numbers
        if normalise_number(number.text)
    }


def _source_numbers(records: Mapping[str, EvidenceRecord]) -> set[str]:
    """Figures that appear verbatim somewhere in a cited source.

    Writers are shown the titles of the studies they cite, and a title routinely carries
    a fact worth repeating — "Impact of a 4-week time-restricted eating intervention".
    Reporting that duration is accurate and improves the writing, but it is not one of the
    claim's numbers, so checking only those refuses it. Refusing an accurate figure taken
    from the cited paper is brittleness rather than integrity.

    This is still I2: the figure resolves to verbatim text in a stored source. It is the
    weaker of the two guarantees — the claims keep span-level provenance, this only knows
    the number is in the paper — so it applies to context the writer adds, never to the
    claims themselves, which the claim-level gate governs.
    """

    return {
        normalise_number(token)
        for record in records.values()
        for token in numeric_tokens(record.document_text)
        if normalise_number(token)
    }


#: Units that measure a practice rather than a result.
_PRACTICE_UNIT = re.compile(
    r"^[\s-]*(minutes?|mins?|hours?|seconds?|times?|days?|weeks?|nights?|sessions?)\b",
    re.IGNORECASE,
)

#: Sentences that suggest rather than report.
_SUGGESTION = re.compile(
    r"^\s*(?:try|spend|aim|take|walk|do|start|begin|set|give|keep|add|use|consider|today|"
    r"each|every|even|make)\b|\byou (?:can|could|might|may|only need)\b",
    re.IGNORECASE,
)


def _is_practice_quantity(text: str, token: str) -> bool:
    """Whether this figure is the shape of a suggestion rather than a finding.

    "Mortality fell by 4.2 percent" asserts something about a study and must be sourced.
    "Spend ten minutes a day" asserts nothing about a study — it is how long to try
    something for. Refusing the second protects nobody and costs every practical tip the
    system would otherwise write.
    """

    for sentence in sentences(text):
        index = sentence.find(token)
        if index == -1:
            continue
        after = sentence[index + len(token) :]
        if _PRACTICE_UNIT.match(after) and _SUGGESTION.search(sentence):
            return True
    return False


def _numbers(
    text: str, bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]
) -> list[Violation]:
    anchored = _anchored_numbers(bundle)
    cited = {key: records[key] for key in bundle.source_keys() if key in records}
    in_sources = _source_numbers(cited)

    violations: list[Violation] = []
    for token in numeric_tokens(text):
        normalised = normalise_number(token)
        if normalised in anchored or normalised in in_sources:
            continue
        if _is_practice_quantity(text, token):
            continue
        violations.append(
            Violation(
                gate="G2",
                detail=(
                    f"Edited text contains {token!r}, which is neither among the figures "
                    "the bundle anchored nor present in any cited source."
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


def _surrogate(
    text: str, bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]
) -> list[Violation]:
    """A biomarker that moved is still not a life that improved.

    The claim-level G5 checks the claims; this checks the sentences actually published.
    The first real tip described two surrogate markers falling and then concluded it was
    "beneficial for managing inflammation and iron metabolism" — the claims were clean and
    the paragraph built on them was not.
    """

    cited = [records[key] for key in bundle.source_keys() if key in records]
    outcomes = [outcome for record in cited for outcome in record.outcomes]
    known = [outcome.is_surrogate for outcome in outcomes if outcome.is_surrogate.is_known]
    if not known or not all(flag.value for flag in known):
        return []

    violations: list[Violation] = []
    for sentence in sentences(text):
        clinical = CLINICAL_LANGUAGE.search(sentence)
        if clinical:
            violations.append(
                Violation(
                    gate="G5",
                    detail=(
                        f"Edited text claims {clinical.group(0)!r} where every measured "
                        "endpoint is a surrogate marker."
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
