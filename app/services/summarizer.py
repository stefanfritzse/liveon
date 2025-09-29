"""Summarizer agent that turns aggregated updates into a polished article draft."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, Sequence

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.prompts import ChatPromptTemplate

from app.models.aggregator import AggregatedContent
from app.models.summarizer import ArticleDraft, SummarizerContext


class SupportsInvoke(Protocol):
    """Protocol describing the subset of LangChain interfaces we rely on."""

    def invoke(self, input: Any, **kwargs: Any) -> BaseMessage | str:
        """Invoke the underlying language model."""


DEFAULT_SYSTEM_PROMPT = (
    "You are Live On, an AI longevity coach. You turn research updates into actionable articles. "
    "Write with an encouraging, trustworthy tone for readers seeking healthy aging guidance."
)

DEFAULT_HUMAN_PROMPT = """
Use the provided longevity research notes to draft a concise article.
Return valid JSON with the shape:
{{
  "title": "string",
  "summary": "2-3 sentence overview",
  "body": "Markdown formatted body",
  "takeaways": ["bullet", "points"],
  "sources": ["https://..."],
  "tags": ["keyword"]
}}

Notes:
{notes}

Current date: {current_date}
""".strip()


def _default_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            ("system", DEFAULT_SYSTEM_PROMPT),
            ("human", DEFAULT_HUMAN_PROMPT),
        ]
    )


@dataclass(slots=True)
class SummarizerAgent:
    """Generate article drafts from aggregated longevity updates using LangChain."""

    llm: SupportsInvoke
    prompt: ChatPromptTemplate = field(default_factory=_default_prompt)

    def summarize(self, items: Sequence[AggregatedContent]) -> ArticleDraft:
        """Summarise aggregated content into an article draft."""

        if not items:
            raise ValueError("At least one aggregated content item is required")

        context = SummarizerContext.from_aggregated(items)
        messages = self.prompt.format_messages(
            notes="\n".join(context.bullet_points),
            current_date=datetime.now(timezone.utc).date().isoformat(),
        )

        response = self.llm.invoke(messages)
        content = self._extract_content(response)
        payload = self._parse_payload(content)
        draft = ArticleDraft(
            title=payload.get("title", ""),
            summary=payload.get("summary", ""),
            body=payload.get("body", ""),
            takeaways=list(payload.get("takeaways", []) or []),
            sources=self._merge_sources(context.source_urls, payload.get("sources", [])),
            tags=list(payload.get("tags", []) or []),
        )
        return draft.with_defaults()

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
            raise ValueError("Summarizer response was not valid JSON")

        candidates: list[str] = []
        fenced = SummarizerAgent._strip_code_fence(text)
        if fenced:
            candidates.append(fenced)
        candidates.append(text)

        for candidate in candidates:
            try:
                return SummarizerAgent._ensure_mapping(json.loads(candidate))
            except json.JSONDecodeError:
                continue

        scanned = SummarizerAgent._scan_for_object(text)
        if scanned is not None:
            return SummarizerAgent._ensure_mapping(scanned)

        raise ValueError("Summarizer response was not valid JSON")

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
            raise ValueError("Summarizer response JSON must be an object")
        return payload

    @staticmethod
    def _merge_sources(primary: Sequence[str], secondary: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        merged: list[str] = []
        for source in list(primary) + list(secondary):
            normalized = source.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                merged.append(normalized)
        return merged
