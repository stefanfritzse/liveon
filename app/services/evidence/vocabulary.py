"""Canonical names for the things Live On writes about.

The first live run of the evidence pipeline fetched ten randomised trials of
time-restricted eating and produced ten separate topics, because clustering keyed on the
intervention phrase the *model* extracted:

    'early time-restricted eating (eTRE) and/or…'
    '16:8 TRE regimen (16 h fasting, 8 h eating)'
    '10-h time-restricted eating (TRE)'
    <not_extractable>

Free text cannot be a key. Every one of those ten records, however, carried the MeSH
descriptor **Intermittent Fasting**, assigned by a human indexer at the National Library
of Medicine — a controlled vocabulary that says the same thing the same way every time.

So topics are named from MeSH, not from prose. This is invariant I1 again, in a place it
had quietly been violated: the model may describe an intervention, but it does not get to
decide what the intervention *is*.

The vocabulary below is curated and therefore incomplete. That is a deliberate trade: a
term we have not mapped falls through to a weaker key rather than being guessed at, and
adding a term is a one-line change with a test beside it. The principled alternative is to
fetch MeSH tree numbers and classify by tree position (E02 Therapeutics, G07 Physiological
Phenomena); that would generalise, and it is the right next step if this list starts
needing frequent additions.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

__all__ = [
    "CHECK_TAGS",
    "INTERVENTION_TERMS",
    "canonical_intervention",
    "fallback_topic",
    "topical_terms",
]

_SLUG_RE = re.compile(r"[^a-z0-9]+")


#: MeSH descriptors that describe *who was studied* or *how the study was run*, never what
#: it was about. NLM calls most of these check tags. They are shared by almost every
#: clinical paper, so clustering on them would merge the entire corpus into one topic.
CHECK_TAGS: frozenset[str] = frozenset(
    {
        # Species and demographics
        "humans", "animals", "male", "female", "adult", "young adult", "adolescent",
        "child", "child, preschool", "infant", "infant, newborn", "aged",
        "aged, 80 and over", "middle aged", "pregnancy", "mice", "rats", "dogs",
        "swine", "rabbits", "cattle",
        # Study design and conduct
        "time factors", "treatment outcome", "cross-over studies", "double-blind method",
        "single-blind method", "prospective studies", "retrospective studies",
        "cohort studies", "follow-up studies", "longitudinal studies",
        "case-control studies", "cross-sectional studies", "reproducibility of results",
        "risk factors", "surveys and questionnaires", "pilot projects",
        "randomized controlled trials as topic", "research design", "sample size",
        "feasibility studies", "intention to treat analysis", "patient compliance",
        "self report", "reference values", "predictive value of tests",
        # Places and administration
        "united states", "europe", "asia", "quality of life",
    }
)


#: MeSH descriptor → canonical topic, ordered most specific first. A record indexed with
#: both "Intermittent Fasting" and "Fasting" is about the former; the ordering is what
#: makes that true rather than a coin toss.
INTERVENTION_TERMS: tuple[tuple[str, str], ...] = (
    # Eating patterns
    ("intermittent fasting", "intermittent-fasting"),
    ("caloric restriction", "caloric-restriction"),
    ("diet, ketogenic", "ketogenic-diet"),
    ("diet, mediterranean", "mediterranean-diet"),
    ("diet, vegetarian", "plant-based-diet"),
    ("diet, vegan", "plant-based-diet"),
    ("diet, reducing", "weight-loss-diet"),
    ("diet, high-protein", "dietary-protein"),
    ("dietary proteins", "dietary-protein"),
    ("dietary fiber", "dietary-fibre"),
    ("dietary supplements", "dietary-supplements"),
    ("fasting", "fasting"),
    ("energy intake", "energy-intake"),
    ("feeding behavior", "eating-behaviour"),
    # Movement
    ("resistance training", "resistance-training"),
    ("high-intensity interval training", "interval-training"),
    ("endurance training", "endurance-training"),
    ("exercise therapy", "exercise"),
    ("exercise", "exercise"),
    ("walking", "walking"),
    ("physical fitness", "physical-fitness"),
    ("sedentary behavior", "sedentary-behaviour"),
    # Sleep and light
    ("sleep hygiene", "sleep"),
    ("sleep duration", "sleep"),
    ("sleep quality", "sleep"),
    ("sleep", "sleep"),
    ("circadian rhythm", "circadian-rhythm"),
    ("chronotherapy", "circadian-rhythm"),
    ("phototherapy", "light-therapy"),
    # Mind and social
    ("mindfulness", "mindfulness"),
    ("meditation", "mindfulness"),
    ("stress, psychological", "stress"),
    ("social support", "social-connection"),
    ("loneliness", "social-connection"),
    # Substances
    ("smoking cessation", "smoking-cessation"),
    ("alcohol drinking", "alcohol"),
    ("metformin", "metformin"),
    ("sirolimus", "rapamycin"),
    ("resveratrol", "resveratrol"),
    ("probiotics", "probiotics"),
    ("vitamin d", "vitamin-d"),
    ("fatty acids, omega-3", "omega-3"),
    ("creatine", "creatine"),
    ("coffee", "coffee"),
    # Exposure
    ("cold temperature", "cold-exposure"),
    ("steam bath", "sauna"),
    ("hyperthermia, induced", "sauna"),
)


def _normalise(terms: Iterable[str] | None) -> list[str]:
    if not terms:
        return []
    return [str(term).strip().lower() for term in terms if str(term).strip()]


def topical_terms(mesh_terms: Iterable[str] | None) -> list[str]:
    """MeSH descriptors that say what a paper is *about*, in their original order."""

    return [term for term in _normalise(mesh_terms) if term not in CHECK_TAGS]


def canonical_intervention(mesh_terms: Iterable[str] | None) -> str | None:
    """The canonical topic for these MeSH descriptors, or ``None`` if unmapped.

    Returns the most specific mapped term, so a paper indexed with both
    "Intermittent Fasting" and "Fasting" is about intermittent fasting.
    """

    indexed = set(_normalise(mesh_terms))
    for descriptor, canonical in INTERVENTION_TERMS:
        if descriptor in indexed:
            return canonical
    return None


def fallback_topic(mesh_terms: Iterable[str] | None) -> str | None:
    """A weaker canonical key: the alphabetically first topical descriptor.

    Used when nothing in the vocabulary matches. Still a controlled term rather than
    model prose, so two papers indexed alike still meet — but it may well name a
    condition or a measurement rather than an intervention, which is why it is the
    fallback and not the rule. A topic that keeps landing here wants a vocabulary entry.
    """

    topical = sorted(set(topical_terms(mesh_terms)))
    return slugify(topical[0]) if topical else None


def slugify(value: str, *, max_words: int = 4) -> str:
    """Lowercase, hyphenated, and capped — keys must stay comparable across runs."""

    slug = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    words = [part for part in slug.split("-") if part]
    return "-".join(words[:max_words])


def canonical_topic(mesh_terms: Iterable[str] | None, *, fallback: bool = True) -> str | None:
    """The best canonical name available for these descriptors."""

    canonical = canonical_intervention(mesh_terms)
    if canonical:
        return canonical
    return fallback_topic(mesh_terms) if fallback else None


def shared_canonical_topics(records_mesh: Sequence[Iterable[str]]) -> set[str]:
    """Canonical topics common to every record. Used by tests and diagnostics."""

    topics = [set(filter(None, [canonical_topic(mesh)])) for mesh in records_mesh]
    if not topics:
        return set()
    common = topics[0]
    for other in topics[1:]:
        common &= other
    return common
