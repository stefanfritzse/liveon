"""Turn an approved bundle into an article or a tip.

Both writers work from the same reviewed evidence, which is the point of item 5: articles
and tips can no longer disagree because they each read a different headline.

What a writer is allowed to do is deliberately narrow. It receives a *frozen claim set*
and opaque handles, and it may arrange, explain and shorten. It may not add a claim, a
number, or a source: anything it writes is checked back against the bundle, and the
citations, the grade line and the source URLs are rendered from stored records rather
than from anything the model produced.

The editorial agents that already exist keep their jobs — the tip editor's rubric is a
good one — but they now edit inside these walls rather than deciding what is true.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Mapping, Protocol

from app.models.evidence import EvidenceBundle, EvidenceRecord
from app.models.summarizer import ArticleDraft
from app.models.tip import TipDraft
from app.services.evidence.citations import EvidenceHandles, citation_url, strip_handles
from app.services.evidence.grading import describe_grade
from app.utils.json_repair import invoke_json_object
from app.utils.langchain_compat import BaseMessage, ChatPromptTemplate

LOGGER = logging.getLogger(__name__)

__all__ = ["ArticleWriter", "TipWriter", "WRITER_PROMPT_VERSION", "evidence_fields"]

WRITER_PROMPT_VERSION = "1"


class SupportsInvoke(Protocol):
    def invoke(self, input: Any, **kwargs: Any) -> BaseMessage | str:  # pragma: no cover
        ...


def evidence_fields(
    bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]
) -> dict[str, Any]:
    """The provenance every published item carries, built entirely in code.

    ``evidence_summary`` is the reader-facing line from item 8 — "Moderate — one human
    randomised trial" — assembled from the records themselves so it cannot drift from
    what was actually cited.
    """

    keys = bundle.source_keys()
    cited = [records[key] for key in keys if key in records]
    return {
        "evidence_bundle_id": bundle.bundle_id,
        "evidence_keys": list(keys),
        "evidence_grade": bundle.grade,
        "evidence_summary": describe_grade(bundle.grade, cited),
        "source_urls": [url for url in (citation_url(key) for key in keys) if url],
    }


def _claims_block(bundle: EvidenceBundle, handles: EvidenceHandles) -> str:
    """Render the frozen claim set, each line tagged with the handles behind it."""

    lines: list[str] = []
    for claim in bundle.claims:
        tags = " ".join(
            f"[{handles.handle_for(key)}]"
            for key in claim.evidence_keys
            if handles.handle_for(key)
        )
        lines.append(f"- {claim.text} {tags}".rstrip())
        if claim.limitations:
            lines.append(f"    limitation: {'; '.join(claim.limitations)}")
        if claim.contradicted_by:
            lines.append("    note: other evidence disagrees with this")
        if claim.population_scope:
            lines.append(f"    applies to: {claim.population_scope}")
    return "\n".join(lines) or "No claims."


ARTICLE_SYSTEM_PROMPT = (
    "You are Live On, a longevity publication. You write from a fixed set of reviewed "
    "claims and you never add to them. Your job is clarity: arrange what is already known "
    "into something a careful reader can act on, and be honest about how settled it is."
)

ARTICLE_HUMAN_PROMPT = """
Write a short article from these reviewed claims. Return valid JSON:

{{
  "title": "string",
  "summary": "2-3 sentences",
  "body": "Markdown, 3-5 short paragraphs",
  "takeaways": ["bullet", "points"],
  "tags": ["keyword"]
}}

Rules:
- Use only the claims below. Do not add findings, numbers, mechanisms or studies.
- Keep every number exactly as written. Do not round, convert, or restate it.
- Cite with the handles as given, e.g. [E1]. Never write a URL, a DOI or a journal name.
- Match the certainty of the claim: if it says "was associated with", do not write
  "reduces".
- Say what is still uncertain. The evidence grade for this set is {grade}.

Claims:
{claims}

Sources:
{handles}
""".strip()


TIP_SYSTEM_PROMPT = (
    "You are Live On, a longevity coach. You turn one reviewed finding into a single "
    "practical habit. You never add advice the evidence does not support."
)

TIP_HUMAN_PROMPT = """
Write one short daily tip from these reviewed claims. Return valid JSON:

