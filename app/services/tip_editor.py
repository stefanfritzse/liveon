"""Tip editor agent that reviews generated tips with an LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.models.tip import TipDraft
from app.models.tip_editor import TipReviewResult
from app.services.tip_generator import SupportsInvoke
from app.utils.json_repair import invoke_json_object
from app.utils.langchain_compat import ChatPromptTemplate

#: Recent tip bodies are trimmed to this length before entering the novelty prompt.
_EXISTING_TIP_BODY_CHARS = 200


TIP_EDITOR_SYSTEM_PROMPT = """You are an exacting, helpful, and concise senior editor for a health and wellness publication. Your sole purpose is to act as a quality-control gate for AI-generated content. You must be strict.

Your review will be based on these 5 criteria:
1.  **Concise:** The tip (title + body) should be short and easily digestible. Under 60 words is ideal.
2.  **Interesting / Novel:** The tip must not be generic (e.g., "sleep more," "drink water," "exercise"). It should provide a specific insight or actionable advice that the user may not have known.
3.  **Actionable:** The user must be able to act on the tip.
4.  **High-Quality:** The text must be grammatically perfect, clear, and written in an encouraging, professional tone.
5.  **Non-Repetitive:** The tip must be sufficiently different from recently published tips.

You MUST respond in a specific JSON format and nothing else. Your entire response must be only the JSON object.
"""

TIP_EDITOR_HUMAN_PROMPT = """
Please review the tip draft below and decide if it meets the rubric.

Compare it against the recent tips list to check for repetition:
{existing_tips}

Tip draft JSON:
{draft_json}

Based on the 5 criteria (Concise, Interesting, Actionable, High-Quality, Non-Repetitive), evaluate the draft.

If the draft is excellent and meets all criteria, set "is_approved" to true. You may optionally provide minor copy edits in "revised_draft" to improve it further.

If the draft fails *any* criterion, set "is_approved" to false. In the "feedback" field, provide a single, clear, constructive sentence for the writer, explaining *why* it was rejected and *how* to fix it.

**Respond with ONLY the raw JSON object.**

Example of a GOOD response (for approval):
{{
  "is_approved": true,
  "feedback": "Clear and actionable.",
  "revised_draft": {{
    "title": "A Slightly Better Title",
    "body": "The original body text, but with a small typo fixed.",
    "tags": ["nutrition", "fasting"]
  }}
}}

Example of a GOOD response (for rejection):
{{
  "is_approved": false,
  "feedback": "This tip is too generic. Please provide a more specific, actionable insight related to the source material.",
  "revised_draft": null
}}

Your response:
""".strip()


def _default_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            ("system", TIP_EDITOR_SYSTEM_PROMPT),
            ("human", TIP_EDITOR_HUMAN_PROMPT),
        ]
    )


@dataclass(slots=True)
class TipEditorAgent:
    """Review :class:`TipDraft` instances and decide whether to publish them."""

    llm: SupportsInvoke
    prompt: ChatPromptTemplate = field(default_factory=_default_prompt)

    def review(
        self,
        draft: TipDraft,
        existing_tips: Sequence[Any] | None = None,
    ) -> TipReviewResult:
        """Review a draft tip and return structured feedback."""

        messages = self.prompt.format_messages(
            existing_tips=self._format_existing_titles(existing_tips or ()),
            draft_json=self._draft_to_json(draft),
        )
        payload = invoke_json_object(self.llm, messages, label="Tip editor")

        is_approved = bool(payload.get("is_approved"))
        feedback = self._normalise_feedback(payload.get("feedback"))
        revised_draft = self._coerce_revised_draft(payload.get("revised_draft"), draft)

        return TipReviewResult(
            is_approved=is_approved,
            feedback=feedback,
            revised_draft=revised_draft,
        )

    @staticmethod
    def _draft_to_json(draft: TipDraft) -> str:
        payload = {
            "title": draft.title,
            "body": draft.body,
            "tags": list(draft.tags),
            "metadata": dict(draft.metadata),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def _format_existing_titles(existing_tips: Sequence[Any]) -> str:
        """Render recent tips as title *and* body for the novelty comparison.

        Titles alone hid near-duplicates: two tips can be worded differently and
        still give the same advice, which is exactly what the rubric is meant to
        catch. Bodies are trimmed so a long backlog cannot crowd out the draft.
        """

        if isinstance(existing_tips, str):
            iterable: Sequence[Any] = [existing_tips]
        else:
            iterable = existing_tips

        entries: list[str] = []
        for tip in iterable:
            title = TipEditorAgent._extract_field(tip, "title")
            body = TipEditorAgent._extract_field(tip, "content_body") or (
                TipEditorAgent._extract_field(tip, "body")
            )
            if not title and not body:
                continue
            if body:
                body = " ".join(body.split())
                if len(body) > _EXISTING_TIP_BODY_CHARS:
                    body = body[:_EXISTING_TIP_BODY_CHARS].rstrip() + "…"
            entries.append(f"- {title or '(untitled)'}: {body}" if body else f"- {title}")

        if not entries:
            # Saying "None" invited the model to reject drafts as "repetitive of
            # recently published tips" that do not exist. Be explicit instead.
            return (
                "(No tips have been published yet, so the Non-Repetitive criterion "
                "cannot fail. Do not claim repetition.)"
            )

        return "\n".join(entries)

    @staticmethod
    def _extract_field(tip: Any, field: str) -> str:
        if isinstance(tip, str):
            return tip.strip() if field == "title" else ""

        candidate = getattr(tip, field, None)
        if isinstance(candidate, str):
            return candidate.strip()

        if isinstance(tip, dict):
            value = tip.get(field)
            if isinstance(value, str):
                return value.strip()

        return ""

    @staticmethod
    def _normalise_feedback(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _coerce_revised_draft(value: Any, fallback: TipDraft) -> TipDraft | None:
        if not isinstance(value, dict):
            return None

        title = str(value.get("title", fallback.title))
        body = str(value.get("body", fallback.body))
        tags = TipEditorAgent._coerce_tags(value.get("tags")) or list(fallback.tags)
        metadata = TipEditorAgent._coerce_metadata(value.get("metadata")) or dict(fallback.metadata)

        revised = TipDraft(
            title=title,
            body=body,
            tags=tags,
            metadata=metadata,
        )
        return revised.with_defaults()

    @staticmethod
    def _coerce_tags(value: Any) -> list[str]:
        if value is None:
            return []
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
