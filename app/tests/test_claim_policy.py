"""Tests for the claim ceiling (G8).

Both directions matter equally. A ceiling that fires on "was associated with a lower risk
of heart disease" would push the writer toward vaguer prose rather than safer prose, so
the passing cases below are as load-bearing as the failing ones.
"""

from __future__ import annotations

import pytest

from app.services.evidence.claim_policy import check_claim_ceiling, sentences  # noqa: F401


def _rules(text: str, grade: str = "moderate") -> set[str]:
    return {
        violation.detail.split(":")[0]
        for violation in check_claim_ceiling(text, grade=grade)
    }


# -- prose that must pass ----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Fasting glucose fell by 4.2 mg/dL over 12 weeks in a randomised trial.",
        "Adults who walked more had a lower risk of heart disease.",
        "Participants ate within an eight-hour window and lost 2.6 kg on average.",
        "The trial measured LDL cholesterol at 3.1 mmol/L after six months.",
        "Resistance training was associated with better metabolic health in older adults.",
        "Sleeping seven to nine hours supports healthy ageing.",
        "Two servings of oily fish a week were linked to lower inflammation markers.",
    ],
)
def test_ordinary_evidence_reporting_passes(text: str) -> None:
    assert check_claim_ceiling(text, grade="moderate") == []


def test_a_concentration_is_not_a_dose() -> None:
    """mg/dL is a measurement; mg in a mouth is a dose. The trailing slash separates them."""

    assert _rules("Fasting glucose fell by 4.2 mg/dL.") == set()
    assert "dosing" in _rules("Take 4 mg of the supplement each day.")


def test_a_quantity_without_an_instruction_to_consume_it_is_not_a_dose() -> None:
    assert _rules("The capsules contained 500 mg of magnesium.") == set()


# -- dosing ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Take 500 mg of magnesium before bed.",
        "Supplement with 2000 IU of vitamin D daily.",
        "Participants should consume 30 g of protein per day.",
        "Take two capsules twice a day.",
    ],
)
def test_dosing_instructions_are_refused(text: str) -> None:
    assert "dosing" in _rules(text)


# -- individual advice -------------------------------------------------


def test_instructing_a_reader_who_has_a_named_condition_is_refused() -> None:
    assert "individual_advice" in _rules("If you have diabetes, you should avoid this.")


def test_diagnosis_language_is_refused() -> None:
    assert "individual_advice" in _rules("If you feel tired often, you probably have diabetes.")
    assert "individual_advice" in _rules("These symptoms diagnose an underlying thyroid condition.")


def test_describing_a_condition_without_addressing_the_reader_passes() -> None:
    assert _rules("Adults with diabetes showed larger reductions in fasting glucose.") == set()


# -- disease claims ----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "This diet cures type 2 diabetes.",
        "Exercise reverses heart disease.",
        "The supplement prevents Alzheimer's disease.",
        "Fasting treats cancer.",
        "Cold exposure protects you against dementia.",
    ],
)
def test_promising_to_defeat_a_named_disease_is_refused(text: str) -> None:
    assert "disease_claim" in _rules(text)


def test_defeating_something_that_is_not_a_named_disease_passes() -> None:
    """The rule is anchored to disease terms, so ordinary prevention talk survives."""

    assert _rules("Resistance training helps prevent age-related muscle loss.") == set()


# -- care substitution -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Try berberine instead of your medication.",
        "You do not need medication once your numbers improve.",
        "Stop taking your statins if this works.",
        "Many people skip their dose on training days.",
        "Start the protocol without consulting your doctor.",
    ],
)
def test_substituting_or_dropping_medical_care_is_refused(text: str) -> None:
    assert "care_substitution" in _rules(text)


def test_encouraging_medical_consultation_passes() -> None:
    assert _rules("Discuss any change with your doctor before starting.") == set()


# -- certainty ---------------------------------------------------------


def test_certainty_language_is_refused_below_a_high_grade() -> None:
    text = "Time-restricted eating is scientifically proven to extend lifespan."

    assert "superlative_certainty" in _rules(text, grade="moderate")
    assert "superlative_certainty" in _rules(text, grade="preliminary")


def test_certainty_language_is_permitted_at_a_high_grade() -> None:
    """The one rule that consults the evidence, because at 'high' it is simply true."""

    text = "The benefit is proven across multiple randomised trials."

    assert check_claim_ceiling(text, grade="high") == []


# -- mechanics ---------------------------------------------------------


def test_rules_apply_within_a_sentence_not_across_a_page() -> None:
    """Otherwise a dose in one paragraph and 'daily' in another would collide."""

    text = "The capsules contained 500 mg of magnesium. Participants trained daily."

    assert check_claim_ceiling(text, grade="moderate") == []


def test_every_violation_is_reported_with_its_rule_and_gate() -> None:
    violations = check_claim_ceiling(
        "If you have diabetes, take 500 mg of berberine instead of your medication.",
        grade="moderate",
        claim_text="original claim",
    )

    assert {violation.gate for violation in violations} == {"G8"}
    assert {violation.claim_text for violation in violations} == {"original claim"}
    assert {"dosing", "individual_advice", "care_substitution"} <= _rules(
        "If you have diabetes, take 500 mg of berberine instead of your medication."
    )


def test_sentence_splitting_handles_newlines_and_empty_input() -> None:
    assert sentences("One. Two!\n\nThree?") == ["One.", "Two!", "Three?"]
    assert sentences("") == []
    assert check_claim_ceiling("") == []


def test_describing_a_diagnosed_study_population_passes() -> None:
    """Study description must survive the diagnosis rule, or trials become unwritable."""

    assert _rules("Participants were diagnosed with type 2 diabetes at baseline.") == set()
    assert _rules("The cohort had an existing diagnosis of hypertension.") == set()


# -- what the ceiling deliberately does NOT judge ----------------------


@pytest.mark.parametrize("grade", ["preliminary", "low", "moderate", "high"])
@pytest.mark.parametrize(
    "text",
    [
        "Try intermittent fasting.",
        "Start adding more protein to breakfast.",
        "Aim for seven hours of sleep.",
    ],
)
def test_a_suggestion_is_not_refused_for_resting_on_weak_evidence(
    text: str, grade: str
) -> None:
    """The grade badge is how weak evidence is made honest. Refusal is not.

    A rule blocking suggestions below `moderate` lived here briefly. It judged the source
    research rather than the writing, and the purpose of this system is to find research
    worth reporting and report it with its strength attached.
    """

    assert check_claim_ceiling(text, grade=grade) == []


def test_the_ceiling_is_the_five_classes_the_plan_fixed() -> None:
    from app.services.evidence.claim_policy import CEILING_RULES

    assert set(CEILING_RULES) == {
        "dosing",
        "individual_advice",
        "disease_claim",
        "care_substitution",
        "superlative_certainty",
    }


# -- conditionals are not diagnoses ------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "If you have mild to moderate hearing loss, hearing aids can help.",
        "When you have trouble sleeping, a consistent bedtime helps.",
        "People who have joint pain often find swimming easier.",
        "Anyone who has high blood pressure should be monitored by their doctor.",
    ],
)
def test_a_conditional_is_not_a_diagnosis(text: str) -> None:
    """Refusing "if you have X" refuses most practical health writing there is."""

    assert "individual_advice" not in _rules(text)


@pytest.mark.parametrize(
    "text",
    ["You probably have diabetes.", "These symptoms diagnose a thyroid condition."],
)
def test_telling_the_reader_what_they_have_is_still_refused(text: str) -> None:
    assert "individual_advice" in _rules(text)
