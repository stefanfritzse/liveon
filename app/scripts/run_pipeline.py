"""Execute the multi-agent content pipeline and publish results to the selected storage.

This utility composes the aggregator, summariser, editor, and publisher agents
so that the pipeline can be run manually or on a schedule (for example via a
Kubernetes CronJob). When optional LangChain integrations for Vertex AI or
OpenAI are available the script will prefer those chat models. For local
development it falls back to a deterministic JSON responder, allowing the
pipeline to be exercised without external LLM access.

Storage selection:
- Default to SQLite for local development (no GCP required).
- Switch via --storage sqlite or LIVEON_STORAGE.
- For SQLite, you can set --db-path PATH or LIVEON_DB_PATH.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Protocol, Sequence

from app.utils.langchain_compat import AIMessage, BaseMessage

from app.models.aggregator import FeedSource
from app.services.aggregator import LongevityNewsAggregator
from app.services.editor import EditorAgent
from app.services.pipeline import ContentPipeline
from app.services.summarizer import SummarizerAgent
from dataclasses import is_dataclass, asdict
from datetime import datetime, date, timezone
from pathlib import Path
from urllib.parse import urlparse

# SQLite repo (new)
from app.services.sqlite_repo import LocalSQLiteContentRepository

# Optional: if you've added LocalDBPublisher, we'll use it; otherwise we fall back.
try:  # pragma: no cover - optional class during migration
    from app.services.publisher import LocalDBPublisher  # type: ignore
except Exception:  # pragma: no cover - optional class during migration
    LocalDBPublisher = None  # type: ignore[assignment]

LOGGER = logging.getLogger("liveon.pipeline")
if not LOGGER.handlers:  # avoid dupes on re-import
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    LOGGER.addHandler(h)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False

LOGGER.info("PIPELINE_START")

DEFAULT_FEEDS: Sequence[FeedSource] = (
    FeedSource(
        name="Google News: Longevity Research",
        url="https://news.google.com/rss/search?q=longevity+research&hl=en-US&gl=US&ceid=US:en",
        topic="research",
    ),
    FeedSource(
        name="Google News: Healthy Aging",
        url="https://news.google.com/rss/search?q=%22healthy+aging%22&hl=en-US&gl=US&ceid=US:en",
        topic="aging",
    ),
    FeedSource(
        name="Google News: Longevity Nutrition",
        url="https://news.google.com/rss/search?q=longevity+nutrition&hl=en-US&gl=US&ceid=US:en",
        topic="lifestyle",
    ),
)

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
    level_name = os.getenv("LIVEON_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def _load_feeds() -> list[FeedSource]:
    """Return the feed configuration, allowing overrides via environment variables."""
    raw_sources = os.getenv("LIVEON_FEED_SOURCES")
    if not raw_sources:
        return [*DEFAULT_FEEDS]

    try:
        payload = json.loads(raw_sources)
    except json.JSONDecodeError as exc:  # pragma: no cover - user configuration
        raise SystemExit("LIVEON_FEED_SOURCES must contain valid JSON") from exc

    feeds: list[FeedSource] = []
    for entry in payload:
        try:
            feeds.append(
                FeedSource(
                    name=entry["name"],
                    url=entry["url"],
                    topic=entry.get("topic"),
                )
            )
        except KeyError as exc:  # pragma: no cover - user configuration
            raise SystemExit(f"Feed configuration missing key: {exc}") from exc
    return feeds


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Live On content pipeline.")
    parser.add_argument(
        "--storage",
        choices=["sqlite"],
        default=os.getenv("LIVEON_STORAGE", "sqlite").lower(),
        help="Select backing storage (default from LIVEON_STORAGE or 'sqlite').",
    )
    parser.add_argument(
        "--db-path",
        default=os.getenv("LIVEON_DB_PATH"),
        help="Path to SQLite database file (default from LIVEON_DB_PATH or user profile).",
    )
    parser.add_argument(
        "--feed-limit",
        type=int,
        default=int(os.getenv("LIVEON_FEED_LIMIT", "5")),
        help="Max items per feed to aggregate.",
    )
    return parser.parse_args(argv)


def _create_llm(agent_label: str) -> "SupportsInvoke":
    """Instantiate a LangChain compatible chat model for the given agent."""
    provider_env_key = f"LIVEON_{agent_label.upper()}_MODEL"
    provider_env_value = os.getenv(provider_env_key)
    provider = (provider_env_value or "ollama").lower()

    if provider == "ollama":
        ChatOllama = _resolve_chat_ollama()
        model_name = (
            os.getenv(f"LIVEON_{agent_label.upper()}_OLLAMA_MODEL")
            or os.getenv("LIVEON_OLLAMA_MODEL")
            or 'phi3:14b-medium-4k-instruct-q4_K_M'
        )
        format_hint = (os.getenv("LIVEON_OLLAMA_FORMAT") or "json").strip().lower()
        base_url = _resolve_ollama_base_url()
        kwargs: dict[str, object] = {"model": model_name, "base_url": base_url}
        if format_hint:
            kwargs["format"] = format_hint
        return ChatOllama(**kwargs)

    if provider in {"openai", "gpt"}:  # pragma: no cover - optional dependency
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise SystemExit("Install langchain-openai to use the OpenAI chat model") from exc

        return ChatOpenAI(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=float(os.getenv("LIVEON_MODEL_TEMPERATURE", "0.2")),
        )

    return LocalJSONResponder(agent_label)


def _resolve_chat_ollama():
    try:
        from langchain_ollama import ChatOllama  # type: ignore
    except ImportError:
        try:
            from langchain_community.chat_models import ChatOllama  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise SystemExit("Install langchain-ollama to use the Ollama provider") from exc
    return ChatOllama


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


class SupportsInvoke(Protocol):
    """Protocol implemented by LangChain chat models."""

    def invoke(self, input: object, **kwargs: object) -> BaseMessage | str:  # pragma: no cover - interface
        """Invoke the underlying model."""


class LocalJSONResponder:
    """Deterministic responder that fabricates JSON payloads for local testing."""

    def __init__(self, agent_label: str) -> None:
        self.agent_label = agent_label.lower()

    def invoke(self, input: object, **kwargs: object) -> AIMessage:
        if isinstance(input, list) and input:
            final_message = input[-1]
            content = getattr(final_message, "content", str(final_message))
        else:
            content = str(input)

        if self.agent_label == "summarizer":
            payload = self._summarizer_payload(str(content))
        else:
            payload = self._editor_payload(str(content))
        return AIMessage(content=json.dumps(payload, default=_json_default, ensure_ascii=False))

    def _summarizer_payload(self, prompt: str) -> dict[str, object]:
        notes_section = prompt.split("Notes:", 1)[-1]
        notes_section = notes_section.split("Current date:", 1)[0]
        notes = [line.strip(" -") for line in notes_section.splitlines() if line.strip()]

        title = notes[0].split(" - ")[0] if notes else "Longevity Highlights"
        body_sections: list[str] = []
        takeaways: list[str] = []
        for note in notes:
            parts = [part.strip() for part in note.split(" - ") if part.strip()]
            if not parts:
                continue
            heading = parts[0]
            summary = " ".join(parts[1:]) if len(parts) > 1 else "Insights from recent research."
            body_sections.append(f"### {heading}\n{summary}")
            takeaways.append(heading)

        summary = body_sections[0] if body_sections else "Fresh longevity guidance from trusted sources."
        body = "\n\n".join(body_sections) if body_sections else "Stay tuned for the latest longevity science."

        return {
            "title": title,
            "summary": summary[:220].strip(),
            "body": body,
            "takeaways": takeaways[:3],
            "sources": [],
            "tags": ["longevity", "research"],
        }

    def _editor_payload(self, prompt: str) -> dict[str, object]:
        def _scan_for_object(text: str, *, prefer_last: bool) -> dict[str, object] | None:
            decoder = json
            idx = 0
            found: dict[str, object] | None = None
            while True:
                brace = text.find("{", idx)
                if brace == -1:
                    break
                try:
                    payload, end = decoder.JSONDecoder().raw_decode(text, brace)
                except json.JSONDecodeError:
                    idx = brace + 1
                    continue
                if isinstance(payload, dict):
                    found = payload
                    if not prefer_last:
                        return found
                idx = end
            return found

        marker = "Draft article JSON:"
        base: dict[str, object] | None = None

        if marker in prompt:
            after_marker = prompt.split(marker, 1)[1]
            base = _scan_for_object(after_marker, prefer_last=False)

        if base is None:
            base = _scan_for_object(prompt, prefer_last=True)

        if base is None:
            base = {
                "title": "Longevity Insights",
                "summary": "Latest updates from the world of healthy aging.",
                "body": "Stay tuned for curated longevity research.",
                "takeaways": ["Stay active", "Eat mindfully"],
                "sources": [],
                "tags": ["longevity"],
            }

        disclaimer = (
            "This article shares educational longevity insights. Consult a healthcare professional before making changes."
        )

        tags = list(dict.fromkeys((base.get("tags") or []) + ["longevity", "healthy-aging"]))

        return {
            "title": (base.get("title") or "Longevity Insights").strip() or "Longevity Insights",
            "summary": (base.get("summary") or "Latest longevity guidance.").strip() or "Latest longevity guidance.",
            "body": (base.get("body") or "Stay tuned for curated longevity research.").strip(),
            "takeaways": base.get("takeaways", []) or ["Stay curious about healthy aging."],
            "sources": base.get("sources", []),
            "tags": tags,
            "disclaimer": disclaimer,
        }


def _build_pipeline(storage: str, db_path: str | None, feed_limit: int) -> ContentPipeline:
    feeds = _load_feeds()
    aggregator = LongevityNewsAggregator(feeds)
    summarizer = SummarizerAgent(llm=_create_llm("summarizer"))
    editor = EditorAgent(llm=_create_llm("editor"))

    storage = (storage or os.getenv("LIVEON_STORAGE", "sqlite")).lower()

    repo = LocalSQLiteContentRepository(db_path=db_path)
    publisher = LocalDBPublisher(repository=repo)

    pipeline = ContentPipeline(
        aggregator=aggregator,
        summarizer=summarizer,
        editor=editor,
        publisher=publisher,
        repository=repo,  # let the pipeline do URL-based duplicate checks
    )

    # Allow feed limit to be overridden in run(); aggregator uses it there.
    os.environ["LIVEON_FEED_LIMIT"] = str(int(feed_limit))
    return pipeline


def run(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = _parse_args(argv)
    pipeline = _build_pipeline(args.storage, args.db_path, args.feed_limit)

    limit = int(os.getenv("LIVEON_FEED_LIMIT", "5"))
    result = pipeline.run(limit_per_feed=limit)

    for warning in result.warnings:
        LOGGER.warning(warning)

    if not result.succeeded:
        if result.errors:
            for error in result.errors:
                LOGGER.error(error)
            return 1

        LOGGER.warning("Pipeline finished without producing content. No articles were published this run.")
        return 0

    publication = result.publication
    assert publication is not None  # for mypy

    LOGGER.info("Published article '%s' at %s", publication.slug, publication.published_at.isoformat())
    LOGGER.info("Storage path: %s", publication.path)
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(run())
