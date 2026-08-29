"""Tip generator agent that distils research notes into actionable advice."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import re
from typing import Any, Protocol, Sequence
from urllib.parse import urlparse

from jinja2 import Template

from app.utils.json_repair import invoke_json_object
from app.utils.langchain_compat import BaseMessage, ChatPromptTemplate

from app.models.tip import TipDraft
from app.models.tip_context import TipGenerationContext

#: Published tip bodies are trimmed before entering the generator prompt.
_PUBLISHED_BODY_CHARS = 160


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

{% if guidance %}
Today's focus: {{ guidance }}
{% endif %}

{% if published %}
Already published recently — pick a DIFFERENT angle or a different research note, and
do not restate any of these:
{{ published }}
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
    """Generate tip drafts using structured context and LangChain prompts."""

    llm: SupportsInvoke
    prompt: ChatPromptTemplate = field(default_factory=_default_prompt)

    def generate(
        self,
        *,
        context: TipGenerationContext,
        feedback: str | None = None,
        published_tips: Sequence[Any] = (),
    ) -> TipDraft:
        """Produce a tip draft from structured context, feedback, and publication history.

        The editor has always known what was published; the generator did not. That
        asymmetry made the review loop unconvergeable — the generator kept re-mining a
        story it had already covered and could not see why it was being rejected.
        """

        notes_block = context.notes_block()
        sources_block = context.sources_block()
        current_date = self._format_date(context.current_date)
        guidance = context.guidance or context.theme

        tip_prompt = self._render_tip_prompt(
            notes=notes_block,
            sources=sources_block,
            current_date=current_date,
            guidance=guidance,
            feedback=feedback,
            published=self._format_published(published_tips),
        )
        messages = self.prompt.format_messages(
            tip_prompt=tip_prompt,
            notes=notes_block,
            sources=sources_block,
            current_date=current_date,
            guidance=guidance,
            feedback=feedback,
        )

        payload = invoke_json_object(self.llm, messages, label="Tip generator")
        tags = self._coerce_tags(payload.get("tags"))
        metadata = self._coerce_metadata(payload.get("metadata"))

        merged_sources = self._merge_sources(context.sources, metadata.get("sources", []))
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
    def _format_date(value: date | datetime | None) -> str:
        if value is None:
            return datetime.now(timezone.utc).date().isoformat()
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).date().isoformat()
        return value.isoformat()

    @staticmethod
    def _format_published(published_tips: Sequence[Any]) -> str:
        """Render recent tip titles and bodies for the "do not repeat" block."""

        lines: list[str] = []
        for tip in published_tips:
            title = str(getattr(tip, "title", "") or "").strip()
            body = str(
                getattr(tip, "content_body", "") or getattr(tip, "body", "") or ""
            ).strip()
            if not title and not body:
                continue
            body = " ".join(body.split())
            if len(body) > _PUBLISHED_BODY_CHARS:
                body = body[:_PUBLISHED_BODY_CHARS].rstrip() + "…"
            lines.append(f"- {title or '(untitled)'}: {body}" if body else f"- {title}")
        return "\n".join(lines)

    @staticmethod
    def _render_tip_prompt(
        *,
        notes: str,
        sources: str,
        current_date: str,
        guidance: str | None,
        feedback: str | None,
        published: str = "",
    ) -> str:
        """Render the human prompt via Jinja to conditionally include extra guidance."""

        return (
            TIP_HUMAN_PROMPT.render(
                notes=notes,
                sources=sources,
                current_date=current_date,
                guidance=guidance,
                feedback=feedback,
                published=published,
            ).strip()
        )


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
