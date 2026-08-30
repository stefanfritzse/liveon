"""Canonical representation of scientific evidence.

Everything downstream of acquisition is expressed in these types, and they exist to
make three of the pipeline invariants structural rather than aspirational:

* **A model never defines what the evidence is (I1).** Records are keyed by a canonical
  identifier built from the source itself — a DOI, a PubMed ID, a trial registration —
  and writers cite those keys through opaque handles they cannot invent.
* **Every number traces to a span (I2).** :class:`Extracted` refuses to hold a value
  without a :class:`Span`, and a span is a byte range into the stored document that can
  be re-checked at any time.
* **Unknown is a value (I3).** A field the source does not state is ``not_reported``; a
  field the extractor could not locate is ``not_extractable``. Neither is ``None``
  pretending to be an answer, and both cap the evidence grade rather than being filled
  in by a model with a plausible number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Generic, Literal, Sequence, TypeVar

__all__ = [
    "SCHEMA_VERSION",
    "Claim",
    "Classification",
    "Effect",
    "EvidenceBundle",
    "EvidenceRecord",
    "Extracted",
    "NumberRef",
    "Outcome",
    "ReviewDecision",
    "Span",
    "Violation",
    "clamp_grade",
    "make_source_key",
    "normalise_identifier",
    "parse_source_key",
]

#: Bumped whenever a stored document shape changes in a way readers must notice.
SCHEMA_VERSION = 1

T = TypeVar("T")

SourceScheme = Literal["doi", "pmid", "pmcid", "nct", "url"]
SOURCE_SCHEMES: tuple[str, ...] = ("doi", "pmid", "pmcid", "nct", "url")

SourceType = Literal[
    "journal_article",
    "preprint",
    "systematic_review",
    "meta_analysis",
    "clinical_trial_record",
    "guideline",
    "regulatory",
    "news",
    "other",
]

StudyDesign = Literal[
    "meta_analysis",
    "systematic_review",
    "rct",
    "non_randomised_trial",
    "prospective_cohort",
    "retrospective_cohort",
    "case_control",
    "cross_sectional",
    "case_report",
    "narrative_review",
    "preclinical",
    "unknown",
]

Subject = Literal["human", "animal", "in_vitro", "in_silico", "mixed", "unknown"]

RetractionState = Literal["none", "concern", "corrected", "retracted"]

#: States a record moves through. Usage is deliberately *not* here: a record is used
#: many times over its life, so usage lives in its own table (improvements.md item 5).
RecordState = Literal[
    "discovered",
    "acquired",
    "extracted",
    "reviewed",
    "approved",
    "rejected",
]

Grade = Literal["high", "moderate", "low", "preliminary", "insufficient"]

#: Worst to best, so code can compare and clamp grades without a lookup table.
GRADE_ORDER: tuple[str, ...] = ("insufficient", "preliminary", "low", "moderate", "high")

ExtractionStatus = Literal["extracted", "not_reported", "not_extractable"]


def clamp_grade(proposed: str, computed: str) -> str:
    """Return the lower of a model's grade and the one code computed.

    This is invariant I4 in one line: the reviewer may argue a bundle *down* — it saw an
    overstatement the rubric could not — but a model that returns "high" for evidence the
    rubric graded "preliminary" is simply overruled. An unrecognised grade is treated as
    the floor, so a typo or a hallucinated grade name can never raise anything.
    """

    if computed not in GRADE_ORDER:
        return "insufficient"
    if proposed not in GRADE_ORDER:
        return computed
    return min(proposed, computed, key=GRADE_ORDER.index)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    """Coerce a stored value into a timezone-aware UTC datetime."""

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        trimmed = value.strip()
        return [trimmed] if trimmed else []
    if isinstance(value, Sequence):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# ----------------------------------------------------------------------
# Source identity
# ----------------------------------------------------------------------


def normalise_identifier(scheme: str, value: str) -> str:
    """Return the canonical spelling of an identifier within ``scheme``.

    The same paper arrives as ``10.1001/X``, ``https://doi.org/10.1001/x`` and
    ``doi:10.1001/x`` depending on which API answered. Collapsing them here is what
    makes deduplication a primary-key constraint rather than a heuristic.
    """

    cleaned = (value or "").strip()
    if not cleaned:
        return ""

    kind = (scheme or "").strip().lower()

    if kind == "doi":
        for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix) :]
                break
        # DOIs are case-insensitive by specification; publishers disagree in practice.
        return cleaned.strip().rstrip(".").lower()

    if kind == "pmid":
        digits = "".join(char for char in cleaned if char.isdigit())
        return digits

    if kind == "pmcid":
        digits = "".join(char for char in cleaned if char.isdigit())
        return f"PMC{digits}" if digits else ""

    if kind == "nct":
        upper = cleaned.upper().replace(" ", "")
        digits = "".join(char for char in upper if char.isdigit())
        return f"NCT{digits}" if digits else ""

    if kind == "url":
        # Reuse the aggregator's normaliser so a feed URL and a stored URL agree.
        from app.services.aggregator import _normalise_url

        return _normalise_url(cleaned)

    return cleaned


def make_source_key(scheme: str, value: str) -> str:
    """Build the canonical ``scheme:value`` key used as the store's primary key."""

    kind = (scheme or "").strip().lower()
    if kind not in SOURCE_SCHEMES:
        raise ValueError(f"Unknown source scheme: {scheme!r}")

    identifier = normalise_identifier(kind, value)
    if not identifier:
        raise ValueError(f"Empty {kind} identifier")

    return f"{kind}:{identifier}"


