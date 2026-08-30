"""The claim ceiling: what an autonomous publication may never say.

Gate **G8**. Unlike the other gates this one does not ask how good the evidence is — it
refuses certain classes of statement at *any* grade, because nobody reviews what this
system publishes before readers see it. A perfectly evidenced dosing instruction is still
a dosing instruction handed to an unknown reader with unknown medication and unknown
conditions.

The rules are lexical and authoritative: when a pattern fires, no model opinion overrides
it. improvements.md pairs this with an LLM paraphrase classifier in the reviewer's
advisory pass; that layer can only *add* violations, never clear one raised here.

Precision matters as much as recall. Health writing legitimately says "was associated with
a lower risk of heart disease" and "fell by 4.2 mg/dL", and a rule that fires on those
would push writers toward vaguer prose rather than safer prose. Every pattern below is
therefore anchored to the thing that makes the sentence unsafe: a dose paired with an
instruction to take it, a disease paired with a verb that promises to defeat it, a reader
paired with a directive about their own condition.
"""

from __future__ import annotations

import re
from typing import Iterable

from app.models.evidence import Violation

__all__ = ["CEILING_RULES", "check_claim_ceiling", "sentences"]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# -- 1. dosing and protocol specifics ---------------------------------------

#: Units an intervention is *taken* in. Concentrations (mg/dL, mmol/L) are reported
#: measurements, not doses, and are excluded below by the trailing-slash guard.
_DOSE_UNITS = r"(?:mg|mcg|µg|ug|g|kg|iu|ml|cc|capsules?|tablets?|pills?|servings?|scoops?|drops?)"
#: Spelled-out amounts count too — "take two capsules twice a day" is a dose.
_DOSE_WORDS = r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|half|a|an)"
_DOSE_AMOUNT = re.compile(
    rf"\b(?:\d[\d,]*(?:\.\d+)?|{_DOSE_WORDS})\s*{_DOSE_UNITS}\b(?!\s*/)", re.IGNORECASE
)
#: A quantity only becomes a dose when the sentence tells someone to consume it.
_DOSE_CONTEXT = re.compile(
    r"\b(take|takes|taking|taken|dose|doses|dosage|dosing|supplement|supplementing|"
    r"supplementation|swallow|ingest|consume|consuming|drink|eat)\b"
    r"|\b(?:per|a|each|every)\s+day\b|\bdaily\b|\btwice\b|\bthree times\b|\bbefore bed\b",
    re.IGNORECASE,
)

# -- 2. diagnosis and individualised medical advice -------------------------

_CONDITIONS = (
    r"cancer|tumou?rs?|alzheimer'?s?|dementia|parkinson'?s?|diabet(?:es|ic)|"
    r"heart disease|cardiovascular disease|stroke|hypertension|high blood pressure|"
    r"arthritis|osteoporosis|depression|anxiety disorder|obesity|copd|asthma|"
    r"kidney disease|liver disease|covid(?:-19)?|infection|dyslipidemia"
)
_CONDITION_RE = re.compile(rf"\b(?:{_CONDITIONS})\b", re.IGNORECASE)
_SECOND_PERSON = re.compile(r"\b(you|your|yours|yourself)\b", re.IGNORECASE)
_DIRECTIVE = re.compile(
    r"\b(should|must|need to|ought to|have to|take|stop|start|switch|increase|reduce|avoid)\b",
    re.IGNORECASE,
)
#: Active diagnosis only. "Participants were diagnosed with type 2 diabetes at baseline"
#: describes a study population and must survive; "these symptoms diagnose X" does not.
_DIAGNOSIS = re.compile(
    r"\b(?:diagnose|diagnoses|diagnosing)\b|\bself-diagnos\w*\b"
    r"|\byou (?:probably |likely |may |might )?have\b",
    re.IGNORECASE,
)

# -- 3. defeating a named disease -------------------------------------------

