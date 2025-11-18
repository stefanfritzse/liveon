"""Conversational coach agent that generates responses using Ollama."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence
import os

import httpx

from app.models.coach import CoachAnswer, CoachQuestion

try:  # pragma: no cover - optional dependency guard
    from langchain_community.chat_models import ChatOllama
    from langchain_core.prompts import ChatPromptTemplate
except ImportError:  # pragma: no cover - handled gracefully in CoachAgent
    ChatOllama = None  # type: ignore[assignment]
    ChatPromptTemplate = None  # type: ignore[assignment]


_DEFAULT_SAFETY_INSTRUCTIONS = (
    "You are LiveOn's Longevity Coach. Offer supportive, educational guidance grounded in"
    " general best practices. Do not diagnose, prescribe, or promise outcomes, and always"
    " encourage the user to consult qualified healthcare professionals for personalised advice."
    " Whenever it is plausible, frame insights through the lens of healthy ageing and human"
    " longevity so the user understands the long-term wellbeing impact of each suggestion."
)

_DEFAULT_DISCLAIMER = ""


@dataclass(slots=True)
class CoachAgent:
    """High level orchestration for answering user questions with Ollama."""

    llm: Any
    safety_instructions: str = _DEFAULT_SAFETY_INSTRUCTIONS
    default_disclaimer: str = _DEFAULT_DISCLAIMER
    _prompt: ChatPromptTemplate | None = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        if ChatPromptTemplate is not None:
            self._prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "{safety_instructions}\n"
                        "Respond in a warm, empathetic tone while staying factual and concise."
                    ),
                    (
                        "human",
                        "User question:\n{question}\n\n"
                    "Structure the response with a short introduction, practical guidance, and"
                    " a concluding encouragement. Clearly tie the guidance back to sustaining"
                    " long-term healthspan and longevity when it is relevant to do so.",
                    ),
                ]
            )

    def ask(self, question: CoachQuestion | str) -> CoachAnswer:
        """Answer ``question`` using the configured language model."""

        question_model = question if isinstance(question, CoachQuestion) else CoachQuestion(text=str(question))
        normalized_question = question_model.stripped()

        prompt_value = self._build_prompt(normalized_question)

        response = self._invoke_llm(prompt_value)
        response_text = self._extract_response_text(response)
        message, disclaimer = _separate_disclaimer(response_text, default=self.default_disclaimer)
        return CoachAnswer(message=message, disclaimer=disclaimer)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _invoke_llm(self, prompt_value: Any) -> Any:
        if hasattr(prompt_value, "to_messages"):
            messages = prompt_value.to_messages()  # type: ignore[assignment]
        else:
            messages = prompt_value

        if hasattr(self.llm, "invoke"):
            try:
                return self.llm.invoke(messages)
            except TypeError:
                return self.llm.invoke(getattr(prompt_value, "to_string", lambda: prompt_value)())

        if callable(self.llm):
            return self.llm(messages)

        raise TypeError("LLM implementation must provide an 'invoke' method or be callable.")

    def _extract_response_text(self, response: Any) -> str:
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        if hasattr(response, "content"):
            content = response.content  # type: ignore[attr-defined]
            if isinstance(content, list):
                return "".join(str(part) for part in content)
            return str(content)
        if isinstance(response, dict) and "content" in response:
            return str(response["content"])
        return str(response)

    def _build_prompt(self, question: str) -> Any:
        """Create a prompt payload regardless of LangChain availability."""

        if self._prompt is not None:
            return self._prompt.invoke(
                {
                    "question": question,
                    "safety_instructions": self.safety_instructions,
                }
            )

        system_message = (
            f"{self.safety_instructions}\n"
            "Respond in a warm, empathetic tone while staying factual and concise."
        )
        human_message = (
            "User question:\n"
            f"{question}\n\n"
            "Structure the response with a short introduction, practical guidance, and"
            " a concluding encouragement. Clearly tie the guidance back to sustaining"
            " long-term healthspan and longevity when it is relevant to do so."
        )

        return [
            {"role": "system", "content": system_message},
            {"role": "user", "content": human_message},
        ]


@dataclass(slots=True)
class LocalCoachResponder:
    """Deterministic fallback responder for offline development and testing."""

    disclaimer: str = _DEFAULT_DISCLAIMER

    def invoke(self, messages: Any) -> str:
        question = _extract_question_from_messages(messages)
        response = (
            "Offline coach response:\n"
            "- A production language model is unavailable.\n"
            "- Provide general educational guidance based on healthy lifestyle principles.\n"
            "- Highlight connections to long-term wellbeing and longevity whenever reasonable.\n\n"
            f"Question received: {question if question else 'No question provided.'}"
        )
        return response

    def __call__(self, messages: Any) -> str:  # pragma: no cover - convenience
        return self.invoke(messages)


class OllamaHTTPChat:
    """Minimal Ollama chat client used when LangChain is unavailable."""

    def __init__(self, model: str, *, base_url: str | None = None, timeout: float = 30.0) -> None:
        self.model = model
        self.base_url = (base_url or os.getenv("LIVEON_OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
        self.timeout = timeout

    def invoke(self, messages: Any) -> Any:
        payload = {
            "model": self.model,
            "messages": self._normalize_messages(messages),
            "stream": False,
        }
        response = httpx.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        # Align shape with LangChain response expectations
        if isinstance(data, dict):
            message = data.get("message")
            if isinstance(message, dict) and "content" in message:
                return message["content"]
            if "response" in data:
                return data["response"]
        return data

    def _normalize_messages(self, messages: Any) -> list[dict[str, str]]:
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]

        if hasattr(messages, "to_messages"):
            messages = messages.to_messages()  # type: ignore[assignment]

        normalized: list[dict[str, str]] = []
        if isinstance(messages, Sequence):
            for message in messages:
                role = getattr(message, "type", getattr(message, "role", "user"))
                content = getattr(message, "content", None)
                if isinstance(message, dict):
                    role = message.get("role") or message.get("type") or role
                    content = message.get("content", content)
                text = content if isinstance(content, str) else ""
                normalized.append({"role": str(role or "user"), "content": text})
        else:
            normalized.append({"role": "user", "content": str(messages)})

        return normalized


def create_coach_llm() -> Any:
    """Construct a chat client for the coach agent."""
    provider = (os.getenv("LIVEON_LLM_PROVIDER") or "ollama").strip().lower()

    if provider == "ollama":
        model = os.getenv("LIVEON_OLLAMA_MODEL") or 'phi3:14b-medium-4k-instruct-q4_K_M'
        if ChatOllama is not None:
            return ChatOllama(model=model)
        return OllamaHTTPChat(model=model)

    # Fallback for local dev and testing
    return LocalCoachResponder()


def _separate_disclaimer(text: str, *, default: str) -> tuple[str, str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return "", default

    marker = "disclaimer:"
    lower = cleaned.lower()
    if marker in lower:
        index = lower.rfind(marker)
        answer = cleaned[:index].strip()
        disclaimer_text = cleaned[index + len(marker) :].strip()
        return answer, disclaimer_text or default
    return cleaned, default


def _extract_question_from_messages(messages: Any) -> str:
    if isinstance(messages, str):
        return messages.strip()

    if hasattr(messages, "to_messages"):
        messages = messages.to_messages()  # type: ignore[assignment]

    if isinstance(messages, Sequence):
        for message in reversed(messages):
            if isinstance(message, dict):
                role = message.get("role") or message.get("type")
                content = message.get("content")
            else:
                role = getattr(message, "type", getattr(message, "role", ""))
                content = getattr(message, "content", "")

            if role in {"human", "user"}:
                if isinstance(content, str):
                    return content.strip()
                if isinstance(content, list):
                    return "".join(str(part) for part in content).strip()
    return ""
