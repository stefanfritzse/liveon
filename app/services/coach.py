"""Conversational coach agent that generates responses using Ollama."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence
import os

from app.models.coach import CoachAnswer, CoachQuestion

try:  # pragma: no cover - optional dependency guard
    from langchain_community.chat_models import ChatOllama
    from langchain_core.prompts import ChatPromptTemplate
except ImportError:  # pragma: no cover - handled gracefully in CoachAgent
    ChatPromptTemplate = None  # type: ignore[assignment]


_DEFAULT_SAFETY_INSTRUCTIONS = (
    "You are LiveOn's Longevity Coach. Offer supportive, educational guidance grounded in"
    " general best practices. Do not diagnose, prescribe, or promise outcomes, and always"
    " encourage the user to consult qualified healthcare professionals for personalised advice."
    " Whenever it is plausible, frame insights through the lens of healthy ageing and human"
    " longevity so the user understands the long-term wellbeing impact of each suggestion."
)

_DEFAULT_DISCLAIMER = (
    "This conversation is for educational purposes only and is not a substitute for professional"
    " medical advice. Always consult a qualified healthcare provider about your personal health."
)


@dataclass(slots=True)
class CoachAgent:
    """High level orchestration for answering user questions with Ollama."""

    llm: Any
    safety_instructions: str = _DEFAULT_SAFETY_INSTRUCTIONS
    default_disclaimer: str = _DEFAULT_DISCLAIMER
    _prompt: ChatPromptTemplate = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if ChatPromptTemplate is None:  # pragma: no cover - depends on optional dependency
            raise RuntimeError(
                "LangChain is required to create a CoachAgent; install langchain-core to proceed."
            )
        self._prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "{safety_instructions}\n"
                    "Respond in a warm, empathetic tone while staying factual and concise."
                    " Always finish with a separate line that begins with 'Disclaimer:'"
                    " followed by the provided disclaimer text verbatim: {default_disclaimer}",
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

        prompt_value = self._prompt.invoke(
            {
                "question": normalized_question,
                "safety_instructions": self.safety_instructions,
                "default_disclaimer": self.default_disclaimer,
            }
        )

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
        return f"{response}\n\nDisclaimer: {self.disclaimer}"

    def __call__(self, messages: Any) -> str:  # pragma: no cover - convenience
        return self.invoke(messages)


def create_coach_llm() -> Any:
    """Construct a chat client for the coach agent."""
    provider = (os.getenv("LIVEON_LLM_PROVIDER") or "ollama").strip().lower()

    if provider == "ollama":
        return ChatOllama(model='phi3:14b-medium-4k-instruct-q4_K_M')

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
