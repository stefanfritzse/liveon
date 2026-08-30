"""Synthesise a reviewed-ready bundle from several extracted records.

This is the stage the old pipeline never had. Articles used to be written from the first
unused headline; here a cluster of sources is reasoned about together, so the output can
say what was found, how strong it is, whether other work agrees, and what is still open.

Two rules keep the model inside its lane:

* **It never sees a document.** The prompt is built from already-extracted fields, each of
  which is anchored to a span. A model that cannot read the abstract cannot quote a number
  that is not in the extraction.
* **It never supplies a number reference.** It writes prose; code then matches every
  figure in that prose against the anchored values of the cited records and attaches the
  spans. A figure with no match gets no reference, and G2 refuses it downstream.

The second rule is what makes fabrication self-defeating rather than merely discouraged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import re
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from uuid import uuid4

from app.models.evidence import (
    Claim,
    EvidenceBundle,
    EvidenceRecord,
    NumberRef,
    Span,
)
from app.services.evidence.citations import EvidenceHandles
from app.services.evidence.gates import normalise_number, numeric_tokens
from app.utils.json_repair import invoke_json_object
from app.utils.langchain_compat import BaseMessage, ChatPromptTemplate

LOGGER = logging.getLogger(__name__)

__all__ = ["SYNTHESIS_PROMPT_VERSION", "SynthesizerAgent", "topic_key_for"]

SYNTHESIS_PROMPT_VERSION = "1"

_CLAIM_TYPES = frozenset({"descriptive", "associative", "causal", "recommendation"})
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class SupportsInvoke(Protocol):
    def invoke(self, input: Any, **kwargs: Any) -> BaseMessage | str:  # pragma: no cover
        ...


DEFAULT_SYSTEM_PROMPT = (
    "You are a research synthesist for a longevity publication. You are given structured "
    "extracts from several studies and you write claims that those extracts support — no "
    "more. You never introduce a study, a number, or a mechanism that is not in the extracts."
)

DEFAULT_HUMAN_PROMPT = """
Write the claims these studies support, taken together.

Return valid JSON:
{{
  "topic": "short phrase naming the intervention and outcome",
  "claims": [
    {{
      "text": "one sentence a reader would understand",
      "claim_type": "descriptive" | "associative" | "causal" | "recommendation",
      "evidence": ["E1"],
      "population_scope": "who this applies to, from the extracts",
      "applicability": "how far this generalises",
      "limitations": ["what the studies could not show"],
      "contradicts": ["E2"]
    }}
  ]
}}

Rules:
- Cite evidence only by the handles listed below. A handle you were not given does not exist.
- Use "causal" only for randomised evidence. For observational studies write associations,
  and say "was associated with" rather than "reduced".
- Where studies disagree, write both and list the disagreeing handle in "contradicts".
  Do not average them into a middle position.
- Say only what the extracts state. If an extract says a value was not reported, it was
  not reported, and no number may be given for it.

Evidence:
{evidence}

