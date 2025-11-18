"""Tip generator agent that distils research notes into actionable advice."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Protocol, Sequence
from urllib.parse import urlparse

from jinja2 import Template

from app.utils.langchain_compat import AIMessage, BaseMessage, ChatPromptTemplate

from app.models.aggregator import AggregatedContent
from app.models.summarizer import SummarizerContext
from app.models.tip import TipDraft


class SupportsInvoke(Protocol):
    """Protocol describing the subset of LangChain interfaces we rely on."""

    def invoke(self, input: Any, **kwargs: Any) -> BaseMessage | str:
        """Invoke the underlying language model."""


TIP_SYSTEM_PROMPT = (
    "You are Live On, an AI longevity coach crafting concise, actionable tips. "
    "Keep the tone encouraging, evidence-informed, and accessible to busy readers."
)

TIP_HUMAN_PROMPT = Template(
    """
{% if feedback %}
A previous tip draft was rejected by our editor. Please generate a fresh tip from the research notes and sources below, explicitly addressing the feedback. Keep the copy tight and practical.

Editor feedback:
{{ feedback }}
{% else %}
Using the research notes below, craft ONE concise longevity tip (2-3 sentences or a short intro plus up to 2 bullets). Make it sound like advice a health coach would give for today.
{% endif %}

Rules you MUST follow:
- The title must be under 12 words and feel like a clear action or benefit.
- The body must avoid URLs and raw source names. Summarise the takeaway in plain English.
- Mention the specific behaviour (e.g., snack on carrots, schedule a strength session) and explicitly say why it helps longevity.
- Do not invent data; if unsure, keep the claim high level but still actionable.

Research notes:
{{ notes }}

Key sources:
{{ sources }}

Current date: {{ current_date }}

