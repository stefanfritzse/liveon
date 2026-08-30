"""Give the coach something to answer from.

Until now it answered from a system prompt and whatever the model happened to remember,
which is the one thing this whole project exists to stop being the basis for a health
claim. The evidence store already holds reviewed, graded bundles; this connects them.

The match is deliberately shallow. A question mentioning fasting is answered with what the
store holds about fasting — no semantic search, no embeddings, just the canonical
vocabulary the clustering already uses (:mod:`app.services.evidence.vocabulary`). Shallow
matching fails in an obvious direction: it finds nothing, and the coach is then told to say
it does not have good evidence, which is a true statement and a safe one. A cleverer
matcher that returns loosely-related evidence would produce answers that *look* grounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re

from app.models.evidence import GRADE_ORDER, EvidenceBundle
from app.services.evidence.vocabulary import INTERVENTION_TERMS

LOGGER = logging.getLogger(__name__)

__all__ = ["CoachEvidence", "EvidenceContext", "topics_in_question"]

#: Spellings a person might use for each canonical topic, beyond the MeSH descriptor
#: itself. Nobody types "Diet, Mediterranean" into a chat box.
_SPOKEN_FORMS: dict[str, tuple[str, ...]] = {
    "intermittent-fasting": (
        "intermittent fasting", "time restricted eating", "time-restricted eating",
        "tre", "16:8", "eating window", "fasting window",
    ),
    "caloric-restriction": ("caloric restriction", "calorie restriction", "eating less"),
    "fasting": ("fasting", "fast"),
    "mediterranean-diet": ("mediterranean diet", "mediterranean"),
    "ketogenic-diet": ("keto", "ketogenic", "low carb"),
    "plant-based-diet": ("plant based", "plant-based", "vegan", "vegetarian"),
    "weight-loss-diet": ("weight loss diet", "dieting"),
    "dietary-protein": ("protein", "high protein"),
    "dietary-fibre": ("fibre", "fiber"),
    "dietary-supplements": ("supplement", "supplements"),
    "resistance-training": ("resistance training", "strength training", "weights", "lifting"),
    "interval-training": ("hiit", "interval training"),
    "endurance-training": ("endurance training", "cardio"),
    "exercise": ("exercise", "working out", "physical activity"),
    "walking": ("walking", "walk", "steps"),
    "sleep": ("sleep", "sleeping", "insomnia", "rest"),
    "circadian-rhythm": ("circadian", "body clock", "chronotype"),
    "light-therapy": ("light therapy", "bright light"),
    "mindfulness": ("mindfulness", "meditation", "meditating"),
    "stress": ("stress",),
    "social-connection": ("loneliness", "lonely", "social connection", "isolation"),
    "smoking-cessation": ("quitting smoking", "stop smoking", "smoking cessation"),
    "alcohol": ("alcohol", "drinking", "wine"),
    "metformin": ("metformin",),
    "rapamycin": ("rapamycin", "sirolimus"),
    "resveratrol": ("resveratrol",),
    "probiotics": ("probiotic", "probiotics"),
    "vitamin-d": ("vitamin d",),
    "omega-3": ("omega 3", "omega-3", "fish oil"),
    "creatine": ("creatine",),
    "coffee": ("coffee", "caffeine"),
    "cold-exposure": ("cold plunge", "cold exposure", "ice bath", "cold shower"),
    "sauna": ("sauna",),
}


def topics_in_question(question: str) -> list[str]:
    """Canonical topics a question mentions, longest match first.

    Longest first so "intermittent fasting" is not answered as though it said "fasting".
    """

    text = (question or "").lower()
    if not text:
        return []

    candidates: list[tuple[int, str]] = []
    known = {canonical for _descriptor, canonical in INTERVENTION_TERMS}

    for canonical, spellings in _SPOKEN_FORMS.items():
        if canonical not in known:
            continue
        for spelling in spellings:
            if re.search(rf"\b{re.escape(spelling)}\b", text):
                candidates.append((len(spelling), canonical))
                break

    ordered = [canonical for _length, canonical in sorted(candidates, reverse=True)]
    seen: set[str] = set()
    return [topic for topic in ordered if not (topic in seen or seen.add(topic))]


@dataclass(slots=True)
class EvidenceContext:
    """What the store can offer about a question."""

    topics: list[str] = field(default_factory=list)
    bundles: list[EvidenceBundle] = field(default_factory=list)

    @property
    def best_grade(self) -> str:
        """The strongest grade retrieved; ``insufficient`` when there is nothing.

        This decides whether the coach may use certainty language at all, so an empty
        result has to mean "no", not "unknown".
        """

        grades = [bundle.grade for bundle in self.bundles if bundle.grade in GRADE_ORDER]
        return max(grades, key=GRADE_ORDER.index) if grades else "insufficient"

    @property
    def is_supported(self) -> bool:
        """Whether anything retrieved is strong enough to answer from."""

        return GRADE_ORDER.index(self.best_grade) >= GRADE_ORDER.index("low")

    def prompt_block(self) -> str:
        """Render the evidence for the coach's prompt, grades included."""

        if not self.bundles:
            return "No reviewed evidence was found for this question."

        lines: list[str] = []
        for bundle in self.bundles:
            lines.append(f"Topic: {bundle.topic_key} (evidence grade: {bundle.grade})")
            for claim in bundle.claims:
                lines.append(f"  - {claim.text}")
                if claim.limitations:
                    lines.append(f"    limits: {'; '.join(claim.limitations)}")
        return "\n".join(lines)

    def instruction(self) -> str:
        """What to tell the coach about the strength of what it has."""

        if not self.bundles:
            return (
                "The evidence store has nothing reviewed on this. Say plainly that you do "
                "not have good evidence for it, offer only general, well-established "
                "healthy-ageing principles, and do not fill the gap from memory."
            )
        if not self.is_supported:
            return (
                f"The strongest reviewed evidence here is graded {self.best_grade}, which is "
                "weak. Say so in your own words, describe what was found without "
                "recommending it, and do not imply the question is settled."
            )
        return (
            f"The reviewed evidence here is graded {self.best_grade}. Stay within what the "
            "claims below actually say, keep their hedging, and mention their limits."
        )


@dataclass(slots=True)
class CoachEvidence:
    """Looks up reviewed evidence for a question."""

    store: object | None = None
    max_bundles: int = 3

    def for_question(self, question: str) -> EvidenceContext:
        """Return what the store holds about this question, or an empty context."""

        topics = topics_in_question(question)
        if not topics or self.store is None:
            return EvidenceContext(topics=topics)

        bundles: list[EvidenceBundle] = []
        try:
            for topic in topics:
                bundles.extend(
                    self.store.approved_bundles(topic, limit=self.max_bundles - len(bundles))
                )
                if len(bundles) >= self.max_bundles:
                    break
        except Exception as exc:  # noqa: BLE001 - the coach must answer even if the store is down
            LOGGER.warning(
                "Could not retrieve coach evidence: %s",
                exc,
                extra={"event": "coach.evidence_unavailable"},
            )
            return EvidenceContext(topics=topics)

        LOGGER.info(
            "Retrieved %s bundle(s) for coach topics %s",
            len(bundles),
            ",".join(topics),
            extra={"event": "coach.evidence_retrieved", "topics": ",".join(topics)},
        )
        return EvidenceContext(topics=topics, bundles=bundles[: self.max_bundles])


def summarise(context: EvidenceContext) -> str:
    """One line for the run log."""

    if not context.topics:
        return "no recognised topic"
    return f"{','.join(context.topics)} -> {len(context.bundles)} bundle(s), {context.best_grade}"