def parse_source_key(key: str) -> tuple[str, str]:
    """Split a source key back into ``(scheme, identifier)``."""

    scheme, separator, value = (key or "").partition(":")
    if not separator or scheme.lower() not in SOURCE_SCHEMES:
        raise ValueError(f"Malformed source key: {key!r}")
    return scheme.lower(), value


# ----------------------------------------------------------------------
# Spans and extracted values
# ----------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Span:
    """A byte range into a stored source document.

    This is the anchor that makes I2 checkable by code rather than by assertion. A span
    whose quote no longer matches its offsets is not a formatting problem — it means the
    value beside it was not read out of the document at all.
    """

    quote: str
    start: int
    end: int

    def verify(self, document_text: str) -> bool:
        """Return ``True`` when the offsets still hold the quoted text."""

        if not self.quote or self.start < 0 or self.end > len(document_text or ""):
            return False
        if self.end - self.start != len(self.quote):
            return False
        return document_text[self.start : self.end] == self.quote

    def to_document(self) -> dict[str, Any]:
        return {"quote": self.quote, "start": self.start, "end": self.end}

    @classmethod
    def from_document(cls, data: Any) -> "Span | None":
        if not isinstance(data, dict):
            return None
        quote = data.get("quote")
        start = data.get("start")
        end = data.get("end")
        if not isinstance(quote, str) or not isinstance(start, int) or not isinstance(end, int):
            return None
        return cls(quote=quote, start=start, end=end)

    @classmethod
    def locate(cls, document_text: str, quote: str, *, hint: int | None = None) -> "Span | None":
        """Find ``quote`` in ``document_text`` and return the span covering it.

        Models are asked for the quote, not for offsets, because they cannot count
        characters reliably. Offsets are computed here, from the real document.
        """

        if not quote or not document_text:
            return None

        start = -1
        if hint is not None and 0 <= hint <= len(document_text):
            start = document_text.find(quote, hint)
        if start == -1:
            start = document_text.find(quote)
        if start == -1:
            return None

        return cls(quote=quote, start=start, end=start + len(quote))


