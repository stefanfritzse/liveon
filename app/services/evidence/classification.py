"""Decide study design and subject from indexed metadata, in code.

PubMed already answers the two questions that decide how strongly a finding may be
stated — "was this randomised?" and "was this in people?" — through publication types and
MeSH descriptors assigned by human indexers. Asking a language model to re-derive them
from the abstract would add a confabulation path and remove the audit trail, so this
module owns both answers and records which terms produced them (invariant I1).

The classification is deliberately conservative. Where the metadata does not say, the
answer is ``unknown``, which caps the evidence grade at ``insufficient`` under G10 rather
than letting a plausible guess through.
"""

from __future__ import annotations

from app.models.evidence import Classification, EvidenceRecord

__all__ = ["classify", "classify_record"]


def _normalise(terms: object) -> list[str]:
    if not isinstance(terms, (list, tuple, set, frozenset)):
        return []
    return [str(term).strip().lower() for term in terms if str(term).strip()]


# -- subject -----------------------------------------------------------------

_HUMAN_TERMS = frozenset({"humans", "human"})

#: Species descriptors PubMed indexes alongside (or instead of) the bare "Animals" term.
_ANIMAL_TERMS = frozenset(
    {
        "animals",
        "mice",
        "rats",
        "mice, inbred c57bl",
        "rats, sprague-dawley",
        "rats, wistar",
        "drosophila melanogaster",
        "drosophila",
        "caenorhabditis elegans",
        "zebrafish",
        "danio rerio",
        "macaca mulatta",
        "dogs",
        "swine",
        "rabbits",
    }
)

_IN_VITRO_TERMS = frozenset(
    {
        "in vitro techniques",
        "cells, cultured",
        "cell line",
        "cell culture techniques",
        "organoids",
    }
)

_IN_SILICO_TERMS = frozenset({"computer simulation", "models, theoretical", "computational biology"})


# -- design ------------------------------------------------------------------

#: Publication types, most decisive first. Order matters: a meta-analysis of trials is
#: indexed with both "Meta-Analysis" and "Review", and the stronger label must win.
_DESIGN_BY_PUBLICATION_TYPE: tuple[tuple[str, str], ...] = (
    ("meta-analysis", "meta_analysis"),
    ("systematic review", "systematic_review"),
    ("randomized controlled trial", "rct"),
    ("controlled clinical trial", "non_randomised_trial"),
    ("clinical trial, phase iii", "non_randomised_trial"),
    ("clinical trial, phase ii", "non_randomised_trial"),
    ("clinical trial, phase i", "non_randomised_trial"),
    ("clinical trial", "non_randomised_trial"),
    ("case reports", "case_report"),
    ("review", "narrative_review"),
)

#: MeSH descriptors that identify an observational design when no trial type is indexed.
_DESIGN_BY_MESH: tuple[tuple[str, str], ...] = (
    ("prospective studies", "prospective_cohort"),
    ("cohort studies", "prospective_cohort"),
    ("longitudinal studies", "prospective_cohort"),
    ("follow-up studies", "prospective_cohort"),
    ("retrospective studies", "retrospective_cohort"),
    ("case-control studies", "case_control"),
    ("cross-sectional studies", "cross_sectional"),
)

#: Source types that are their own answer, whatever else is indexed.
_DESIGN_BY_SOURCE_TYPE: dict[str, str] = {
    "meta_analysis": "meta_analysis",
    "systematic_review": "systematic_review",
}


def classify(
    *,
    publication_types: object = (),
    mesh_terms: object = (),
    source_type: str = "",
) -> Classification:
    """Return the design and subject implied by ``publication_types`` and ``mesh_terms``."""

    types = _normalise(publication_types)
    mesh = _normalise(mesh_terms)
    basis: list[str] = []

    subject = _subject(mesh, basis)
    design = _design(types, mesh, source_type, basis)

    # A finding in mice is preclinical even when nothing labels the design, and calling
    # that "unknown" would block it under G10 for the wrong reason.
    if design == "unknown" and subject in ("animal", "in_vitro", "in_silico"):
        design = "preclinical"
        basis.append(f"subject:{subject}")

    return Classification(design=design, subject=subject, basis=tuple(basis))


def classify_record(record: EvidenceRecord) -> Classification:
    """Classify an acquired record from the metadata it arrived with."""

    return classify(
        publication_types=record.publication_types,
        mesh_terms=record.mesh_terms,
        source_type=record.source_type,
    )


def _subject(mesh: list[str], basis: list[str]) -> str:
    terms = set(mesh)
    has_human = bool(terms & _HUMAN_TERMS)
    has_animal = bool(terms & _ANIMAL_TERMS)

    if has_human and has_animal:
        basis.extend(sorted(terms & (_HUMAN_TERMS | _ANIMAL_TERMS)))
        return "mixed"
    if has_human:
        basis.extend(sorted(terms & _HUMAN_TERMS))
        return "human"
    if has_animal:
        basis.extend(sorted(terms & _ANIMAL_TERMS))
        return "animal"
    if terms & _IN_VITRO_TERMS:
        basis.extend(sorted(terms & _IN_VITRO_TERMS))
        return "in_vitro"
    if terms & _IN_SILICO_TERMS:
        basis.extend(sorted(terms & _IN_SILICO_TERMS))
        return "in_silico"
    return "unknown"


def _design(types: list[str], mesh: list[str], source_type: str, basis: list[str]) -> str:
    by_source = _DESIGN_BY_SOURCE_TYPE.get((source_type or "").strip().lower())
    if by_source:
        basis.append(f"source_type:{source_type}")
        return by_source

    for label, design in _DESIGN_BY_PUBLICATION_TYPE:
        if label in types:
            basis.append(f"pt:{label}")
            return design

    mesh_terms = set(mesh)
    for label, design in _DESIGN_BY_MESH:
        if label in mesh_terms:
            basis.append(f"mesh:{label}")
            return design

    return "unknown"