{% raw %}
Respond with ONLY the JSON object in this exact structure:
{
  "title": "short tip title",
  "body": "plain text with <=3 sentences or short list",
  "tags": ["keywords"],
  "metadata": {
    "sources": ["https://..."],
    "confidence": "low|medium|high"
  }
}
{% endraw %}
""".strip()
)


def _default_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            ("system", TIP_SYSTEM_PROMPT),
            ("human", "{tip_prompt}"),
        ]
    )


@dataclass(slots=True)
class TipGenerator:
    """Generate tip drafts from aggregated longevity updates using LangChain."""

    llm: SupportsInvoke
    prompt: ChatPromptTemplate = field(default_factory=_default_prompt)

    def generate(
        self,
        items: Sequence[AggregatedContent],
        feedback: str | None = None,
    ) -> TipDraft:
        """Produce a tip draft from aggregated content with optional feedback to guide revisions."""

        if not items:
            raise ValueError("At least one aggregated content item is required")

        context = SummarizerContext.from_aggregated(items)
        tip_notes = context.to_tip_notes()
        notes_block = "\n".join(tip_notes) if tip_notes else "No concise notes available."
        sources_block = "\n".join(context.source_urls) if context.source_urls else "Not provided"
        current_date = datetime.now(timezone.utc).date().isoformat()

        tip_prompt = self._render_tip_prompt(
            notes=notes_block,
            sources=sources_block,
            current_date=current_date,
            feedback=feedback,
        )
        messages = self.prompt.format_messages(
            tip_prompt=tip_prompt,
            notes=notes_block,
            sources=sources_block,
            current_date=current_date,
            feedback=feedback,
        )

        response = self.llm.invoke(messages)
        content = self._extract_content(response)
        payload = self._parse_payload(content)
        tags = self._coerce_tags(payload.get("tags"))
        metadata = self._coerce_metadata(payload.get("metadata"))

        merged_sources = self._merge_sources(context.source_urls, metadata.get("sources", []))
        if merged_sources:
            metadata["sources"] = merged_sources

        body = self._normalise_body(str(payload.get("body", "")))

        draft = TipDraft(
            title=str(payload.get("title", "")),
            body=body,
            tags=tags,
            metadata=metadata,
        )
        return draft.with_defaults()

    @staticmethod
    def _render_tip_prompt(
        *,
        notes: str,
        sources: str,
        current_date: str,
        feedback: str | None,
    ) -> str:
        """Render the human prompt via Jinja to conditionally include feedback guidance."""

        return TIP_HUMAN_PROMPT.render(
            notes=notes,
            sources=sources,
            current_date=current_date,
            feedback=feedback,
        ).strip()

    @staticmethod
    def _extract_content(response: BaseMessage | str) -> str:
        if isinstance(response, AIMessage):
            return response.content or ""
        if isinstance(response, BaseMessage):
            return str(response.content) if getattr(response, "content", None) else ""
        return str(response)

    @staticmethod
    def _parse_payload(content: str) -> dict[str, Any]:
        text = content.strip()
        if not text:
            raise ValueError("Tip generator response was not valid JSON")

        candidates: list[str] = []
        fenced = TipGenerator._strip_code_fence(text)
        if fenced:
            candidates.append(fenced)
        candidates.append(text)

        for candidate in candidates:
            try:
                return TipGenerator._ensure_mapping(json.loads(candidate))
            except json.JSONDecodeError:
                continue

        scanned = TipGenerator._scan_for_object(text)
        if scanned is not None:
            return TipGenerator._ensure_mapping(scanned)

        raise ValueError("Tip generator response was not valid JSON")

    @staticmethod
    def _strip_code_fence(text: str) -> str | None:
        if not text.startswith("```"):
            return None

        closing_index = text.rfind("```")
        if closing_index <= 0:
            return None

        first_linebreak = text.find("\n")
        if first_linebreak == -1:
            content = text[3:closing_index]
        else:
            content = text[first_linebreak + 1 : closing_index]

        cleaned = content.strip()
        return cleaned or None

    @staticmethod
    def _scan_for_object(text: str) -> dict[str, Any] | None:
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                payload, _ = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        return None

    @staticmethod
    def _ensure_mapping(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Tip generator response JSON must be an object")
        return payload

    @staticmethod
    def _coerce_tags(value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, Sequence):
            return []

        tags: list[str] = []
        for item in value:
            if isinstance(item, str):
                trimmed = item.strip()
                if trimmed:
                    tags.append(trimmed)
        return tags

    @staticmethod
    def _coerce_metadata(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        return {}

    @staticmethod
    def _merge_sources(primary: Sequence[str], secondary: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        merged: list[str] = []
        for source in list(primary) + list(secondary):
            normalized = source.strip() if isinstance(source, str) else ""
            if normalized and normalized not in seen:
                seen.add(normalized)
                merged.append(normalized)
        return merged

    @staticmethod
    def _normalise_body(text: str) -> str:
        """Shorten verbose URLs and normalise anchor tags for readability."""

        if not text:
            return ""

        normalised = text.replace("“", '"').replace("”", '"')
        normalised = TipGenerator._replace_anchor_tags(normalised)
        normalised = TipGenerator._shorten_plain_urls(normalised)
        normalised = re.sub(r"Key sources::", "Key sources:", normalised, flags=re.IGNORECASE)
        return normalised

    @staticmethod
    def _replace_anchor_tags(text: str) -> str:
        anchor_re = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)

        def _replacement(match: re.Match[str]) -> str:
            url = match.group(1).strip()
            label = re.sub(r"\s+", " ", match.group(2)).strip()
            if not label:
                label = urlparse(url).netloc or url
            return f"[{label}]({url})"

        return anchor_re.sub(_replacement, text)

    @staticmethod
    def _shorten_plain_urls(text: str) -> str:
        url_re = re.compile(r"(https?://[^\s)]+)")

        def _replacement(match: re.Match[str]) -> str:
            url = match.group(1).rstrip(".,")
            domain = urlparse(url).netloc or url
            return f"[{domain}]({url})"

        return url_re.sub(_replacement, text)