Handles:
{handles}
""".strip()


def _default_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [("system", DEFAULT_SYSTEM_PROMPT), ("human", DEFAULT_HUMAN_PROMPT)]
    )


@dataclass(slots=True)
class SynthesizerAgent:
    """Turn a cluster of extracted records into an :class:`EvidenceBundle`."""

    llm: SupportsInvoke
    prompt: ChatPromptTemplate = field(default_factory=_default_prompt)
    model_id: str = "unknown"
    prompt_version: str = SYNTHESIS_PROMPT_VERSION
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    bundle_id_factory: Callable[[], str] = lambda: uuid4().hex

    def synthesize(
        self,
        records: Sequence[EvidenceRecord],
        *,
        run_id: str | None = None,
        topic_key: str | None = None,
    ) -> EvidenceBundle:
        """Return a bundle of claims supported by ``records``."""

        if not records:
            raise ValueError("Synthesis requires at least one evidence record")

        by_key = {record.source_key: record for record in records if record.source_key}
        handles = EvidenceHandles.for_keys(by_key)

        payload = invoke_json_object(
            self.llm,
            self.prompt.format_messages(
                evidence=_render_evidence(handles, by_key),
                handles=handles.prompt_block({key: record.title for key, record in by_key.items()}),
            ),
            label="Synthesizer",
            logger=LOGGER,
        )

        claims = [
            claim
            for claim in (
                self._build_claim(item, handles, by_key) for item in payload.get("claims") or []
            )
            if claim is not None
        ]

        bundle = EvidenceBundle(
            bundle_id=self.bundle_id_factory(),
            topic_key=topic_key or topic_key_for(records, hint=payload.get("topic")),
            claims=claims,
            created_at=self.now(),
            run_id=run_id,
        )

        LOGGER.info(
            "Synthesised %s claim(s) from %s source(s)",
            len(claims),
            len(by_key),
            extra={
                "event": "evidence.synthesised",
                "bundle_id": bundle.bundle_id,
                "topic_key": bundle.topic_key,
            },
        )
        return bundle

    def _build_claim(
        self,
        item: Any,
        handles: EvidenceHandles,
        records: Mapping[str, EvidenceRecord],
    ) -> Claim | None:
        if not isinstance(item, dict):
            return None

        text = str(item.get("text") or "").strip()
        if not text:
            return None

        keys = _resolve_handles(item.get("evidence"), handles, context=text)
        contradicted = _resolve_handles(item.get("contradicts"), handles, context=text)

        claim_type = str(item.get("claim_type") or "").strip().lower()
        if claim_type not in _CLAIM_TYPES:
            claim_type = "descriptive"

        cited = [records[key] for key in keys if key in records]
        return Claim(
            text=text,
            claim_type=claim_type,
            evidence_keys=keys,
            numbers=number_references(text, cited),
            population_scope=str(item.get("population_scope") or "").strip(),
            applicability=str(item.get("applicability") or "").strip(),
            limitations=[
                str(entry).strip()
                for entry in item.get("limitations") or []
                if str(entry).strip()
            ],
            contradicted_by=contradicted,
        )


def _resolve_handles(
    raw: Any, handles: EvidenceHandles, *, context: str
) -> list[str]:
    """Turn model-supplied handles into source keys, dropping anything never issued."""

    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Iterable):
        return []

    keys: list[str] = []
    for entry in raw:
        key = handles.resolve(str(entry))
        if key is None:
            LOGGER.info(
                "Dropped an evidence handle that was never issued",
                extra={"event": "evidence.unknown_handle", "handle": str(entry)[:32]},
            )
            continue
        if key not in keys:
            keys.append(key)
    return keys


def number_references(text: str, records: Sequence[EvidenceRecord]) -> list[NumberRef]:
    """Attach spans to the figures in ``text``, using only anchored values.

    The model never supplies these. A number it wrote that matches no extracted value gets
    no reference at all, which is precisely what G2 refuses — so an invented figure fails
    at the gate rather than being quietly published with a plausible-looking citation.
    """

    references: list[NumberRef] = []
    seen: set[str] = set()

    for token in numeric_tokens(text):
        normalised = normalise_number(token)
        if not normalised or normalised in seen:
            continue

        for record in records:
            span = _anchored_span_for(normalised, record)
            if span is not None:
                references.append(
                    NumberRef(text=token, source_key=record.source_key, span=span)
                )
                seen.add(normalised)
                break

    return references


def _anchored_span_for(normalised: str, record: EvidenceRecord) -> Span | None:
    """Find an extracted value in ``record`` whose span quote carries this number."""

    for value, span in _anchored_numbers(record):
        if span is None:
            continue
        if normalise_number(value) != normalised:
            continue
        # The gate re-checks that the figure is inside the quote; agree with it here.
        if normalised in normalise_number(span.quote):
            return span
    return None


def _anchored_numbers(record: EvidenceRecord) -> Iterable[tuple[Any, Span | None]]:
    if record.sample_size.is_known:
        yield record.sample_size.value, record.sample_size.span

    for outcome in record.outcomes:
        effect = outcome.effect
        for item in (effect.magnitude, effect.ci_low, effect.ci_high):
            if item.is_known:
                yield item.value, item.span


def topic_key_for(records: Sequence[EvidenceRecord], *, hint: Any = None) -> str:
    """Build the clustering key: intervention and outcome, normalised.

    A model-supplied ``hint`` is used only when the extracts have nothing to offer, and
    even then it is slugged rather than trusted as prose. The key decides what counts as
    "the same finding" for the repetition window, so it must be stable across runs.
    """

    interventions = [
        record.intervention.value
        for record in records
        if record.intervention.is_known and record.intervention.value
    ]
    outcomes = [outcome.name for record in records for outcome in record.outcomes if outcome.name]

    parts = []
    if interventions:
        parts.append(_slug(min(interventions, key=len)))
    if outcomes:
        parts.append(_slug(min(outcomes, key=len)))

    if not parts and hint:
        parts.append(_slug(str(hint)))

    return "|".join(part for part in parts if part) or "unclassified"


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    # Long extracted phrases would make every run a new "topic", defeating the point.
    return "-".join(slug.split("-")[:4])


def _render_evidence(handles: EvidenceHandles, records: Mapping[str, EvidenceRecord]) -> str:
    """Render the extracts. No document text, no URLs — only anchored fields."""

    blocks: list[str] = []
    for handle, key in handles.by_handle.items():
        record = records.get(key)
        if record is None:
            continue

        classification = record.classification
        lines = [
            f"[{handle}] {record.title or key}",
            f"  design: {classification.design}; subject: {classification.subject}",
            f"  sample size: {_field(record.sample_size)}",
            f"  population: {_field(record.population)}",
            f"  intervention: {_field(record.intervention)}",
            f"  comparator: {_field(record.comparator)}",
            f"  limitations: {_field(record.limitations)}",
        ]
        for outcome in record.outcomes:
            lines.append(
                f"  outcome '{outcome.name}': direction {_field(outcome.direction)}, "
                f"magnitude {_field(outcome.effect.magnitude)} {_field(outcome.effect.unit)}, "
                f"surrogate {_field(outcome.is_surrogate)}"
            )
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) or "No extracted evidence."


def _field(value: Any) -> str:
    """Render an extracted field, saying plainly when it is unknown (I3)."""

    if getattr(value, "is_known", False):
        return str(value.value)
    status = getattr(value, "status", "not_extractable")
    return "not reported" if status == "not_reported" else "not extractable"