{{
  "title": "short, specific",
  "body": "2-4 sentences: the action, and why the evidence supports it",
  "tags": ["keyword"]
}}

Rules:
- Use only the claims below. Do not add findings, numbers or studies.
- Keep every number exactly as written.
- Name a concrete behaviour someone can do today.
- No doses, no medical instructions, no promises about diseases.
- Match the certainty of the claim. The evidence grade for this set is {grade}.

Claims:
{claims}

Sources:
{handles}
""".strip()


def _prompt(system: str, human: str) -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages([("system", system), ("human", human)])


@dataclass(slots=True)
class _BundleWriter:
    """Shared plumbing: prompt the model, then keep only what code can vouch for."""

    llm: SupportsInvoke
    model_id: str = "unknown"
    prompt_version: str = WRITER_PROMPT_VERSION

    def _invoke(
        self,
        prompt: ChatPromptTemplate,
        bundle: EvidenceBundle,
        records: Mapping[str, EvidenceRecord],
        label: str,
    ) -> tuple[dict[str, Any], EvidenceHandles]:
        handles = EvidenceHandles.for_keys(bundle.source_keys())
        titles = {key: record.title for key, record in records.items()}

        payload = invoke_json_object(
            self.llm,
            prompt.format_messages(
                claims=_claims_block(bundle, handles),
                handles=handles.prompt_block(titles),
                grade=bundle.grade,
            ),
            label=label,
            logger=LOGGER,
        )
        return payload, handles


@dataclass(slots=True)
class ArticleWriter(_BundleWriter):
    """Write an :class:`ArticleDraft` from an approved bundle."""

    prompt: ChatPromptTemplate = field(
        default_factory=lambda: _prompt(ARTICLE_SYSTEM_PROMPT, ARTICLE_HUMAN_PROMPT)
    )

    def write(
        self, bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]
    ) -> ArticleDraft:
        payload, handles = self._invoke(self.prompt, bundle, records, "ArticleWriter")
        provenance = evidence_fields(bundle, records)

        body = _clean_body(str(payload.get("body") or ""), handles, bundle)

        draft = ArticleDraft(
            title=str(payload.get("title") or "").strip(),
            summary=str(payload.get("summary") or "").strip(),
            body=body,
            takeaways=[
                strip_handles(str(item))
                for item in payload.get("takeaways") or []
                if str(item).strip()
            ],
            # Sources are rendered from the bundle, never from the model's reply.
            sources=list(provenance["source_urls"]),
            tags=[str(tag).strip() for tag in payload.get("tags") or [] if str(tag).strip()],
        )
        return draft.with_defaults()


@dataclass(slots=True)
class TipWriter(_BundleWriter):
    """Write a :class:`TipDraft` from an approved bundle."""

    prompt: ChatPromptTemplate = field(
        default_factory=lambda: _prompt(TIP_SYSTEM_PROMPT, TIP_HUMAN_PROMPT)
    )

    def write(self, bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]) -> TipDraft:
        payload, handles = self._invoke(self.prompt, bundle, records, "TipWriter")
        provenance = evidence_fields(bundle, records)

        draft = TipDraft(
            title=str(payload.get("title") or "").strip(),
            body=_clean_body(str(payload.get("body") or ""), handles, bundle),
            tags=[str(tag).strip() for tag in payload.get("tags") or [] if str(tag).strip()],
            source_urls=list(provenance["source_urls"]),
            evidence_bundle_id=provenance["evidence_bundle_id"],
            evidence_keys=list(provenance["evidence_keys"]),
            evidence_grade=provenance["evidence_grade"],
            evidence_summary=provenance["evidence_summary"],
        )
        return draft.with_defaults()


def _clean_body(body: str, handles: EvidenceHandles, bundle: EvidenceBundle) -> str:
    """Strip citation markers, logging any handle the writer invented.

    An unknown handle is not repaired into a real one — that would be inventing a citation
    on the model's behalf. It is simply removed, and the claim it decorated is left to
    face the post-edit re-check without support.
    """

    _, unknown = handles.resolve_all(body)
    if unknown:
        LOGGER.info(
            "Writer cited %s handle(s) that were never issued",
            len(unknown),
            extra={
                "event": "evidence.writer_unknown_handle",
                "bundle_id": bundle.bundle_id,
                "handles": ",".join(unknown),
            },
        )
    return strip_handles(body)