@dataclass(slots=True)
class Extracted(Generic[T]):
    """A value read out of a source document, or an explicit statement that it was not.

    Construction is normalising rather than strict: a value claiming to be ``extracted``
    without both a value and a span is demoted to ``not_extractable``. Demotion is the
    safe direction — it caps the evidence grade — and it means no caller can accidentally
    manufacture an unanchored fact by forgetting an argument.
    """

    value: T | None = None
    status: ExtractionStatus = "not_extractable"
    span: Span | None = None

    def __post_init__(self) -> None:
        if self.status == "extracted" and (self.value is None or self.span is None):
            self.value = None
            self.span = None
            self.status = "not_extractable"
        elif self.status != "extracted":
            self.value = None
            self.span = None

    @classmethod
    def found(cls, value: T, span: Span) -> "Extracted[T]":
        return cls(value=value, status="extracted", span=span)

    @classmethod
    def not_reported(cls) -> "Extracted[T]":
        """The document was read and genuinely does not state this."""

        return cls(value=None, status="not_reported", span=None)

    @classmethod
    def not_extractable(cls) -> "Extracted[T]":
        """The value could not be located, or its span failed verification."""

        return cls(value=None, status="not_extractable", span=None)

    @property
    def is_known(self) -> bool:
        return self.status == "extracted"

    def verify(self, document_text: str) -> "Extracted[T]":
        """Return self, or a demoted copy when the span no longer holds."""

        if self.status != "extracted":
            return self
        if self.span is not None and self.span.verify(document_text):
            return self
        return Extracted.not_extractable()

    def to_document(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status}
        if self.status == "extracted":
            payload["value"] = self.value
            payload["span"] = self.span.to_document() if self.span else None
        return payload

    @classmethod
    def from_document(
        cls,
        data: Any,
        *,
        coerce: Callable[[Any], T | None] | None = None,
    ) -> "Extracted[T]":
        if not isinstance(data, dict):
            return cls.not_extractable()

        status = data.get("status")
        if status not in ("extracted", "not_reported", "not_extractable"):
            return cls.not_extractable()
        if status != "extracted":
            return cls(value=None, status=status, span=None)

        raw_value = data.get("value")
        value = coerce(raw_value) if coerce is not None else raw_value
        span = Span.from_document(data.get("span"))
        if value is None or span is None:
            return cls.not_extractable()
        return cls(value=value, status="extracted", span=span)


# ----------------------------------------------------------------------
# Findings
# ----------------------------------------------------------------------


@dataclass(slots=True)
class Effect:
    """A quantified result. Every populated field must come from the document."""

    magnitude: Extracted[float] = field(default_factory=Extracted.not_extractable)
    unit: Extracted[str] = field(default_factory=Extracted.not_extractable)
    ci_low: Extracted[float] = field(default_factory=Extracted.not_extractable)
    ci_high: Extracted[float] = field(default_factory=Extracted.not_extractable)
    p_value: Extracted[str] = field(default_factory=Extracted.not_extractable)

    @property
    def has_uncertainty(self) -> bool:
        """Whether the effect arrives with an interval rather than a bare number."""

        return self.ci_low.is_known and self.ci_high.is_known

    def to_document(self) -> dict[str, Any]:
        return {
            "magnitude": self.magnitude.to_document(),
            "unit": self.unit.to_document(),
            "ci_low": self.ci_low.to_document(),
            "ci_high": self.ci_high.to_document(),
            "p_value": self.p_value.to_document(),
        }

    @classmethod
    def from_document(cls, data: Any) -> "Effect":
        payload = data if isinstance(data, dict) else {}
        return cls(
            magnitude=Extracted.from_document(payload.get("magnitude"), coerce=_as_float),
            unit=Extracted.from_document(payload.get("unit"), coerce=_as_str),
            ci_low=Extracted.from_document(payload.get("ci_low"), coerce=_as_float),
            ci_high=Extracted.from_document(payload.get("ci_high"), coerce=_as_float),
            p_value=Extracted.from_document(payload.get("p_value"), coerce=_as_str),
        )

    def spans(self) -> list[Span]:
        return [
            item.span
            for item in (self.magnitude, self.unit, self.ci_low, self.ci_high, self.p_value)
            if item.span is not None
        ]


@dataclass(slots=True)
class Outcome:
    """One reported endpoint and what happened to it."""

    name: str
    is_surrogate: Extracted[bool] = field(default_factory=Extracted.not_extractable)
    direction: Extracted[str] = field(default_factory=Extracted.not_extractable)
    effect: Effect = field(default_factory=Effect)

    def to_document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "is_surrogate": self.is_surrogate.to_document(),
            "direction": self.direction.to_document(),
            "effect": self.effect.to_document(),
        }

    @classmethod
    def from_document(cls, data: Any) -> "Outcome":
        payload = data if isinstance(data, dict) else {}
        return cls(
            name=_text(payload.get("name")),
            is_surrogate=Extracted.from_document(payload.get("is_surrogate"), coerce=_as_bool),
            direction=Extracted.from_document(payload.get("direction"), coerce=_as_str),
            effect=Effect.from_document(payload.get("effect")),
        )


