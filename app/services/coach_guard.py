"""Bounds on what the coach may say.

The publication pipeline spent four slices making sure an article cannot say "reduces"
without randomised evidence. The coach, meanwhile, answers *personalised* questions in
real time — "I have diabetes, should I fast?", "how much magnesium should I take?" — from
a system prompt and nothing else. It is the most direct channel to a reader in the product
and it had no gate at all. That asymmetry is what this module closes.

Three controls, in the order they apply:

1. **Screen the question.** Some questions have no safe answer from an autonomous system:
   a dose, a diagnosis, a decision about someone's medication, an emergency. Those are
   answered by code with a referral, and the model is never asked — a question the model
   should not answer is a question it should not be handed.
2. **Gate the answer sentence by sentence.** The same lexical claim ceiling the publisher
   uses (:mod:`app.services.evidence.claim_policy`), applied to what the coach is about to
   say. Streaming makes this a live problem: text already sent cannot be retracted, so a
   sentence is held until it is complete and checked, and only then released.
3. **Say what is not known.** Where the evidence store has nothing to support an answer,
   the coach is told to say so rather than improvise, and the strength of what it does
   have decides whether certainty language is permitted at all.

Refusals are written here, in code. They are never generated, so they cannot themselves
trip the gate, and they say the same thing every time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import logging
import re
from typing import Iterable

from app.models.evidence import Violation
from app.services.evidence.claim_policy import check_claim_ceiling

LOGGER = logging.getLogger(__name__)

__all__ = [
    "RequestKind",
    "SentenceGate",
    "classify_request",
    "refusal_for",
    "screen_answer",
]


class RequestKind(StrEnum):
    """What a question is asking for."""

    #: Answerable: general guidance about habits, evidence, or healthy ageing.
    GENERAL = "general"
    #: A medical emergency. Nothing else matters until it is dealt with.
    EMERGENCY = "emergency"
    #: How much of something to take.
    DOSING = "dosing"
    #: What condition the person has.
    DIAGNOSIS = "diagnosis"
    #: Whether to start, stop, or change a prescribed treatment.
    MEDICATION = "medication"


_EMERGENCY = re.compile(
    r"\b(chest pain|crushing pain|can'?t breathe|cannot breathe|struggling to breathe|"
    r"severe bleeding|bleeding heavily|unconscious|passed out|overdose|overdosed|"
    r"suicidal|kill myself|end my life|want to die|stroke symptoms|face drooping|"
    r"slurred speech|numb on one side|anaphylaxis|severe allergic)\b",
    re.IGNORECASE,
)

_DOSING = re.compile(
    r"\bhow (?:much|many)\b.{0,40}\b(take|dose|supplement|mg|grams?|iu)\b"
    r"|\bwhat (?:dose|dosage)\b"
    r"|\b(?:correct|right|safe|optimal|best) (?:dose|dosage|amount)\b"
    r"|\bhow many (?:pills|tablets|capsules)\b"
    r"|\b\d+\s*(?:mg|mcg|iu|grams?)\b.{0,30}\b(safe|too much|enough|should i)\b",
    re.IGNORECASE,
)

_DIAGNOSIS = re.compile(
    r"\bdo i have\b|\bhave i got\b|\bam i (?:diabetic|prediabetic|deficient)\b"
    r"|\bis (?:this|that|it) (?:cancer|diabetes|dementia|a stroke|a heart attack)\b"
    r"|\bwhat(?:'s| is) wrong with me\b|\bdiagnose (?:me|my)\b"
    r"|\bare these symptoms\b"
    r"|\bwhat do my\b[\w\s]{0,24}\b(?:symptoms|results|labs?|bloods?|numbers|levels)\b.{0,10}\bmean\b",
    re.IGNORECASE,
)

_MEDICATION = re.compile(
    r"\bshould i (?:stop|start|quit|come off|reduce|increase|change|switch)\b"
    r".{0,40}\b(medication|medicine|drugs?|pills?|prescription|statins?|metformin|"
    r"insulin|antidepressants?|blood pressure|thyroid)\b"
    r"|\b(?:stop|come off|quit) (?:taking|my)\b.{0,25}\b(medication|medicine|statins?|"
    r"metformin|insulin|prescription)\b"
    r"|\binstead of my (?:medication|medicine|prescription|treatment)\b"
    r"|\bdo i (?:still )?need (?:my|to take)\b.{0,25}\b(medication|medicine|statins?|"
    r"metformin|insulin)\b",
    re.IGNORECASE,
)


#: Written here so they are identical every time and cannot trip the output gate.
_REFUSALS: dict[RequestKind, str] = {
    RequestKind.EMERGENCY: (
        "This sounds like it could be a medical emergency, and I am not able to help with "
        "one. Please contact your local emergency number or urgent care service now. If "
        "someone is with you, tell them what is happening."
    ),
    RequestKind.DOSING: (
        "I cannot suggest doses. How much of anything is right for you depends on your "
        "health, your other medicines and your history, and getting it wrong can cause "
        "real harm — so this is a question for a pharmacist or your doctor, who can see "
        "all of that. I am glad to talk about what the research says about the habit "
        "itself."
    ),
    RequestKind.DIAGNOSIS: (
        "Identifying a condition is not something I can do, and guessing would not be "
        "safe. Symptoms and test results need someone who can examine you and see your "
        "full history — please take this to your doctor. I can talk about general healthy "
        "ageing research in the meantime."
    ),
    RequestKind.MEDICATION: (
        "Decisions about starting, stopping or changing a prescribed medicine belong with "
        "the clinician who prescribed it — stopping some medicines suddenly is dangerous. "
        "Please speak to them before changing anything. I am happy to discuss the "
        "lifestyle research alongside whatever they advise."
    ),
}

#: Appended when the output gate stops a partly-written answer.
GATE_INTERRUPTION = (
    "I have stopped there, because I was about to say something I am not able to stand "
    "behind. If this is about your own treatment or a specific dose, please ask your "
    "doctor or pharmacist."
)


def classify_request(question: str) -> RequestKind:
    """What kind of question this is.

    Order matters: an emergency mentioned alongside a medication question is still an
    emergency.
    """

    text = question or ""

    if _EMERGENCY.search(text):
        return RequestKind.EMERGENCY
    if _MEDICATION.search(text):
        return RequestKind.MEDICATION
    if _DIAGNOSIS.search(text):
        return RequestKind.DIAGNOSIS
    if _DOSING.search(text):
        return RequestKind.DOSING
    return RequestKind.GENERAL


def refusal_for(kind: RequestKind) -> str | None:
    """The standing answer for a question the coach should not attempt."""

    return _REFUSALS.get(kind)


def screen_answer(text: str, *, grade: str = "insufficient") -> tuple[str, list[Violation]]:
    """Check a complete answer, returning the text to send and any violations.

    Used on the non-streaming path, where the whole answer exists before anything is
    shown. A tripped sentence is not edited into safety — everything from that point is
    dropped, because a model that has started down that road is not to be trusted for the
    rest of the paragraph.
    """

    kept: list[str] = []
    violations: list[Violation] = []

    for sentence in _split_sentences(text):
        found = check_claim_ceiling(sentence, grade=grade)
        if found:
            violations.extend(found)
            break
        kept.append(sentence)

    if not violations:
        return text, []

    safe = " ".join(part.strip() for part in kept if part.strip())
    LOGGER.info(
        "Coach answer stopped by the claim ceiling",
        extra={
            "event": "coach.answer_gated",
            "rules": ",".join(sorted({v.detail.split(":")[0] for v in violations})),
        },
    )
    return (f"{safe}\n\n{GATE_INTERRUPTION}" if safe else GATE_INTERRUPTION), violations


@dataclass(slots=True)
class SentenceGate:
    """Release streamed text one checked sentence at a time.

    Streaming is the hard case: a token already sent cannot be recalled, so nothing may be
    emitted until the sentence containing it is complete and has passed the ceiling. The
    cost is that the reader sees text a sentence at a time rather than a word at a time,
    which is a small price for not having to retract a dosing instruction mid-flow.
    """

    grade: str = "insufficient"
    buffer: str = ""
    stopped: bool = False
    violations: list[Violation] = field(default_factory=list)

    def feed(self, chunk: str) -> list[str]:
        """Take a fragment; return whatever is now safe to send."""

        if self.stopped or not chunk:
            return []

        self.buffer += chunk
        released: list[str] = []

        while True:
            sentence, remainder = _take_sentence(self.buffer)
            if sentence is None:
                break
            self.buffer = remainder
            found = check_claim_ceiling(sentence, grade=self.grade)
            if found:
                self.violations.extend(found)
                self.stopped = True
                self.buffer = ""
                released.append(("\n\n" if released else "") + GATE_INTERRUPTION)
                LOGGER.info(
                    "Coach stream stopped by the claim ceiling",
                    extra={"event": "coach.stream_gated"},
                )
                return released
            released.append(sentence)

        return released

    def finish(self) -> list[str]:
        """Flush whatever is left once the model stops producing."""

        if self.stopped:
            return []

        remainder, self.buffer = self.buffer, ""
        if not remainder.strip():
            return []

        found = check_claim_ceiling(remainder, grade=self.grade)
        if found:
            self.violations.extend(found)
            self.stopped = True
            LOGGER.info(
                "Coach stream stopped by the claim ceiling",
                extra={"event": "coach.stream_gated"},
            )
            return [GATE_INTERRUPTION]
        return [remainder]


_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n")


def _take_sentence(buffer: str) -> tuple[str | None, str]:
    """Split off the first complete sentence, or return ``None`` if there is not one."""

    match = _SENTENCE_END.search(buffer)
    if match is None:
        return None, buffer
    end = match.end()
    return buffer[:end], buffer[end:]


def _split_sentences(text: str) -> list[str]:
    parts: list[str] = []
    remainder = text or ""
    while True:
        sentence, remainder = _take_sentence(remainder)
        if sentence is None:
            break
        parts.append(sentence)
    if remainder.strip():
        parts.append(remainder)
    return parts


def gated_rules(violations: Iterable[Violation]) -> set[str]:
    """The ceiling rules that fired, for logging and tests."""

    return {violation.detail.split(":")[0] for violation in violations}
