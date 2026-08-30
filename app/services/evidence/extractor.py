"""Read structured findings out of one source document.

The model is asked for a *quote*, never for an offset: it cannot count characters, but it
can copy a phrase. Offsets are then computed here against the real document, which turns
"the model says the sample was 412 people" into "the string the model quoted exists at
bytes 431–486 of the abstract, and it contains 412". A quote that cannot be found is not
retried and not repaired — the field is demoted to ``not_extractable``, because a quote
that is not in the document is the signature of an invented value.

This stage is deliberately per-source and cacheable. Synthesis across sources happens
later and never sees raw document text, so it cannot introduce an unanchored number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any, Callable, Protocol, Sequence

from app.models.evidence import (
    Effect,
    EvidenceRecord,
    Extracted,
    Outcome,
    Span,
)
from app.utils.json_repair import invoke_json_object
from app.utils.langchain_compat import BaseMessage, ChatPromptTemplate

LOGGER = logging.getLogger(__name__)

__all__ = ["EXTRACTION_PROMPT_VERSION", "ExtractorAgent"]

#: Bump when the prompt changes meaningfully; it is part of the extraction cache key, so
#: a bump re-extracts everything rather than mixing outputs from two different prompts.
EXTRACTION_PROMPT_VERSION = "1"


class SupportsInvoke(Protocol):
    def invoke(self, input: Any, **kwargs: Any) -> BaseMessage | str:  # pragma: no cover
        ...


DEFAULT_SYSTEM_PROMPT = (
    "You are a research assistant that extracts facts from scientific abstracts. "
    "You never infer, estimate, or complete missing information. "
    "Every value you report must be copied from the document you are given."
)

DEFAULT_HUMAN_PROMPT = """
Extract what the document below actually states. Return valid JSON with this shape:

{{
  "population": {{"value": "who was studied", "quote": "verbatim phrase from the document"}},
  "sample_size": {{"value": 0, "quote": "verbatim phrase containing the number"}},
  "intervention": {{"value": "what was given or done", "quote": "verbatim phrase"}},
  "comparator": {{"value": "what it was compared against", "quote": "verbatim phrase"}},
  "duration": {{"value": "how long", "quote": "verbatim phrase"}},
  "limitations": {{"value": "stated limitations", "quote": "verbatim phrase"}},
  "funding": {{"value": "who funded it", "quote": "verbatim phrase"}},
  "conflicts": {{"value": "declared conflicts", "quote": "verbatim phrase"}},
  "outcomes": [
    {{
      "name": "the endpoint measured",
      "direction": {{"value": "increase|decrease|no_change", "quote": "verbatim phrase"}},
      "is_surrogate": {{"value": true, "quote": "verbatim phrase"}},
      "magnitude": {{"value": 0.0, "quote": "verbatim phrase containing the number"}},
      "unit": {{"value": "%", "quote": "verbatim phrase"}},
      "ci_low": {{"value": 0.0, "quote": "verbatim phrase"}},
      "ci_high": {{"value": 0.0, "quote": "verbatim phrase"}},
      "p_value": {{"value": "p<0.05", "quote": "verbatim phrase"}}
    }}
  ]
}}

Rules, without exception:
- Every "quote" must be copied character-for-character from the document. Do not
  paraphrase, tidy, translate, or shorten it.
- If the document does not state a field, write "not_reported" in place of that object.
- Never guess a number. A study that does not report its sample size has no sample size.
- "is_surrogate" is true when the endpoint is a biomarker or intermediate measure rather
  than a clinical outcome someone would notice.

Document:
{document}
""".strip()


def _default_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            ("system", DEFAULT_SYSTEM_PROMPT),
            ("human", DEFAULT_HUMAN_PROMPT),
        ]
    )


@dataclass(slots=True)
class ExtractorAgent:
    """Turn a record's ``document_text`` into span-anchored fields."""

    llm: SupportsInvoke
    prompt: ChatPromptTemplate = field(default_factory=_default_prompt)
    model_id: str = "unknown"
    prompt_version: str = EXTRACTION_PROMPT_VERSION
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def extract(self, record: EvidenceRecord, *, force: bool = False) -> EvidenceRecord:
        """Return ``record`` with its extracted fields populated.

        Re-extraction is skipped when the record already carries output from this prompt
        and model, which is what keeps a re-run — or the offline benchmark — from paying
        for the same abstract twice.
        """

        if not record.document_text.strip():
            LOGGER.info(
                "Skipping extraction: record has no document text",
                extra={"event": "evidence.extract_skipped", "source_key": record.source_key},
            )
            return record

        if not force and self._already_extracted(record):
            return record

        payload = invoke_json_object(
            self.llm,
            self.prompt.format_messages(document=record.document_text),
            label="Extractor",
            logger=LOGGER,
        )

        document = record.document_text
        record.population = _string_field(payload.get("population"), document)
        record.sample_size = _int_field(payload.get("sample_size"), document)
        record.intervention = _string_field(payload.get("intervention"), document)
        record.comparator = _string_field(payload.get("comparator"), document)
        record.duration = _string_field(payload.get("duration"), document)
        record.limitations = _string_field(payload.get("limitations"), document)
        record.funding = _string_field(payload.get("funding"), document)
        record.conflicts = _string_field(payload.get("conflicts"), document)
        record.outcomes = _outcomes(payload.get("outcomes"), document)

        record.extraction_model = self.model_id
        record.extraction_prompt_version = self.prompt_version
        record.extracted_at = self.now()
        record.state = "extracted"

        dropped = _dropped_fields(record)
        if dropped:
            LOGGER.info(
                "Extractor produced %s unanchored field(s); demoted to not_extractable",
                len(dropped),
                extra={
                    "event": "evidence.extract_unanchored",
                    "source_key": record.source_key,
                    "fields": ",".join(dropped),
                },
            )

        # Belt and braces: the record is re-verified on load anyway, but a value that
        # fails here should never be handed onward inside this run either.
        return record.verified()

    def _already_extracted(self, record: EvidenceRecord) -> bool:
        return (
            record.state in ("extracted", "reviewed", "approved")
            and record.extraction_prompt_version == self.prompt_version
            and record.extraction_model == self.model_id
        )