@dataclass(slots=True, frozen=True)
class Classification:
    """Study design and subject, decided from metadata by code — never by a model.

    PubMed publication types and MeSH descriptors already answer "was this an RCT?" and
    "was this in people?". Asking a language model to re-answer them from the abstract
    adds a failure mode and removes an audit trail, so :mod:`app.services.evidence.classification`
    computes this and records which terms decided it.
    """

    design: str = "unknown"
    subject: str = "unknown"
    basis: tuple[str, ...] = ()

    @property
    def is_known(self) -> bool:
        return self.design != "unknown" and self.subject != "unknown"

    @property
    def is_human(self) -> bool:
        return self.subject in ("human", "mixed")

    def to_document(self) -> dict[str, Any]:
        return {"design": self.design, "subject": self.subject, "basis": list(self.basis)}

    @classmethod
    def from_document(cls, data: Any) -> "Classification":
        payload = data if isinstance(data, dict) else {}
        return cls(
            design=_text(payload.get("design")) or "unknown",
            subject=_text(payload.get("subject")) or "unknown",
            basis=tuple(_strings(payload.get("basis"))),
        )


# ----------------------------------------------------------------------
# The record
# ----------------------------------------------------------------------


@dataclass(slots=True)
class EvidenceRecord:
    """One scientific source, as acquired and (optionally) extracted.

    ``document_text`` is the verbatim text every span indexes into. It is written once at
    acquisition and never rewritten: normalising it later would silently invalidate every
    span that points into it, which is exactly the kind of quiet corruption the span
    mechanism exists to prevent.
    """

    source_key: str
    source_type: str = "journal_article"
    title: str = ""
    aliases: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    journal: str = ""
    source_published_at: datetime | None = None
    retrieved_at: datetime | None = None
    document_text: str = ""
    document_kind: str = "abstract"  # abstract | full_text | record
    open_access: bool = False

    # Metadata the code classifies on (never the model).
    publication_types: list[str] = field(default_factory=list)
    mesh_terms: list[str] = field(default_factory=list)
    classification: Classification = field(default_factory=Classification)

    retraction_state: str = "none"
    retraction_notes: list[str] = field(default_factory=list)
    superseded_by: str | None = None

    # Extracted from ``document_text``; each carries its own span.
    population: Extracted[str] = field(default_factory=Extracted.not_extractable)
    sample_size: Extracted[int] = field(default_factory=Extracted.not_extractable)
    intervention: Extracted[str] = field(default_factory=Extracted.not_extractable)
    comparator: Extracted[str] = field(default_factory=Extracted.not_extractable)
    duration: Extracted[str] = field(default_factory=Extracted.not_extractable)
    limitations: Extracted[str] = field(default_factory=Extracted.not_extractable)
    funding: Extracted[str] = field(default_factory=Extracted.not_extractable)
    conflicts: Extracted[str] = field(default_factory=Extracted.not_extractable)
    outcomes: list[Outcome] = field(default_factory=list)

    extraction_model: str | None = None
    extraction_prompt_version: str | None = None
    extracted_at: datetime | None = None

    state: str = "discovered"
    schema_version: int = SCHEMA_VERSION

    # -- derived -------------------------------------------------------

    @property
    def is_retracted(self) -> bool:
        """Whether this record is blocked from publication by G6."""

        return self.retraction_state in ("retracted", "concern")

    @property
    def extracted_fields(self) -> dict[str, Extracted[Any]]:
        """The span-carrying fields, by name, for verification and gate reporting."""

        return {
            "population": self.population,
            "sample_size": self.sample_size,
            "intervention": self.intervention,
            "comparator": self.comparator,
            "duration": self.duration,
            "limitations": self.limitations,
            "funding": self.funding,
            "conflicts": self.conflicts,
        }

    def spans(self) -> list[Span]:
        """Every span anywhere in the record, including inside outcomes."""

        collected = [item.span for item in self.extracted_fields.values() if item.span]
        for outcome in self.outcomes:
            for item in (outcome.is_surrogate, outcome.direction):
                if item.span is not None:
                    collected.append(item.span)
            collected.extend(outcome.effect.spans())
        return collected

    def unverified_spans(self) -> list[Span]:
        """Spans that no longer match ``document_text``. Non-empty means corruption."""

        return [span for span in self.spans() if not span.verify(self.document_text)]

    def verified(self) -> "EvidenceRecord":
        """Return a copy with every field whose span fails demoted to unknown.

        Called after extraction, and again on load, so a value can never outlive the
        evidence for it.
        """

        text = self.document_text
        replaced = {name: value.verify(text) for name, value in self.extracted_fields.items()}
        outcomes = [
            Outcome(
                name=outcome.name,
                is_surrogate=outcome.is_surrogate.verify(text),
                direction=outcome.direction.verify(text),
                effect=Effect(
                    magnitude=outcome.effect.magnitude.verify(text),
                    unit=outcome.effect.unit.verify(text),
                    ci_low=outcome.effect.ci_low.verify(text),
                    ci_high=outcome.effect.ci_high.verify(text),
                    p_value=outcome.effect.p_value.verify(text),
                ),
            )
            for outcome in self.outcomes
        ]

        copy = self.to_document()
        record = EvidenceRecord.from_document(copy)
        for name, value in replaced.items():
            setattr(record, name, value)
        record.outcomes = outcomes
        return record

    # -- persistence ---------------------------------------------------

    def to_document(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "source_type": self.source_type,
            "title": self.title,
            "aliases": list(self.aliases),
            "authors": list(self.authors),
            "journal": self.journal,
            "source_published_at": self.source_published_at,
            "retrieved_at": self.retrieved_at,
            "document_text": self.document_text,
            "document_kind": self.document_kind,
            "open_access": self.open_access,
            "publication_types": list(self.publication_types),
            "mesh_terms": list(self.mesh_terms),
            "classification": self.classification.to_document(),
            "retraction_state": self.retraction_state,
            "retraction_notes": list(self.retraction_notes),
            "superseded_by": self.superseded_by,
            "population": self.population.to_document(),
            "sample_size": self.sample_size.to_document(),
            "intervention": self.intervention.to_document(),
            "comparator": self.comparator.to_document(),
            "duration": self.duration.to_document(),
            "limitations": self.limitations.to_document(),
            "funding": self.funding.to_document(),
            "conflicts": self.conflicts.to_document(),
            "outcomes": [outcome.to_document() for outcome in self.outcomes],
            "extraction_model": self.extraction_model,
            "extraction_prompt_version": self.extraction_prompt_version,
            "extracted_at": self.extracted_at,
            "state": self.state,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_document(cls, data: Any) -> "EvidenceRecord":
        payload = data if isinstance(data, dict) else {}
        return cls(
            source_key=_text(payload.get("source_key")),
            source_type=_text(payload.get("source_type")) or "journal_article",
            title=_text(payload.get("title")),
            aliases=_strings(payload.get("aliases")),
            authors=_strings(payload.get("authors")),
            journal=_text(payload.get("journal")),
            source_published_at=_parse_datetime(payload.get("source_published_at")),
            retrieved_at=_parse_datetime(payload.get("retrieved_at")),
            document_text=payload.get("document_text") if isinstance(payload.get("document_text"), str) else "",
            document_kind=_text(payload.get("document_kind")) or "abstract",
            open_access=bool(payload.get("open_access")),
            publication_types=_strings(payload.get("publication_types")),
            mesh_terms=_strings(payload.get("mesh_terms")),
            classification=Classification.from_document(payload.get("classification")),
            retraction_state=_text(payload.get("retraction_state")) or "none",
            retraction_notes=_strings(payload.get("retraction_notes")),
            superseded_by=_text(payload.get("superseded_by")) or None,
            population=Extracted.from_document(payload.get("population"), coerce=_as_str),
            sample_size=Extracted.from_document(payload.get("sample_size"), coerce=_as_int),
            intervention=Extracted.from_document(payload.get("intervention"), coerce=_as_str),
            comparator=Extracted.from_document(payload.get("comparator"), coerce=_as_str),
            duration=Extracted.from_document(payload.get("duration"), coerce=_as_str),
            limitations=Extracted.from_document(payload.get("limitations"), coerce=_as_str),
            funding=Extracted.from_document(payload.get("funding"), coerce=_as_str),
            conflicts=Extracted.from_document(payload.get("conflicts"), coerce=_as_str),
            outcomes=[Outcome.from_document(item) for item in payload.get("outcomes") or []],
            extraction_model=_text(payload.get("extraction_model")) or None,
            extraction_prompt_version=_text(payload.get("extraction_prompt_version")) or None,
            extracted_at=_parse_datetime(payload.get("extracted_at")),
            state=_text(payload.get("state")) or "discovered",
            schema_version=payload.get("schema_version") if isinstance(payload.get("schema_version"), int) else SCHEMA_VERSION,
        )


# ----------------------------------------------------------------------
# Claims and bundles
# ----------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class NumberRef:
    """A number as it will appear in prose, tied to where it came from."""

    text: str
    source_key: str
    span: Span

    def to_document(self) -> dict[str, Any]:
        return {"text": self.text, "source_key": self.source_key, "span": self.span.to_document()}

    @classmethod
    def from_document(cls, data: Any) -> "NumberRef | None":
        if not isinstance(data, dict):
            return None
        span = Span.from_document(data.get("span"))
        text = _text(data.get("text"))
        source_key = _text(data.get("source_key"))
        if not span or not text or not source_key:
            return None
        return cls(text=text, source_key=source_key, span=span)


@dataclass(slots=True)
class Claim:
    """One assertion, its supporting sources, and the shape of its support."""

    text: str
    claim_type: str = "descriptive"  # descriptive | associative | causal | recommendation
    evidence_keys: list[str] = field(default_factory=list)
    numbers: list[NumberRef] = field(default_factory=list)
    population_scope: str = ""
    applicability: str = ""
    limitations: list[str] = field(default_factory=list)
    contradicted_by: list[str] = field(default_factory=list)

    def to_document(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "claim_type": self.claim_type,
            "evidence_keys": list(self.evidence_keys),
            "numbers": [number.to_document() for number in self.numbers],
            "population_scope": self.population_scope,
            "applicability": self.applicability,
            "limitations": list(self.limitations),
            "contradicted_by": list(self.contradicted_by),
        }

    @classmethod
    def from_document(cls, data: Any) -> "Claim":
        payload = data if isinstance(data, dict) else {}
        numbers = [NumberRef.from_document(item) for item in payload.get("numbers") or []]
        return cls(
            text=_text(payload.get("text")),
            claim_type=_text(payload.get("claim_type")) or "descriptive",
            evidence_keys=_strings(payload.get("evidence_keys")),
            numbers=[number for number in numbers if number is not None],
            population_scope=_text(payload.get("population_scope")),
            applicability=_text(payload.get("applicability")),
            limitations=_strings(payload.get("limitations")),
            contradicted_by=_strings(payload.get("contradicted_by")),
        )


@dataclass(slots=True, frozen=True)
class Violation:
    """A gate refusal. ``gate`` is the identifier from improvements.md item 3."""

    gate: str
    detail: str
    claim_text: str = ""
    source_key: str = ""

    def to_document(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "detail": self.detail,
            "claim_text": self.claim_text,
            "source_key": self.source_key,
        }

    @classmethod
    def from_document(cls, data: Any) -> "Violation | None":
        if not isinstance(data, dict):
            return None
        gate = _text(data.get("gate"))
        if not gate:
            return None
        return cls(
            gate=gate,
            detail=_text(data.get("detail")),
            claim_text=_text(data.get("claim_text")),
            source_key=_text(data.get("source_key")),
        )


@dataclass(slots=True)
class ReviewDecision:
    """The outcome of evidence review, and enough of its reasoning to audit later.

    ``grade`` is always the computed grade after clamping, never whatever a model
    proposed: see :func:`clamp_grade`.
    """

    status: str = "pending"  # pending | approved | downgraded | regenerate | rejected
    grade: str = "insufficient"
    rationale: list[str] = field(default_factory=list)
    violations: list["Violation"] = field(default_factory=list)
    notes: str = ""
    reviewed_at: datetime = field(default_factory=_utc_now)
    model_id: str | None = None
    prompt_version: str | None = None

    @property
    def is_approved(self) -> bool:
        return self.status in ("approved", "downgraded") and self.grade != "insufficient"

    @property
    def may_retry(self) -> bool:
        """Whether regenerating the prose could plausibly fix this."""

        return self.status == "regenerate"

    def to_document(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "grade": self.grade,
            "rationale": list(self.rationale),
            "violations": [violation.to_document() for violation in self.violations],
            "notes": self.notes,
            "reviewed_at": self.reviewed_at,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
        }

    @classmethod
    def from_document(cls, data: Any) -> "ReviewDecision | None":
        if not isinstance(data, dict):
            return None
        violations = [Violation.from_document(item) for item in data.get("violations") or []]
        return cls(
            status=_text(data.get("status")) or "pending",
            grade=_text(data.get("grade")) or "insufficient",
            rationale=_strings(data.get("rationale")),
            violations=[violation for violation in violations if violation is not None],
            notes=_text(data.get("notes")),
            reviewed_at=_parse_datetime(data.get("reviewed_at")) or _utc_now(),
            model_id=_text(data.get("model_id")) or None,
            prompt_version=_text(data.get("prompt_version")) or None,
        )


@dataclass(slots=True)
class EvidenceBundle:
    """A reviewed set of claims about one topic, and the sources behind them."""

    bundle_id: str
    topic_key: str = ""
    claims: list[Claim] = field(default_factory=list)
    grade: str = "insufficient"
    grade_rationale: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    review_status: str = "pending"  # pending | approved | downgraded | regenerate | rejected
    review: ReviewDecision | None = None
    #: Set when a newer bundle covers the same topic. The older one is not deleted — it is
    #: what was believed at the time, and the run log points at it.
    superseded_by: str | None = None
    created_at: datetime = field(default_factory=_utc_now)
    run_id: str | None = None
    schema_version: int = SCHEMA_VERSION

    @property
    def is_publishable(self) -> bool:
        """Approved, and graded above the floor. ``insufficient`` never publishes."""

        return self.review_status == "approved" and self.grade != "insufficient"

    def source_keys(self) -> list[str]:
        """Every source key cited by any claim, in first-seen order."""

        seen: set[str] = set()
        ordered: list[str] = []
        for claim in self.claims:
            for key in claim.evidence_keys:
                if key not in seen:
                    seen.add(key)
                    ordered.append(key)
        return ordered

    def to_document(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "topic_key": self.topic_key,
            "claims": [claim.to_document() for claim in self.claims],
            "grade": self.grade,
            "grade_rationale": list(self.grade_rationale),
            "violations": [violation.to_document() for violation in self.violations],
            "review_status": self.review_status,
            "review": self.review.to_document() if self.review else None,
            "superseded_by": self.superseded_by,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_document(cls, data: Any) -> "EvidenceBundle":
        payload = data if isinstance(data, dict) else {}
        violations = [Violation.from_document(item) for item in payload.get("violations") or []]
        return cls(
            bundle_id=_text(payload.get("bundle_id")),
            topic_key=_text(payload.get("topic_key")),
            claims=[Claim.from_document(item) for item in payload.get("claims") or []],
            grade=_text(payload.get("grade")) or "insufficient",
            grade_rationale=_strings(payload.get("grade_rationale")),
            violations=[violation for violation in violations if violation is not None],
            review_status=_text(payload.get("review_status")) or "pending",
            review=ReviewDecision.from_document(payload.get("review")),
            superseded_by=_text(payload.get("superseded_by")) or None,
            created_at=_parse_datetime(payload.get("created_at")) or _utc_now(),
            run_id=_text(payload.get("run_id")) or None,
            schema_version=payload.get("schema_version") if isinstance(payload.get("schema_version"), int) else SCHEMA_VERSION,
        )


# ----------------------------------------------------------------------
# Coercion helpers used by ``Extracted.from_document``
# ----------------------------------------------------------------------


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        digits = value.strip().replace(",", "")
        try:
            return int(digits)
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes"):
            return True
        if lowered in ("false", "no"):
            return False
    return None
