"""Conversational coach agent that generates responses using Ollama."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Sequence
import json
import logging
import os
import re

import httpx

from app.models.coach import CoachAnswer, CoachQuestion, CoachTurn
from app.services.llm_factory import (
    DEFAULT_OLLAMA_MODEL,
    build_chat_ollama,
    resolve_ollama_base_url,
)

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

# Every answer is health guidance, so the safety note travels with it rather than
# depending on the model happening to emit one or on the reader noticing the footer.
_DEFAULT_DISCLAIMER = (
    "Live On shares general educational information, not medical advice. Check with a"
    " qualified healthcare professional before changing your health routine, especially"
    " if you have a medical condition or take medication."
)

# A 14B model answering on CPU routinely needs well over a minute. The ceiling exists to
# stop a wedged daemon from pinning a worker forever, not to cut generation short.
_DEFAULT_LLM_TIMEOUT = 180.0

# History is replayed into every prompt, so it is bounded twice over: by turn count and
# by characters. A local model has a modest context window, and the client supplies the
# transcript, so neither bound may be left to the caller.
_DEFAULT_HISTORY_TURNS = 6
_MAX_HISTORY_CHARS = 4000
_MAX_TURN_CHARS = 2000


def resolve_history_turns() -> int:
    """Return how many earlier turns are replayed into the prompt."""

    raw = (os.getenv("LIVEON_COACH_HISTORY_TURNS") or "").strip()
    if not raw:
        return _DEFAULT_HISTORY_TURNS
    try:
        turns = int(raw)
    except ValueError:
        return _DEFAULT_HISTORY_TURNS
    return max(0, turns)


def trim_history(history: Sequence[CoachTurn]) -> list[CoachTurn]:
    """Return the most recent turns that fit within the configured budgets.

    Turns are taken newest-first so the freshest context survives, then restored to
    chronological order for the prompt.
    """

    limit = resolve_history_turns()
    if limit <= 0:
        return []

    kept: list[CoachTurn] = []
    budget = _MAX_HISTORY_CHARS
    for turn in reversed(list(history)):
        text = turn.stripped()
        if not text:
            continue
        if len(text) > _MAX_TURN_CHARS:
            text = text[:_MAX_TURN_CHARS].rstrip() + "…"
        if len(text) > budget:
            break
        budget -= len(text)
        kept.append(CoachTurn(role=turn.role, text=text))
        if len(kept) >= limit:
            break

    kept.reverse()
    return kept


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

    def ask(self, question: CoachQuestion | str) -> CoachAnswer:
        """Answer ``question`` using the configured language model."""

        question_model = question if isinstance(question, CoachQuestion) else CoachQuestion(text=str(question))
        normalized_question = question_model.stripped()

        prompt_value = self._build_prompt(normalized_question, question_model.history)

        try:
            response = self._invoke_llm(prompt_value)
        except Exception as exc:  # noqa: BLE001 - re-raised as a classified CoachError
            raise classify_llm_error(exc) from exc

        response_text = self._extract_response_text(response)
        message, disclaimer = separate_disclaimer(response_text, default=self.default_disclaimer)
        return CoachAnswer(message=message, disclaimer=disclaimer)

    def stream(self, question: CoachQuestion | str) -> "Iterator[str]":
        """Yield answer fragments as the model produces them.

        Falls back to a single fragment when the underlying client cannot stream, so
        callers can treat streaming as always available.
        """

        question_model = question if isinstance(question, CoachQuestion) else CoachQuestion(text=str(question))
        prompt_value = self._build_prompt(question_model.stripped(), question_model.history)

        try:
            if hasattr(self.llm, "stream"):
                messages = self._as_messages(prompt_value)
                for chunk in self.llm.stream(messages):
                    text = self._extract_response_text(chunk)
                    if text:
                        yield text
                return
            response = self._invoke_llm(prompt_value)
        except Exception as exc:  # noqa: BLE001 - re-raised as a classified CoachError
            raise classify_llm_error(exc) from exc

        text = self._extract_response_text(response)
        if text:
            yield text

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _as_messages(prompt_value: Any) -> Any:
        """Return the message sequence for ``prompt_value``."""

        if hasattr(prompt_value, "to_messages"):
            return prompt_value.to_messages()
        return prompt_value

    def _invoke_llm(self, prompt_value: Any) -> Any:
        messages = self._as_messages(prompt_value)

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

    def _build_prompt(
        self,
        question: str,
        history: Sequence[CoachTurn] = (),
    ) -> Any:
        """Create a prompt payload regardless of LangChain availability.

        Messages are plain ``{"role", "content"}`` mappings. LangChain converts those
        natively, and :class:`OllamaHTTPChat` translates the roles at the wire boundary,
        so one representation covers both clients — and, unlike a fixed
        ``ChatPromptTemplate``, it can carry a variable number of prior turns.
        """

        messages: list[dict[str, str]] = [{"role": "system", "content": self._system_message()}]

        for turn in trim_history(history):
            text = turn.stripped()
            if text:
                messages.append({"role": "human" if turn.is_user else "ai", "content": text})

        messages.append({"role": "human", "content": self._human_message(question, bool(history))})
        return messages

    def _system_message(self) -> str:
        return (
            f"{self.safety_instructions}\n"
            "Respond in a warm, empathetic tone while staying factual and concise."
        )

    @staticmethod
    def _human_message(question: str, has_history: bool) -> str:
        guidance = (
            "Structure the response with a short introduction, practical guidance, and"
            " a concluding encouragement. Clearly tie the guidance back to sustaining"
            " long-term healthspan and longevity when it is relevant to do so."
        )
        if has_history:
            guidance = (
                "This continues the conversation above, so resolve any references to"
                " what was already discussed and do not repeat advice you have already"
                f" given. {guidance}"
            )
        return f"User question:\n{question}\n\n{guidance}"


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
        resolved_base_url = base_url or resolve_ollama_base_url()
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

    def stream(self, messages: Any) -> Iterator[str]:
        """Yield content fragments from Ollama's newline-delimited JSON stream."""

        payload = {
            "model": self.model,
            "messages": self._normalize_messages(messages),
            "stream": True,
        }
        with httpx.stream(
            "POST",
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=self.timeout,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:  # pragma: no cover - defensive against partial lines
                    continue
                if not isinstance(data, dict):
                    continue
                message = data.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content:
                        yield content
                if data.get("done"):
                    break

    # Ollama's chat API speaks system/user/assistant, while LangChain messages carry
    # human/ai. Translate at the wire boundary so one internal representation serves
    # both clients.
    _WIRE_ROLES = {"human": "user", "ai": "assistant", "assistant": "assistant",
                   "user": "user", "system": "system"}

    @classmethod
    def _wire_role(cls, role: Any) -> str:
        return cls._WIRE_ROLES.get(str(role or "user").lower(), "user")

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
                normalized.append({"role": self._wire_role(role), "content": text})
        else:
            normalized.append({"role": "user", "content": str(messages)})

        return normalized


def create_coach_llm() -> Any:
    """Construct a chat client for the coach agent."""
    provider = (os.getenv("LIVEON_LLM_PROVIDER") or "ollama").strip().lower()

    if provider == "ollama":
        model = os.getenv("LIVEON_OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL
        base_url = resolve_ollama_base_url()
        timeout = resolve_llm_timeout()
        if ChatOllama is not None:
            # Conversational replies are prose, so no JSON mode here.
            return build_chat_ollama(
                model=model, base_url=base_url, json_mode=False, timeout=timeout
            )
        return OllamaHTTPChat(model=model, base_url=base_url, timeout=timeout)

    # Fallback for local dev and testing
    return LocalCoachResponder()


# A trailing disclaimer starts its own line, optionally dressed up as a bullet or in
# bold. Matching mid-sentence mentions ("read the label disclaimer: ...") used to eat
# the rest of the answer, so the marker must anchor to the start of a line.
# Emphasis may close before or after the colon: "**Disclaimer:**" and "**Disclaimer**:"
# are both common, so an optional marker is allowed on either side.
_EMPHASIS = r"(?:\*\*|__|\*|_)?"
_DISCLAIMER_LINE_RE = re.compile(
    rf"^[ \t]*(?:[-*>]\s+)?{_EMPHASIS}\s*disclaimer\s*{_EMPHASIS}\s*[:\-–—]\s*{_EMPHASIS}\s*",
    re.IGNORECASE | re.MULTILINE,
)

def separate_disclaimer(text: str, *, default: str) -> tuple[str, str]:
    """Split a trailing ``Disclaimer:`` note off the end of ``text``.

    Two conditions must hold: the marker starts its own line, and it opens the final
    block of the response (nothing after it is separated by a blank line). A mid-answer
    mention — "check the supplement label disclaimer: ..." — therefore keeps its text,
    where the previous ``rfind`` on the bare word silently discarded everything after it.
    """

    cleaned = (text or "").strip()
    if not cleaned:
        return "", default

    for match in reversed(list(_DISCLAIMER_LINE_RE.finditer(cleaned))):
        remainder = cleaned[match.end() :]
        if "\n\n" in remainder.strip():
            # More paragraphs follow, so this is body text rather than a closing note.
            continue
        answer = cleaned[: match.start()].strip()
        if not answer:
            # The whole response was the disclaimer; keep it as the answer.
            break
        return answer, remainder.strip() or default

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