# ----------------------------------------------------------------------
# Field parsing
# ----------------------------------------------------------------------


def _dropped_fields(record: EvidenceRecord) -> list[str]:
    return [
        name
        for name, value in record.extracted_fields.items()
        if value.status == "not_extractable"
    ]


def _anchor(payload: Any, document: str) -> tuple[Any, Span] | None:
    """Return the raw value and its span, or ``None`` when it cannot be anchored."""

    if not isinstance(payload, dict):
        return None
    quote = payload.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        return None

    span = Span.locate(document, quote.strip())
    if span is None:
        # Models often reflow whitespace when copying. Collapsing both sides is a
        # forgiving retry; anything beyond that would be repairing an invented quote.
        span = _locate_normalised(document, quote)
    if span is None:
        return None

    return payload.get("value"), span


def _locate_normalised(document: str, quote: str) -> Span | None:
    needle = " ".join(quote.split())
    if not needle:
        return None

    collapsed = " ".join(document.split())
    if needle not in collapsed:
        return None

    # Walk the document once, comparing whitespace-insensitively, so the span still
    # refers to real offsets in the untouched text.
    words = needle.split(" ")
    start = document.find(words[0])
    while start != -1:
        cursor = start
        matched = True
        for index, word in enumerate(words):
            if index:
                while cursor < len(document) and document[cursor].isspace():
                    cursor += 1
            if not document.startswith(word, cursor):
                matched = False
                break
            cursor += len(word)
        if matched:
            return Span(quote=document[start:cursor], start=start, end=cursor)
        start = document.find(words[0], start + 1)
    return None


def _is_absent(payload: Any) -> bool:
    """Whether the model explicitly said the document does not report this."""

    if payload is None:
        return True
    if isinstance(payload, str):
        return payload.strip().lower() in ("not_reported", "not reported", "none", "n/a", "")
    if isinstance(payload, dict):
        status = payload.get("status")
        if isinstance(status, str) and status.strip().lower() in ("not_reported", "not reported"):
            return True
        return payload.get("value") is None and not payload.get("quote")
    return False


def _string_field(payload: Any, document: str) -> Extracted[str]:
    if _is_absent(payload):
        return Extracted.not_reported()
    anchored = _anchor(payload, document)
    if anchored is None:
        return Extracted.not_extractable()
    value, span = anchored
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return Extracted.not_extractable()
    return Extracted.found(text, span)


def _int_field(payload: Any, document: str) -> Extracted[int]:
    if _is_absent(payload):
        return Extracted.not_reported()
    anchored = _anchor(payload, document)
    if anchored is None:
        return Extracted.not_extractable()
    value, span = anchored
    number = _coerce_int(value)
    if number is None:
        return Extracted.not_extractable()
    # The number has to be in the quoted text, not merely beside it in the reply.
    if not _digits_present(number, span.quote):
        return Extracted.not_extractable()
    return Extracted.found(number, span)


def _float_field(payload: Any, document: str) -> Extracted[float]:
    if _is_absent(payload):
        return Extracted.not_reported()
    anchored = _anchor(payload, document)
    if anchored is None:
        return Extracted.not_extractable()
    value, span = anchored
    number = _coerce_float(value)
    if number is None:
        return Extracted.not_extractable()
    if not _digits_present(number, span.quote):
        return Extracted.not_extractable()
    return Extracted.found(number, span)


def _bool_field(payload: Any, document: str) -> Extracted[bool]:
    if _is_absent(payload):
        return Extracted.not_reported()
    anchored = _anchor(payload, document)
    if anchored is None:
        return Extracted.not_extractable()
    value, span = anchored
    if isinstance(value, bool):
        return Extracted.found(value, span)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes"):
            return Extracted.found(True, span)
        if lowered in ("false", "no"):
            return Extracted.found(False, span)
    return Extracted.not_extractable()


def _outcomes(payload: Any, document: str) -> list[Outcome]:
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        return []

    outcomes: list[Outcome] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        outcomes.append(
            Outcome(
                name=name.strip(),
                is_surrogate=_bool_field(item.get("is_surrogate"), document),
                direction=_string_field(item.get("direction"), document),
                effect=Effect(
                    magnitude=_float_field(item.get("magnitude"), document),
                    unit=_string_field(item.get("unit"), document),
                    ci_low=_float_field(item.get("ci_low"), document),
                    ci_high=_float_field(item.get("ci_high"), document),
                    p_value=_string_field(item.get("p_value"), document),
                ),
            )
        )
    return outcomes


def _digits_present(number: float | int, quote: str) -> bool:
    """Whether the reported number literally appears in the quoted source text."""

    digits = "".join(char for char in str(number) if char.isdigit()).lstrip("0")
    quote_digits = "".join(char for char in quote if char.isdigit())
    if not digits:
        return "0" in quote_digits
    return digits in quote_digits


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace(" ", "")
        try:
            return int(cleaned)
        except ValueError:
            return None
    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("%", "")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None
