"""Tests for the bounds on the coach.

The coach answers personalised health questions in real time, which makes it the most
direct channel to a reader in this product. Everything here is about what it will not do:
the questions it declines outright, the sentences it stops mid-answer, and the fact that
streamed text is checked before it is sent rather than after.

The passing cases matter as much as the refusals. A coach that cannot say "try a
ten-minute walk after dinner" is not safe, it is useless.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from app.models.coach import CoachQuestion
from app.services.coach import CoachAgent
from app.services.coach_evidence import EvidenceContext
from app.services.coach_guard import (
    GATE_INTERRUPTION,
    RequestKind,
    SentenceGate,
    classify_request,
    refusal_for,
    screen_answer,
)
from app.models.evidence import Claim, EvidenceBundle


class _LLM:
    """Returns a fixed answer, and records what it was asked."""

    def __init__(self, answer: str = "Walk after meals. It helps.") -> None:
        self.answer = answer
        self.prompts: list[Any] = []

    def invoke(self, messages: Any) -> str:
        self.prompts.append(messages)
        return self.answer


class _StreamingLLM:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.prompts: list[Any] = []

    def stream(self, messages: Any) -> Iterator[str]:
        self.prompts.append(messages)
        yield from self.chunks

    def invoke(self, messages: Any) -> str:  # pragma: no cover - streaming preferred
        return "".join(self.chunks)


# -- screening the question --------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("I have crushing chest pain, what should I do?", RequestKind.EMERGENCY),
        ("I think I want to end my life", RequestKind.EMERGENCY),
        ("How much magnesium should I take?", RequestKind.DOSING),
        ("What dose of vitamin D is best?", RequestKind.DOSING),
        ("Do I have diabetes?", RequestKind.DIAGNOSIS),
        ("What do my blood results mean?", RequestKind.DIAGNOSIS),
        ("Should I stop taking my statins?", RequestKind.MEDICATION),
        ("Can I use berberine instead of my medication?", RequestKind.MEDICATION),
    ],
)
def test_questions_with_no_safe_answer_are_recognised(
    question: str, expected: RequestKind
) -> None:
    assert classify_request(question) is expected


@pytest.mark.parametrize(
    "question",
    [
        "Does time-restricted eating help with weight?",
        "What does the research say about strength training after 60?",
        "How can I sleep better?",
        "Is walking after meals worth doing?",
        "Why does muscle mass matter as I age?",
    ],
)
def test_ordinary_coaching_questions_are_answered(question: str) -> None:
    """A coach that declines everything is useless, not safe."""

    assert classify_request(question) is RequestKind.GENERAL


def test_an_emergency_outranks_everything_else() -> None:
    """Mentioning medication does not make chest pain a medication question."""

    assert (
        classify_request("I have chest pain, should I stop taking my statins?")
        is RequestKind.EMERGENCY
    )


def test_every_declined_kind_has_a_standing_answer() -> None:
    for kind in RequestKind:
        if kind is RequestKind.GENERAL:
            assert refusal_for(kind) is None
        else:
            answer = refusal_for(kind)
            assert answer and len(answer) > 40


def test_refusals_do_not_trip_the_gate_they_belong_to() -> None:
    """They are code-written, and they mention medicines and doctors by necessity."""

    for kind in RequestKind:
        answer = refusal_for(kind)
        if answer:
            text, violations = screen_answer(answer)
            assert violations == []
            assert text == answer


# -- the model is not asked what it should not answer -------------------


@pytest.mark.parametrize(
    "question",
    [
        "How much creatine should I take?",
        "Do I have prediabetes?",
        "Should I stop taking my metformin?",
        "I cannot breathe properly",
    ],
)
def test_the_model_is_never_asked_a_question_it_should_not_answer(question: str) -> None:
    llm = _LLM()

    answer = CoachAgent(llm=llm).ask(question)

    assert llm.prompts == []
    assert any(
        word in answer.message
        for word in ("doctor", "pharmacist", "clinician", "emergency")
    )


def test_a_declined_question_is_declined_when_streaming_too() -> None:
    llm = _StreamingLLM(["Take ", "500 mg."])

    fragments = list(CoachAgent(llm=llm).stream("What dose of magnesium should I take?"))

    assert llm.prompts == []
    assert len(fragments) == 1
    assert "pharmacist" in fragments[0]


# -- gating the answer -------------------------------------------------


def test_an_ordinary_answer_passes_through_untouched() -> None:
    answer = "Try a ten-minute walk after dinner. It is one of the easier habits to keep."

    text, violations = screen_answer(answer)

    assert text == answer
    assert violations == []


def test_an_answer_that_names_a_dose_is_cut_at_that_sentence() -> None:
    answer = "Magnesium is worth discussing. Take 400 mg before bed. It helps sleep."

    text, violations = screen_answer(answer)

    assert "Magnesium is worth discussing." in text
    assert "400 mg" not in text
    assert "It helps sleep" not in text  # everything after the trip is dropped
    assert GATE_INTERRUPTION in text
    assert violations


def test_certainty_language_is_bounded_by_the_evidence_grade() -> None:
    answer = "This is proven to extend lifespan."

    assert screen_answer(answer, grade="insufficient")[1]
    assert screen_answer(answer, grade="high")[1] == []


def test_an_ungrounded_answer_gets_no_certainty_allowance() -> None:
    """No retrieved evidence must mean "no support", not "unknown"."""

    llm = _LLM("This is proven to work.")

    message = CoachAgent(llm=llm).ask("Does fasting help?").message

    assert "proven" not in message
    assert GATE_INTERRUPTION in message


# -- gating a stream ---------------------------------------------------


def test_a_stream_releases_only_complete_checked_sentences() -> None:
    gate = SentenceGate()

    assert gate.feed("Walk after ") == []
    assert gate.feed("meals.") == ["Walk after meals."]
    assert gate.stopped is False


def test_a_stream_stops_before_sending_a_bad_sentence() -> None:
    """The whole point: the harmful sentence is never sent, not corrected afterwards."""

    gate = SentenceGate()

    gate.feed("Sleep is important. ")
    released = gate.feed("Take 500 mg of magnesium daily. And then rest.")

    assert released == [GATE_INTERRUPTION]
    assert gate.stopped is True
    assert gate.violations


def test_a_stopped_stream_ignores_everything_after() -> None:
    gate = SentenceGate()
    gate.feed("Take 500 mg daily.")

    assert gate.feed("More text here.") == []
    assert gate.finish() == []


def test_a_trailing_fragment_is_checked_before_it_is_flushed() -> None:
    gate = SentenceGate()
    gate.feed("Take 500 mg of magnesium daily")

    assert gate.finish() == [GATE_INTERRUPTION]
    assert gate.stopped is True


def test_a_clean_trailing_fragment_is_flushed() -> None:
    gate = SentenceGate()
    gate.feed("Keep moving")

    assert gate.finish() == ["Keep moving"]


def test_the_agent_stops_a_stream_mid_answer() -> None:
    llm = _StreamingLLM(["Sleep matters. ", "Take 500 mg of magnesium. ", "Rest well."])

    fragments = list(CoachAgent(llm=llm).stream("How do I sleep better?"))

    assert "Sleep matters." in fragments[0]
    assert not any("500 mg" in fragment for fragment in fragments)
    assert GATE_INTERRUPTION in fragments[-1]


# -- grounding ---------------------------------------------------------


class _Evidence:
    def __init__(self, context: EvidenceContext) -> None:
        self.context = context
        self.asked: list[str] = []

    def for_question(self, question: str) -> EvidenceContext:
        self.asked.append(question)
        return self.context


def _bundle(grade: str = "moderate") -> EvidenceBundle:
    return EvidenceBundle(
        bundle_id="b1",
        topic_key="intermittent-fasting|weight",
        grade=grade,
        review_status="approved",
        claims=[
            Claim(
                text="Time-restricted eating was associated with modest weight loss.",
                limitations=["short trials"],
            )
        ],
    )


def test_retrieved_evidence_reaches_the_prompt() -> None:
    llm = _LLM()
    evidence = _Evidence(EvidenceContext(topics=["intermittent-fasting"], bundles=[_bundle()]))

    CoachAgent(llm=llm, evidence=evidence).ask("Does time-restricted eating help?")

    prompt = " ".join(str(message) for message in llm.prompts[0])
    assert "associated with modest weight loss" in prompt
    assert "short trials" in prompt
    assert "graded moderate" in prompt


def test_an_empty_store_tells_the_coach_to_say_so() -> None:
    llm = _LLM()
    evidence = _Evidence(EvidenceContext(topics=["sauna"], bundles=[]))

    CoachAgent(llm=llm, evidence=evidence).ask("Is sauna good for me?")

    prompt = " ".join(str(message) for message in llm.prompts[0])
    assert "nothing reviewed on this" in prompt
    assert "do not fill the gap" in prompt


def test_weak_evidence_is_described_as_weak() -> None:
    llm = _LLM()
    evidence = _Evidence(
        EvidenceContext(topics=["fasting"], bundles=[_bundle(grade="preliminary")])
    )

    CoachAgent(llm=llm, evidence=evidence).ask("Does fasting work?")

    prompt = " ".join(str(message) for message in llm.prompts[0])
    assert "which is weak" in prompt


def test_a_broken_store_does_not_stop_the_coach_answering() -> None:
    class _Broken:
        def for_question(self, question: str) -> EvidenceContext:
            raise RuntimeError("store is down")

    answer = CoachAgent(llm=_LLM(), evidence=_Broken()).ask("Does walking help?")

    assert answer.message


def test_the_hard_limits_are_stated_to_the_model_as_well_as_enforced() -> None:
    """Enforcement is the guarantee; saying it means most answers never reach the gate."""

    llm = _LLM()

    CoachAgent(llm=llm).ask("How do I stay healthy?")

    prompt = " ".join(str(message) for message in llm.prompts[0])
    assert "do not give doses" in prompt
    assert "do not diagnose" in prompt


def test_the_coach_still_works_with_no_evidence_layer_configured() -> None:
    """The offline responder and the existing tests depend on this."""

    answer = CoachAgent(llm=_LLM("Walking is good for you.")).ask(
        CoachQuestion(text="Is walking good?")
    )

    assert answer.message == "Walking is good for you."
