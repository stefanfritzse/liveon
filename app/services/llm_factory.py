"""One place where chat models are constructed for every agent.

The article pipeline, the tip pipeline, and the coach each used to grow their own
copy of "work out the provider, resolve the Ollama URL, pick a model name". The
copies drifted: only the article path set ``format="json"`` and a low temperature,
while the tip path — whose agents demand strict JSON — ran at Ollama's default
temperature of 0.8 through the deprecated ``langchain_community`` import.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)

#: Providers ``create_chat_model`` understands. ``local`` returns a caller-supplied
#: deterministic stub, used for offline development and tests.
SUPPORTED_PROVIDERS = ("ollama", "openai", "local")

DEFAULT_OLLAMA_MODEL = "phi3:14b-medium-4k-instruct-q4_K_M"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_TEMPERATURE = 0.2

_PROVIDER_ALIASES = {
    "gpt": "openai",
    "openai": "openai",
    "ollama": "ollama",
    "local": "local",
    "stub": "local",
}


def resolve_ollama_base_url() -> str:
    """Return a client-safe Ollama base URL, defaulting to localhost.

    ``0.0.0.0`` is a bind address, not a destination: a daemon listening on it is
    reached at ``127.0.0.1``. Rewriting it here keeps a common misconfiguration from
    surfacing as a confusing connection error.
    """

    raw = (os.getenv("LIVEON_OLLAMA_URL") or os.getenv("OLLAMA_HOST") or "").strip()
    if not raw:
        raw = DEFAULT_OLLAMA_URL

    if "://" not in raw:
        raw = f"http://{raw}"

    parsed = urlparse(raw)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"

    if host in {"0.0.0.0", "::", "", "[::]"}:
        host = "127.0.0.1"

    port = parsed.port or 11434
    return f"{scheme}://{host}:{port}"


def resolve_model_temperature(default: float = DEFAULT_TEMPERATURE) -> float:
    """Sampling temperature for every provider; falls back on an unusable value.

    Ollama defaults to 0.8, which invents dates and URLs on summarisation tasks and
    breaks strict-JSON adherence, so agents run cooler unless told otherwise.
    """

    raw = (os.getenv("LIVEON_MODEL_TEMPERATURE") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        LOGGER.warning("Ignoring invalid LIVEON_MODEL_TEMPERATURE=%r", raw)
        return default


def normalise_provider(raw: str | None, *, default: str = "ollama") -> str:
    """Map a configured provider name onto a supported one."""

    cleaned = (raw or "").strip().lower()
    if not cleaned:
        return default
    resolved = _PROVIDER_ALIASES.get(cleaned)
    if resolved is None:
        LOGGER.warning(
            "Unknown model provider %r; falling back to %r", cleaned, default
        )
        return default
    return resolved


def resolve_chat_ollama_class() -> Any:
    """Return the best available ``ChatOllama`` implementation.

    ``langchain-ollama`` is the maintained package; ``langchain-community`` carries a
    deprecated copy that still works and keeps the app runnable without the newer
    dependency installed.
    """

    try:
        from langchain_ollama import ChatOllama  # type: ignore

        return ChatOllama
    except ImportError:
        pass

    try:
        from langchain_community.chat_models import ChatOllama  # type: ignore

        return ChatOllama
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "Install langchain-ollama (or langchain-community) to use the Ollama provider."
        ) from exc


def build_chat_ollama(
    *,
    model: str,
    base_url: str | None = None,
    temperature: float | None = None,
    json_mode: bool = False,
    timeout: float | None = None,
) -> Any:
    """Instantiate ``ChatOllama``, degrading gracefully on unsupported options.

    Option support differs between the ``langchain-ollama`` and ``langchain-community``
    builds, so unsupported keywords are dropped one at a time rather than allowed to
    break start-up.
    """

    chat_ollama = resolve_chat_ollama_class()
    kwargs: dict[str, Any] = {
        "model": model,
        "base_url": base_url or resolve_ollama_base_url(),
        "temperature": resolve_model_temperature() if temperature is None else temperature,
    }
    if json_mode:
        kwargs["format"] = (os.getenv("LIVEON_OLLAMA_FORMAT") or "json").strip().lower()
    if timeout is not None:
        kwargs["timeout"] = int(timeout)

    # These clients are pydantic models that accept unknown keywords silently, so an
    # unsupported option would look applied while doing nothing. Drop what the class
    # does not declare, and say so — for `timeout` in particular, the caller should
    # know the request deadline is then the only thing bounding a call.
    declared = getattr(chat_ollama, "model_fields", None)
    if isinstance(declared, dict):
        for key in [key for key in kwargs if key not in declared and key != "model"]:
            kwargs.pop(key)
            LOGGER.info(
                "%s does not support %r; relying on the caller's deadline instead.",
                getattr(chat_ollama, "__name__", "ChatOllama"),
                key,
                extra={"event": "llm.option_unavailable", "option": key},
            )

    optional_keys = ["timeout", "format", "temperature"]
    while True:
        try:
            return chat_ollama(**kwargs)
        except (TypeError, ValueError) as exc:
            dropped = next((key for key in optional_keys if key in kwargs), None)
            if dropped is None:
                raise
            optional_keys.remove(dropped)
            kwargs.pop(dropped, None)
            LOGGER.warning(
                "ChatOllama rejected %r; retrying without it: %s",
                dropped,
                exc,
                extra={"event": "llm.option_unsupported", "option": dropped},
            )


def create_chat_model(
    *,
    provider: str | None = None,
    agent_label: str = "agent",
    model_name: str | None = None,
    json_mode: bool = True,
    local_factory: Callable[[], Any] | None = None,
) -> Any:
    """Build the chat model for ``agent_label``.

    ``provider`` overrides configuration; when omitted it is read from the
    agent-specific environment variable, then the shared one.
    """

    resolved = normalise_provider(
        provider
        or os.getenv(f"LIVEON_{agent_label.upper()}_MODEL")
        or os.getenv("LIVEON_LLM_PROVIDER")
    )

    if resolved == "ollama":
        model = (
            model_name
            or os.getenv(f"LIVEON_{agent_label.upper()}_OLLAMA_MODEL")
            or os.getenv("LIVEON_OLLAMA_MODEL")
            or DEFAULT_OLLAMA_MODEL
        )
        return build_chat_ollama(model=model, json_mode=json_mode)

    if resolved == "openai":  # pragma: no cover - optional dependency
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise RuntimeError(
                "Install langchain-openai to use the OpenAI chat model."
            ) from exc

        model = (
            model_name
            or os.getenv(f"LIVEON_{agent_label.upper()}_OPENAI_MODEL")
            or os.getenv("OPENAI_MODEL")
            or DEFAULT_OPENAI_MODEL
        )
        return ChatOpenAI(model=model, temperature=resolve_model_temperature())

    if local_factory is None:
        raise RuntimeError(
            f"No local stub is available for {agent_label!r}; choose the 'ollama' or "
            "'openai' provider instead."
        )
    return local_factory()
