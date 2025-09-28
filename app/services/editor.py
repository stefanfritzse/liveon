"""Editor agent that refines article drafts using an LLM."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.prompts import ChatPromptTemplate

from app.models.editor import EditedArticle
from app.models.summarizer import ArticleDraft
from app.services.summarizer import SupportsInvoke

DEFAULT_SYSTEM_PROMPT = (
    "You are the editorial agent for Live On, an AI longevity coach. "
    "Polish drafts so they are factual, encouraging, and medically responsible. "
    "Ensure claims are grounded in the provided sources and flag any uncertainties."
)

DEFAULT_HUMAN_PROMPT = """
You will receive the summariser's draft article as JSON.
Review it for clarity, accuracy, and tone. Strengthen citations, add a brief reader-friendly
summary, and include a single-sentence disclaimer reminding readers to consult healthcare
professionals.

Return **valid JSON** with the following structure:
{{
  "title": "Updated headline",
  "summary": "2-3 sentence refined overview",
  "body": "Rewritten Markdown body",
  "takeaways": ["Bulleted", "Key lessons"],
  "sources": ["https://validated.source"],
  "tags": ["keyword"],
  "disclaimer": "Optional short disclaimer"
}}

Draft article JSON:
{draft}

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
class EditorAgent:
    """Refine :class:`ArticleDraft` instances into polished articles."""

    llm: SupportsInvoke
    prompt: ChatPromptTemplate = field(default_factory=_default_prompt)

    def revise(self, draft: ArticleDraft) -> EditedArticle:
        """Revise the provided draft, returning an :class:`EditedArticle`."""

        payload = {
            "title": draft.title,
            "summary": draft.summary,
            "body": draft.body,
            "takeaways": draft.takeaways,
            "sources": draft.sources,
            "tags": draft.tags,
        }
        messages = self.prompt.format_messages(
            draft=json.dumps(payload, ensure_ascii=False, indent=2),
            current_date=datetime.now(timezone.utc).date().isoformat(),
        )
        response = self.llm.invoke(messages)
        content = self._extract_content(response)
        data = self._parse_payload(content)
        edited = EditedArticle(
            title=data.get("title", draft.title),
            summary=data.get("summary", draft.summary),
            body=data.get("body", draft.body),
            takeaways=list(data.get("takeaways", []) or []),
            sources=list(data.get("sources", []) or []),
            tags=list(data.get("tags", []) or []),
            disclaimer=data.get("disclaimer"),
        )
        return edited.normalised(draft)

    @staticmethod
    def _extract_content(response: BaseMessage | str) -> str:
        if isinstance(response, AIMessage):
            return response.content or ""
        if isinstance(response, BaseMessage):
            return str(getattr(response, "content", "") or "")
        return str(response)

    @staticmethod
    def _parse_payload(content: str) -> dict[str, Any]:
        text = content.strip()
        if not text:
            raise ValueError("Editor response was not valid JSON")

        candidates: list[str] = []
        fenced = EditorAgent._strip_code_fence(text)
        if fenced:
            candidates.append(fenced)
        candidates.append(text)

        for candidate in candidates:
            try:
                return EditorAgent._ensure_mapping(json.loads(candidate))
            except json.JSONDecodeError:
                continue

        scanned = EditorAgent._scan_for_object(text)
        if scanned is not None:
            return EditorAgent._ensure_mapping(scanned)

        raise ValueError("Editor response was not valid JSON")

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
            raise ValueError("Editor response JSON must be an object")
        return payload
