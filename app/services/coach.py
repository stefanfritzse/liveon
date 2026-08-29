"""Conversational coach agent that generates responses using Ollama."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence
import logging
import os
from urllib.parse import urlparse

import httpx

from app.models.coach import CoachAnswer, CoachQuestion

LOGGER = logging.getLogger(__name__)

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

# A 14B model answering on CPU routinely needs well over a minute. The ceiling exists to
# stop a wedged daemon from pinning a worker forever, not to cut generation short.
_DEFAULT_LLM_TIMEOUT = 180.0


class CoachError(RuntimeError):
    """Base class for coach failures that the API layer knows how to translate.

    Inherits from :class:`RuntimeError` so existing callers that only guard against
    ``RuntimeError`` keep working.
    """


class CoachUnavailableError(CoachError):
    """The language model could not be reached (daemon down, wrong host, DNS)."""


class CoachTimeoutError(CoachError):
    """The language model accepted the request but did not answer in time."""


def resolve_llm_timeout() -> float:
    """Return the per-request language model timeout in seconds."""

    raw = (os.getenv("LIVEON_LLM_TIMEOUT") or "").strip()
    if not raw:
        return _DEFAULT_LLM_TIMEOUT
    try:
        timeout = float(raw)
    except ValueError:
        return _DEFAULT_LLM_TIMEOUT
    return timeout if timeout > 0 else _DEFAULT_LLM_TIMEOUT


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Return ``exc`` plus its ``__cause__``/``__context__`` ancestry."""

    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def classify_llm_error(exc: Exception) -> CoachError:
    """Translate an arbitrary model-client exception into a coach-specific error.

    The coach can sit behind ``langchain-community`` (which uses ``requests``), the
    built-in ``httpx`` client, or whatever a future provider brings. Rather than
    enumerating every library's exception hierarchy, classify on the exception chain
    using the shared naming conventions those libraries follow.
    """

    if isinstance(exc, CoachError):
        return exc

    chain = _exception_chain(exc)
    names = [type(item).__name__.lower() for item in chain]

    # Order matters: a connect *timeout* is more usefully reported as unreachable.
    if any("connect" in name or "unreachable" in name for name in names):
        return CoachUnavailableError(str(exc) or "Could not reach the language model.")

    if any(isinstance(item, (TimeoutError, httpx.TimeoutException)) for item in chain):
        return CoachTimeoutError(str(exc) or "The language model timed out.")

    if any("timeout" in name or "timedout" in name for name in names):
        return CoachTimeoutError(str(exc) or "The language model timed out.")

    if any(isinstance(item, (ConnectionError, httpx.TransportError)) for item in chain):
        return CoachUnavailableError(str(exc) or "Could not reach the language model.")

    return CoachError(str(exc) or "The language model failed to answer.")


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

        try:
            response = self._invoke_llm(prompt_value)
        except Exception as exc:  # noqa: BLE001 - re-raised as a classified CoachError
            raise classify_llm_error(exc) from exc

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

    def __init__(self, model: str, *, base_url: str | None = None, timeout: float | None = None) -> None:
        self.model = model
        resolved_base_url = base_url or _resolve_ollama_base_url()
        self.base_url = resolved_base_url.rstrip("/")
        self.timeout = resolve_llm_timeout() if timeout is None else timeout

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


def _resolve_ollama_base_url() -> str:
    """Return a client-safe Ollama base URL, defaulting to localhost."""

    raw = (os.getenv("LIVEON_OLLAMA_URL") or os.getenv("OLLAMA_HOST") or "").strip()
    if not raw:
        raw = "http://127.0.0.1:11434"

    if "://" not in raw:
        raw = f"http://{raw}"

    parsed = urlparse(raw)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"

    if host in {"0.0.0.0", "::", "", "[::]"}:
        host = "127.0.0.1"

    port = parsed.port or 11434
    return f"{scheme}://{host}:{port}"


def create_coach_llm() -> Any:
    """Construct a chat client for the coach agent."""
    provider = (os.getenv("LIVEON_LLM_PROVIDER") or "ollama").strip().lower()

    if provider == "ollama":
        model = os.getenv("LIVEON_OLLAMA_MODEL") or 'phi3:14b-medium-4k-instruct-q4_K_M'
        base_url = _resolve_ollama_base_url()
        timeout = resolve_llm_timeout()
        if ChatOllama is not None:
            try:
                return ChatOllama(model=model, base_url=base_url, timeout=int(timeout))
            except (TypeError, ValueError) as exc:
                # Not every ChatOllama build exposes ``timeout`` (langchain-ollama takes it
                # via client options). Losing the ceiling is worth a warning, not a crash.
                LOGGER.warning(
                    "ChatOllama rejected the timeout option; running without one: %s",
                    exc,
                    extra={"event": "coach.timeout_unsupported"},
                )
                return ChatOllama(model=model, base_url=base_url)
        return OllamaHTTPChat(model=model, base_url=base_url, timeout=timeout)

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
