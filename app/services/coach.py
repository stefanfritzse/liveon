"""Conversational coach agent that orchestrates retrieval augmented responses."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence
import os

from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import DefaultCredentialsError

from app.models.coach import CoachAnswer, CoachQuestion, CoachSource
from app.services.firestore import FirestoreContentRepository

try:  # pragma: no cover - optional dependency guard
    from langchain_core.prompts import ChatPromptTemplate
except ImportError:  # pragma: no cover - handled gracefully in CoachAgent
    ChatPromptTemplate = None  # type: ignore[assignment]


_DEFAULT_SAFETY_INSTRUCTIONS = (
    "You are LiveOn's Longevity Coach. Offer supportive, educational guidance based only on the"
    " research snippets you receive. Do not diagnose, prescribe, or promise outcomes, and always"
    " encourage the user to consult qualified healthcare professionals for personalised advice."
)

_DEFAULT_CITATION_INSTRUCTIONS = (
    "Reference the supplied context by citing the numbered sources using Markdown footnotes such as"
    " [^1]. If no supporting sources are available, say so explicitly."
)

_DEFAULT_DISCLAIMER = (
    "This conversation is for educational purposes only and is not a substitute for professional"
    " medical advice. Always consult a qualified healthcare provider about your personal health."
)


class CoachDataUnavailableError(RuntimeError):
    """Raised when the coach cannot access supporting Firestore content."""


@dataclass(slots=True)
class CoachAgent:
    """High level orchestration for answering user questions with retrieved content."""

    llm: Any
    repository: FirestoreContentRepository
    context_limit: int = 5
    safety_instructions: str = _DEFAULT_SAFETY_INSTRUCTIONS
    citation_instructions: str = _DEFAULT_CITATION_INSTRUCTIONS
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
                    "{safety_instructions}\n\n{citation_instructions}\n"
                    "Respond in a warm, empathetic tone while staying factual and concise.",
                ),
                (
                    "human",
                    "User question:\n{question}\n\nAvailable research snippets:\n{context}\n\n"
                    "Structure your reply with a short introduction, practical guidance, and a"
                    " concluding encouragement. End with a separate line starting with"
                    " 'Disclaimer:' summarising key safety points.",
                ),
            ]
        )

    def ask(self, question: CoachQuestion | str) -> CoachAnswer:
        """Answer ``question`` by consulting Firestore-backed context."""

        question_model = question if isinstance(question, CoachQuestion) else CoachQuestion(text=str(question))
        normalized_question = question_model.stripped()
        try:
            sources = self.repository.search_articles_for_question(
                normalized_question, limit=max(1, self.context_limit)
            )
        except (DefaultCredentialsError, GoogleAPIError) as exc:
            raise CoachDataUnavailableError("Failed to load supporting context for coach response") from exc
        context_text = self._format_context(sources)

        prompt_value = self._prompt.invoke(
            {
                "question": normalized_question,
                "context": context_text,
                "safety_instructions": self.safety_instructions,
                "citation_instructions": self.citation_instructions,
            }
        )

        response = self._invoke_llm(prompt_value)
        response_text = self._extract_response_text(response)
        message, disclaimer = _separate_disclaimer(response_text, default=_DEFAULT_DISCLAIMER)
        return CoachAnswer(message=message, disclaimer=disclaimer, sources=list(sources))

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

    def _format_context(self, sources: Sequence[CoachSource]) -> str:
        if not sources:
            return (
                "No articles matched the request. Provide general educational guidance and emphasise"
                " that specific medical questions require a clinician."
            )

        parts: list[str] = []
        for index, source in enumerate(sources, start=1):
            heading = source.title or "Untitled source"
            url = source.url or "Unavailable"
            snippet = source.snippet.strip()
            parts.append(
                f"[{index}] {heading}\nURL: {url}\nSnippet: {snippet}"
            )
        return "\n\n".join(parts)

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
            "- Review the context snippets above to craft a manual answer.\n\n"
            f"Question received: {question if question else 'No question provided.'}"
        )
        return f"{response}\n\nDisclaimer: {self.disclaimer}"

    def __call__(self, messages: Any) -> str:  # pragma: no cover - convenience
        return self.invoke(messages)


def create_coach_llm() -> Any:
    """Factory that selects an appropriate LLM backend for the coach agent."""

    choice = os.getenv("LIVEON_COACH_MODEL", "").strip().lower()
    temperature = _coerce_float(os.getenv("LIVEON_MODEL_TEMPERATURE"), default=0.2)
    max_tokens = _coerce_int(os.getenv("LIVEON_MODEL_MAX_OUTPUT_TOKENS"), default=1024)

    if choice.startswith("vertex"):
        try:  # pragma: no cover - optional dependency
            from langchain_google_vertexai import ChatVertexAI
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "langchain-google-vertexai must be installed to use Vertex AI models."
            ) from exc

        model_name = os.getenv("LIVEON_COACH_VERTEX_MODEL", os.getenv("LIVEON_VERTEX_MODEL", "chat-bison"))
        location = os.getenv("LIVEON_VERTEX_LOCATION")
        return ChatVertexAI(
            model_name=model_name,
            temperature=temperature,
            max_output_tokens=max_tokens,
            **({"location": location} if location else {}),
        )

    if choice.startswith("openai"):
        try:  # pragma: no cover - optional dependency
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("langchain-openai must be installed to use OpenAI models.") from exc

        model_name = os.getenv("LIVEON_COACH_OPENAI_MODEL", os.getenv("LIVEON_OPENAI_MODEL", "gpt-4o-mini"))
        return ChatOpenAI(model=model_name, temperature=temperature, max_tokens=max_tokens)

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


def _coerce_float(value: str | None, *, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: str | None, *, default: int) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default
