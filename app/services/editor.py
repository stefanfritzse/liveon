"""Editor agent that refines article drafts using an LLM."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.utils.json_repair import invoke_json_object
from app.utils.langchain_compat import ChatPromptTemplate

from app.models.editor import EditedArticle, rejected_sources
from app.models.summarizer import ArticleDraft
from app.services.summarizer import SupportsInvoke
from dataclasses import is_dataclass, asdict
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger("liveon.editor")

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

def _json_default(o):
    if isinstance(o, (datetime, date)):
        # ensure timezone-aware ISO format for consistency
        if isinstance(o, datetime) and o.tzinfo is None:
            o = o.replace(tzinfo=timezone.utc)
        return o.isoformat()
    if is_dataclass(o):
        return asdict(o)
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, set):
        return list(o)
    # fallback
    return str(o)

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
            draft=json.dumps(payload, default=_json_default, ensure_ascii=False),
            current_date=datetime.now(timezone.utc).date().isoformat(),
        )
        data = invoke_json_object(self.llm, messages, label="Editor", logger=logger)
        edited = EditedArticle(
            title=data.get("title", draft.title),
            summary=data.get("summary", draft.summary),
            body=data.get("body", draft.body),
            takeaways=list(data.get("takeaways", []) or []),
            sources=list(data.get("sources", []) or []),
            tags=list(data.get("tags", []) or []),
            disclaimer=data.get("disclaimer"),
        )
        invented = rejected_sources(draft.sources, edited.sources)
        if invented:
            logger.warning(
                "Editor returned %d source URL(s) not present in the feed; dropping them: %s",
                len(invented),
                ", ".join(invented),
                extra={"event": "editor.sources_rejected", "urls": invented},
            )
        return edited.normalised(draft)
