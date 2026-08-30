"""Compute the evidence grade, in code.

Invariant I4: grades are computed here from what the sources actually are, and a model may
only argue one down (:func:`app.models.evidence.clamp_grade`). That asymmetry is the whole
point — an autonomous system that can talk itself into a higher grade has no grade at all.

The rubric is improvements.md item 3, one branch per row:

===============  ===========================================================
``high``         SR/meta-analysis of human RCTs, or two independent human
                 RCTs agreeing, with clinical endpoints and no unresolved
                 contradiction.
``moderate``     One human RCT with n >= 100 and a clinical endpoint, or a
                 systematic review of prospective cohorts.
``low``          Human observational evidence only, or an RCT with surrogate
                 endpoints or n < 100.
``preliminary``  Animal, in-vitro, in-silico, preprint, n < 30, or a single
                 exploratory result.
``insufficient`` Anything failing G1, G2, G6 or G10. Never publishes.
===============  ===========================================================

Every branch records why, because "moderate" on its own tells a later reader nothing about
whether the system was being careful or merely lucky.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from app.models.evidence import (
    EvidenceBundle,
    EvidenceRecord,
    Violation,
    clamp_grade,
)
from app.services.evidence.gates import CAP_GRADES, INSUFFICIENT_GATES

__all__ = ["compute_grade", "describe_grade", "provisional_grade"]

#: Designs that pool other studies rather than running one.
_AGGREGATE_DESIGNS = frozenset({"meta_analysis", "systematic_review"})
_COHORT_DESIGNS = frozenset({"prospective_cohort", "retrospective_cohort"})
_OBSERVATIONAL_DESIGNS = _COHORT_DESIGNS | {"case_control", "cross_sectional", "case_report"}

#: Below this, a single trial is suggestive rather than persuasive.
_MODERATE_SAMPLE_FLOOR = 100


def compute_grade(
    bundle: EvidenceBundle,
    records: Mapping[str, EvidenceRecord],
    violations: Sequence[Violation] = (),
) -> tuple[str, list[str]]:
    """Return ``(grade, rationale)`` for ``bundle``.

    ``violations`` are the gate results. Capping gates (G5, G7) lower the ceiling; the
    gates in :data:`INSUFFICIENT_GATES` mean the evidence cannot support publication at
    all, whatever the study designs look like.
    """

    rationale: list[str] = []

    blocking = sorted({v.gate for v in violations if v.gate in INSUFFICIENT_GATES})
    if blocking:
        rationale.append(f"{', '.join(blocking)} failed; the evidence cannot be relied on.")
        return "insufficient", rationale

    cited = [records[key] for key in bundle.source_keys() if key in records]
    if not cited:
        rationale.append("No cited evidence resolved to a stored record.")
        return "insufficient", rationale

    grade = _base_grade(bundle, cited, rationale)

    for gate in sorted({v.gate for v in violations if v.gate in CAP_GRADES}):
        capped = clamp_grade(grade, CAP_GRADES[gate])
        if capped != grade:
            rationale.append(f"{gate} capped the grade at {CAP_GRADES[gate]}.")
            grade = capped

    return grade, rationale


def provisional_grade(records: Sequence[EvidenceRecord]) -> tuple[str, list[str]]:
    """Grade a set of records before any claim has been written.

    Ranking has to choose what to write about *before* synthesis, which means it needs a
    strength estimate without a bundle. This runs the same rubric over the records alone,
    so the ordering candidates are ranked in cannot disagree with the grade they later
    receive — the only differences come from what the claims turn out to say.
    """

    if not records:
        return "insufficient", ["No records."]

    rationale: list[str] = []
    grade = _base_grade(EvidenceBundle(bundle_id="provisional"), records, rationale)
    return grade, rationale


def _base_grade(
    bundle: EvidenceBundle, cited: Sequence[EvidenceRecord], rationale: list[str]
) -> str:
    human = [record for record in cited if record.classification.is_human]
    if not human:
        subjects = sorted({record.classification.subject for record in cited})
        rationale.append(f"No human evidence ({', '.join(subjects)} only).")
        return "preliminary"

    if any(record.source_type == "preprint" for record in human):
        rationale.append("Cited work includes a preprint, which has not been peer reviewed.")
        return "preliminary"

    designs = {record.classification.design for record in human}
    rcts = [record for record in human if record.classification.design == "rct"]
    aggregates = [record for record in human if record.classification.design in _AGGREGATE_DESIGNS]
    contradicted = any(claim.contradicted_by for claim in bundle.claims)
    clinical = _has_clinical_endpoint(human)

    largest = _largest_sample(human)
    if largest is not None and largest < 30:
        rationale.append(f"Largest human sample is {largest}.")
        return "preliminary"

    # "high" is reserved for pooled *randomised* evidence. A systematic review tells us
    # it pooled something, not what: unless the bundle also cites the trials, or the
    # record is indexed as randomised, a lone review could be pooling cohorts. Ambiguity
    # resolves downward here, as everywhere else in this system.
    pooled_randomised = bool(aggregates) and (
        bool(rcts) or any(_indexed_randomised(record) for record in aggregates)
    )

    if pooled_randomised and clinical and not contradicted:
        rationale.append(
            f"Pooled randomised human evidence ({', '.join(sorted(designs & _AGGREGATE_DESIGNS))}) "
            "with clinical endpoints."
        )
        return "high"

    if len(rcts) >= 2 and clinical and not contradicted:
        rationale.append(f"{len(rcts)} independent human trials agree on clinical endpoints.")
        return "high"

    if contradicted:
        rationale.append("Contradicting evidence is on record, so this is not settled.")

    if rcts and clinical and (largest or 0) >= _MODERATE_SAMPLE_FLOOR:
        rationale.append(f"One human trial with {largest} participants and clinical endpoints.")
        return "moderate"

    if aggregates and designs & _COHORT_DESIGNS:
        rationale.append("Pooled observational evidence.")
        return "moderate"

    if aggregates:
        rationale.append(
            "Pooled human evidence, but nothing in the metadata says it pooled trials."
        )
        return "moderate"

    if rcts:
        reason = "surrogate endpoints" if not clinical else f"only {largest or 'an unstated number of'} participants"
        rationale.append(f"Randomised human evidence with {reason}.")
        return "low"

    if designs & _OBSERVATIONAL_DESIGNS:
        rationale.append("Human observational evidence only; association, not causation.")
        return "low"

    rationale.append("Human evidence of an unclassified design.")
    return "preliminary"


def _indexed_randomised(record: EvidenceRecord) -> bool:
    """Whether the record itself is indexed as randomised work."""

    return any(
        "randomized controlled trial" in publication_type.lower()
        for publication_type in record.publication_types
    )


def _largest_sample(records: Iterable[EvidenceRecord]) -> int | None:
    """The biggest reported human sample, or ``None`` when none reported one."""

    sizes = [
        record.sample_size.value
        for record in records
        if record.sample_size.is_known and record.sample_size.value is not None
    ]
    return max(sizes) if sizes else None


def _has_clinical_endpoint(records: Iterable[EvidenceRecord]) -> bool:
    """Whether any cited study measured something other than a surrogate marker.

    An unclassified endpoint does not count as clinical. That keeps the optimistic
    reading — "it probably measured something that matters" — out of the grade.
    """

    for record in records:
        for outcome in record.outcomes:
            if outcome.is_surrogate.is_known and outcome.is_surrogate.value is False:
                return True
    return False


def describe_grade(grade: str, records: Sequence[EvidenceRecord]) -> str:
    """A reader-facing one-liner, built in code rather than written by a model.

    Item 8 of improvements.md asks for "Moderate — one human RCT plus supporting
    observational evidence". The wording is assembled from the records themselves so it
    cannot drift from what was actually cited.
    """

    if grade == "insufficient" or not records:
        return "Not assessed"

    counts: dict[str, int] = {}
    for record in records:
        label = _design_label(record)
        counts[label] = counts.get(label, 0) + 1

    parts = [
        f"{count} {label}" if count == 1 else f"{count} {_pluralise(label)}"
        for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return f"{grade.capitalize()} — {', '.join(parts)}"


#: Study-design names whose plural is not formed by adding an "s".
_IRREGULAR_PLURALS = {"meta-analysis": "meta-analyses", "analysis": "analyses"}


def _pluralise(label: str) -> str:
    """Pluralise a design label. "2 human meta-analysiss" reached a reader once."""

    for singular, plural in _IRREGULAR_PLURALS.items():
        if label.endswith(singular):
            return label[: -len(singular)] + plural
    return f"{label}s"


def _design_label(record: EvidenceRecord) -> str:
    subject = record.classification.subject
    design = record.classification.design
    readable = {
        "meta_analysis": "meta-analysis",
        "systematic_review": "systematic review",
        "rct": "randomised trial",
        "non_randomised_trial": "non-randomised trial",
        "prospective_cohort": "cohort study",
        "retrospective_cohort": "retrospective study",
        "case_control": "case-control study",
        "cross_sectional": "cross-sectional study",
        "case_report": "case report",
        "narrative_review": "review",
        # Deliberately bare: the subject prefix below supplies "animal" or "laboratory",
        # and "animal laboratory study" reads like two labels fighting.
        "preclinical": "study",
        "unknown": "study",
    }.get(design, design.replace("_", " "))

    if subject in ("animal", "in_vitro", "in_silico"):
        prefix = {"animal": "animal", "in_vitro": "laboratory", "in_silico": "modelling"}[subject]
        return f"{prefix} {readable}"
    if subject in ("human", "mixed"):
        return f"human {readable}"
    return readable

