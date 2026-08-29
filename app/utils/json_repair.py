"""Coax a JSON object out of a language model response.

Small local models rarely honour "reply with only JSON" on the first attempt: they
wrap the object in a code fence, prepend a sentence of preamble, or emit Python's
``True``/``None`` instead of JSON's literals. Every agent in the pipeline needs the
same recovery ladder, so it lives here once rather than in four near-identical copies.

The ladder, in order of preference:

1. Parse the whole response as JSON.
2. Strip a surrounding ``` fence and parse that.
3. Scan for the first embedded ``{...}`` object.
4. Fall back to :func:`ast.literal_eval` for Python-flavoured dicts.

When all of that fails, :func:`invoke_json_object` re-asks the model with the parse
error attached, which recovers most of the remainder.
"""

from __future__ import annotations

import ast
import json
import logging
from typing import Any, Protocol, Sequence

from app.utils.langchain_compat import AIMessage, BaseMessage, HumanMessage

__all__ = [
    "JsonParseError",
    "extract_message_text",
    "invoke_json_object",
    "parse_json_object",
]

LOGGER = logging.getLogger(__name__)

#: How much of an unparseable reply to quote back to the model when re-asking.
_ECHO_LIMIT = 400


class JsonParseError(ValueError):
    """Raised when a model response cannot be read as a JSON object."""


class SupportsInvoke(Protocol):
    """The subset of the LangChain chat interface the pipeline relies on."""

    def invoke(self, input: Any, **kwargs: Any) -> BaseMessage | str:  # pragma: no cover
        ...


def extract_message_text(response: BaseMessage | str | Any) -> str:
    """Return the text content of a model response, whatever shape it arrives in."""

    if response is None:
        return ""
    if isinstance(response, str):
        return response

    if isinstance(response, dict):
        content = response.get("content")
    else:
        content = getattr(response, "content", None)

    if content is None:
        # A message object with no content is empty, not its repr.
        if isinstance(response, (AIMessage, BaseMessage)) or isinstance(response, dict):
            return ""
        return str(response)

    if isinstance(content, list):
        # Chat models may return content as a list of blocks.
        return "".join(
            part if isinstance(part, str) else str(part.get("text", part))
            if isinstance(part, dict)
            else str(part)
            for part in content
        )
    return str(content)


def parse_json_object(content: str, *, label: str = "Model") -> dict[str, Any]:
    """Return the JSON object encoded in ``content``.

    :raises JsonParseError: when no object can be recovered.
    """

    text = (content or "").strip()
    if not text:
        raise JsonParseError(f"{label} returned an empty response.")

    candidates: list[str] = []
    fenced = _strip_code_fence(text)
    if fenced:
        candidates.append(fenced)
    candidates.append(text)

    for candidate in candidates:
        parsed = _try_parse_mapping(candidate)
        if parsed is not None:
            return parsed

    scanned = _scan_for_object(text)
    if scanned is not None:
        return scanned

    raise JsonParseError(f"{label} response was not valid JSON.")


def invoke_json_object(
    llm: SupportsInvoke,
    messages: Any,
    *,
    label: str = "Model",
    retries: int = 1,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Invoke ``llm`` and parse its reply as a JSON object, re-asking on failure.

    One malformed reply used to discard a whole pipeline run along with the feed
    fetching and summarisation that preceded it. Re-asking with the parse error
    attached costs one extra call and recovers most of those runs.
    """

    log = logger or LOGGER
    attempts = max(1, retries + 1)
    conversation = list(messages) if isinstance(messages, Sequence) and not isinstance(messages, str) else messages
    last_error: JsonParseError | None = None

    for attempt in range(1, attempts + 1):
        response = llm.invoke(conversation)
        content = extract_message_text(response)
        try:
            return parse_json_object(content, label=label)
        except JsonParseError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            log.warning(
                "%s returned unparseable JSON on attempt %s/%s; re-asking",
                label,
                attempt,
                attempts,
                extra={"event": "agent.json_retry", "agent": label, "attempt": attempt},
            )
            conversation = _append_correction(conversation, content, exc)

    raise last_error or JsonParseError(f"{label} response was not valid JSON.")


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _append_correction(conversation: Any, content: str, error: JsonParseError) -> list[Any]:
    """Return ``conversation`` plus a corrective instruction for the model."""

    echo = (content or "").strip()[:_ECHO_LIMIT]
    correction = (
        f"That reply could not be parsed as JSON ({error}). "
        "Reply again with ONLY the JSON object described above — no prose, no "
        "explanation, and no code fence."
    )
    if echo:
        correction += f"\n\nYour previous reply began:\n{echo}"

    base = list(conversation) if isinstance(conversation, (list, tuple)) else [conversation]
    return [*base, HumanMessage(content=correction)]


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


def _scan_for_object(text: str) -> dict[str, Any] | None:
    """Return the first embedded JSON object, ignoring surrounding prose."""

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


def _try_parse_mapping(candidate: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        return payload

    # Some models answer with a Python dict literal (True/None instead of true/null).
    try:
        parsed = ast.literal_eval(candidate)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None

    return parsed if isinstance(parsed, dict) else None
