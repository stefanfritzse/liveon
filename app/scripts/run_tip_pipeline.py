"""Execute the longevity tip pipeline and publish the resulting content."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence
from urllib.parse import urlparse

from app.services.pipeline import TipPipeline
from app.services.tip_generator import TipGenerator
from app.services.tip_editor import TipEditorAgent
from app.services.tip_context import DailyTipContextProvider
from app.services.tip_publisher import TipPublisher
from app.services.sqlite_repo import LocalSQLiteContentRepository
from app.utils.langchain_compat import AIMessage, BaseMessage

LOGGER = logging.getLogger("liveon.tip_pipeline")

if not LOGGER.handlers:  # avoid duplicates on re-import
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


class SupportsInvoke(Protocol):
    """Protocol implemented by LangChain compatible chat models."""

    def invoke(self, input: Any, **kwargs: Any) -> BaseMessage | str:  # pragma: no cover - interface
        """Invoke the underlying model."""


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


def _configure_logging() -> None:
    """Configure root logging based on ``LIVEON_LOG_LEVEL``."""
    level_name = os.getenv("LIVEON_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def _env_bool(variable: str, default: bool = False) -> bool:
    value = os.getenv(variable)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _default_model_provider() -> str:
    raw = os.getenv("LIVEON_TIP_MODEL") or os.getenv("LIVEON_SUMMARIZER_MODEL") or "local"
    return raw.lower()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute the Live On tip pipeline")
    parser.add_argument(
        "--model-provider",
        choices=["local", "openai", "gpt"],
        default=_default_model_provider(),
        help="Language model backend for the tip generator",
    )
    parser.add_argument(
        "--model",
        dest="model_name",
        default=os.getenv("LIVEON_TIP_MODEL_NAME"),
        help="Optional model identifier when using Vertex AI or OpenAI",
    )
    parser.add_argument(
        "--allow-local-llm",
        action="store_true",
        default=_env_bool("LIVEON_ALLOW_LOCAL_LLM"),
        help="Permit the deterministic local stub even in managed environments",
    )
    parser.add_argument(
        "--published-at",
        default=os.getenv("LIVEON_TIP_PUBLISHED_AT"),
        help="ISO-8601 timestamp to override the publication time for the stored tip",
    )
    return parser.parse_args(argv)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:  # pragma: no cover - user configuration
        raise SystemExit(f"Invalid ISO-8601 timestamp: {value}") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed


def _create_tip_llm(provider: str, *, model_name: str | None, allow_local_stub: bool) -> SupportsInvoke:
    provider_key = provider.lower()
    temperature = float(os.getenv("LIVEON_MODEL_TEMPERATURE", "0.2"))

    if provider_key == "ollama":
        from langchain_community.chat_models import ChatOllama
        model = (
            os.getenv("LIVEON_TIP_OLLAMA_MODEL")
            or os.getenv("LIVEON_OLLAMA_MODEL")
            or 'phi3:14b-medium-4k-instruct-q4_K_M'
        )
        base_url = _resolve_ollama_base_url()
        return ChatOllama(model=model, base_url=base_url)

    if provider_key in {"openai", "gpt"}:  # pragma: no cover - optional dependency
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise SystemExit("Install langchain-openai to use the OpenAI chat model") from exc

        model_id = model_name or os.getenv("LIVEON_TIP_OPENAI_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
        return ChatOpenAI(model=model_id, temperature=temperature)

    if provider_key != "local":
        raise SystemExit(f"Unsupported model provider: {provider}")

    return TipLocalJSONResponder()


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


class TipLocalJSONResponder:
    """Deterministic responder that fabricates tip JSON payloads for testing."""

    def invoke(self, input: Any, **kwargs: Any) -> AIMessage:
        if isinstance(input, list) and input:
            final_message = input[-1]
            content = getattr(final_message, "content", str(final_message))
        else:
            content = str(input)

        normalized_content = content.lower()
        if "is_approved" in normalized_content or "rubric" in normalized_content:
            # Mimic editor feedback so the pipeline can complete locally.
            payload = {"is_approved": True, "feedback": "Looks good.", "revised_draft": None}
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))

        payload = self._build_payload(content)
        return AIMessage(content=json.dumps(payload, default=_json_default, ensure_ascii=False))

    @staticmethod
    def _build_payload(prompt: str) -> dict[str, Any]:
        notes = TipLocalJSONResponder._extract_block(prompt, "Research notes:", "Key sources:")
        sources = TipLocalJSONResponder._extract_block(prompt, "Key sources:", "Current date:")

        def _clean_line(raw: str) -> str:
            text = html.unescape(raw)
            text = re.sub(r"<[^>]+>", "", text)
            return " ".join(text.replace("**", "").split())

        cleaned = [_clean_line(note) for note in notes if note.strip()]
        focus = cleaned[0] if cleaned else "Healthy habit"
        parts = re.split(r"\s[-\u2014:]\s", focus, maxsplit=1)
        subject = parts[0].strip(' "\'') or "Healthy habit"
        detail = parts[1].strip() if len(parts) > 1 else "make this part of your day"

        title = subject[:60] or "Daily Longevity Tip"
        if len(subject) > 60:
            title = title.rstrip() + "..."

        body_sentences = [
            f"{subject} supports long-term vitality.",
            f"Today, focus on {detail.lower()} to put it into practice.",
        ]
        if len(cleaned) > 1:
            extra_subject = cleaned[1].split(" - ")[0].strip(' "\'')
            if extra_subject and extra_subject.lower() != subject.lower():
                body_sentences.append(f"Pair it with {extra_subject.lower()} for an extra boost.")

        body = " ".join(sentence.strip() for sentence in body_sentences if sentence.strip())

        primary_tag = subject.split()[0].lower() if subject.split() else "habit"
        tags = ["longevity", primary_tag]
        metadata = {"sources": sources[:2], "confidence": "medium"}

        return {
            "title": title,
            "body": body,
            "tags": tags,
            "metadata": metadata,
        }

    @staticmethod
    def _extract_block(prompt: str, start_marker: str, end_marker: str | None) -> list[str]:
        block = ""
        if start_marker in prompt:
            block = prompt.split(start_marker, 1)[1]
            if end_marker and end_marker in block:
                block = block.split(end_marker, 1)[0]
        return [line.strip(" -") for line in block.splitlines() if line.strip()]


def _build_pipeline(llm: SupportsInvoke) -> TipPipeline:
    context_provider = DailyTipContextProvider()
    generator = TipGenerator(llm=llm)
    repository = LocalSQLiteContentRepository()
    publisher = TipPublisher(repository)
    editor = TipEditorAgent(llm=llm)

    return TipPipeline(
        context_provider=context_provider,
        generator=generator,
        editor=editor,
        publisher=publisher,
        repository=repository,
    )


def main(argv: Sequence[str] | None = None) -> int:
    _configure_logging()
    args = _parse_args(argv)
    LOGGER.info("TIP_PIPELINE_START provider=%s", args.model_provider)

    try:
        llm = _create_tip_llm(
            args.model_provider,
            model_name=args.model_name,
            allow_local_stub=args.allow_local_llm,
        )
    except Exception:
        LOGGER.exception("Failed to initialise language model for tip generator")
        return 1

    pipeline = _build_pipeline(llm)
    published_at = _parse_datetime(args.published_at)

    try:
        result = pipeline.run(published_at=published_at)
    except Exception:  # pragma: no cover - defensive fallback
        LOGGER.exception("Tip pipeline encountered an unexpected error")
        return 1

    for warning in result.warnings:
        LOGGER.warning("TIP_PIPELINE_WARNING %s", warning)
    for error in result.errors:
        LOGGER.error("TIP_PIPELINE_ERROR %s", error)

    payload = {
        "context": asdict(result.context) if result.context else None,
        "draft": asdict(result.draft) if result.draft else None,
        "tip": asdict(result.tip) if result.tip else None,
        "publication": asdict(result.publication) if result.publication else None,
        "warnings": result.warnings,
        "errors": result.errors,
        "succeeded": result.succeeded,
        "created": result.created,
        "generation_attempts": getattr(result, "generation_attempts", 1),
        "editor_feedback": getattr(result, "editor_feedback", []),
    }
    serialized = json.dumps(payload, default=_json_default, ensure_ascii=False)
    LOGGER.debug("TIP_PIPELINE_RESULT %s", serialized)
    print(serialized)

    if not result.succeeded:
        LOGGER.error("Tip pipeline failed to produce a tip")
        return 1

    LOGGER.info(
        "TIP_PIPELINE_COMPLETE created=%s title=%s", result.created, result.tip.title if result.tip else None
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