_DEFEAT_VERBS = re.compile(
    r"\b(cure[sd]?|curing|reverse[sd]?|reversing|prevent[sd]?|preventing|prevention of|"
    r"treat[s]?|treated|treating|eliminate[sd]?|eradicat\w+|protects? (?:you )?(?:from|against))\b",
    re.IGNORECASE,
)

# -- 4. discontinuing or substituting medical care --------------------------

_CARE_SUBSTITUTION = re.compile(
    r"\b(?:instead of|in place of|rather than)\s+(?:your\s+)?"
    r"(?:medication|medicine|drugs?|prescription|treatment|therapy|doctor|statins?|metformin)\b"
    r"|\b(?:you )?(?:do not|don'?t|no longer) need (?:your |any )?"
    r"(?:medication|medicine|drugs?|prescription|treatment|doctor)\b"
    r"|\bstop taking (?:your )?(?:medication|medicine|drugs?|prescription|statins?|metformin)\b"
    r"|\bskip (?:your |their |a |the )?(?:dose|medication|treatment)\b"
    r"|\bwithout (?:consulting|telling|asking) (?:your |a )?doctor\b",
    re.IGNORECASE,
)

# -- 5. superlative certainty ------------------------------------------------

_SUPERLATIVE = re.compile(
    r"\b(proven|proves|clinically proven|scientifically proven|guaranteed?|"
    r"definitive(?:ly)?|conclusive(?:ly)?|undeniabl[ey]|irrefutabl[ey]|"
    r"miracle|cure-all|breakthrough)\b",
    re.IGNORECASE,
)

#: Human-readable rule names, in the order improvements.md 0.2 lists them.
CEILING_RULES: tuple[str, ...] = (
    "dosing",
    "individual_advice",
    "disease_claim",
    "care_substitution",
    "superlative_certainty",
)


def sentences(text: str) -> list[str]:
    """Split prose into sentences. Rules apply within a sentence, not across a page."""

    return [part.strip() for part in _SENTENCE_SPLIT.split(text or "") if part.strip()]


def check_claim_ceiling(
    text: str,
    *,
    grade: str = "insufficient",
    claim_text: str = "",
    source_key: str = "",
) -> list[Violation]:
    """Return G8 violations for ``text``.

    ``grade`` is consulted for one rule only: certainty language is permitted in a ``high``
    grade bundle and nowhere else. Everything else is refused at every grade.
    """

    violations: list[Violation] = []
    attribution = claim_text or text

    for sentence in sentences(text):
        for rule, detail in _violations_in(sentence, grade=grade):
            violations.append(
                Violation(
                    gate="G8",
                    detail=f"{rule}: {detail}",
                    claim_text=attribution,
                    source_key=source_key,
                )
            )
    return violations


def _violations_in(sentence: str, *, grade: str) -> Iterable[tuple[str, str]]:
    dose = _DOSE_AMOUNT.search(sentence)
    if dose and _DOSE_CONTEXT.search(sentence):
        yield "dosing", f"names a dose to take ({dose.group(0).strip()})"

    has_condition = bool(_CONDITION_RE.search(sentence))
    addresses_reader = bool(_SECOND_PERSON.search(sentence))

    if _DIAGNOSIS.search(sentence):
        yield "individual_advice", "reads as diagnosis rather than description"
    elif has_condition and addresses_reader and _DIRECTIVE.search(sentence):
        yield "individual_advice", "instructs a reader who has a named condition"

    defeat = _DEFEAT_VERBS.search(sentence)
    if defeat and has_condition:
        yield "disease_claim", f"claims to {defeat.group(0).strip().lower()} a named disease"

    substitution = _CARE_SUBSTITUTION.search(sentence)
    if substitution:
        yield "care_substitution", f"suggests replacing medical care ({substitution.group(0).strip()})"

    superlative = _SUPERLATIVE.search(sentence)
    if superlative and grade != "high":
        yield (
            "superlative_certainty",
            f"states certainty ({superlative.group(0).strip()}) at grade {grade!r}",
        )
