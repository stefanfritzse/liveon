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
from typing import Protocol

from app.utils.langchain_compat import AIMessage, BaseMessage

from app.models.aggregator import FeedSource
from app.services.aggregator import LongevityNewsAggregator, load_feeds
from app.services.editor import EditorAgent
from app.services.pipeline import ContentPipeline
from app.services.llm_factory import create_chat_model
from app.services.summarizer import SummarizerAgent
from dataclasses import is_dataclass, asdict
from datetime import datetime, date, timezone
from pathlib import Path

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
    """Return the shared feed configuration, surfacing config errors as exit codes."""

    try:
        return load_feeds()
    except ValueError as exc:  # pragma: no cover - user configuration
        raise SystemExit(str(exc)) from exc


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
        help="Max items per feed to aggregate (widens the candidate pool).",
    )
    parser.add_argument(
        "--max-articles",
        type=int,
        default=int(os.getenv("LIVEON_MAX_ARTICLES", "1")),
        help="How many articles to publish in this run (default 1).",
    )
    return parser.parse_args(argv)


def _create_llm(agent_label: str) -> "SupportsInvoke":
    """Instantiate a LangChain compatible chat model for the given agent."""

    return create_chat_model(
        agent_label=agent_label,
        json_mode=True,
        local_factory=lambda: LocalJSONResponder(agent_label),
    )


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
    # Logged here rather than at import: the web app imports this module to run the
    # scheduled job, and used to announce a pipeline start just by loading it.
    LOGGER.info("PIPELINE_START")
    args = _parse_args(argv)
    pipeline = _build_pipeline(args.storage, args.db_path, args.feed_limit)

    limit = int(os.getenv("LIVEON_FEED_LIMIT", "5"))
    result = pipeline.run(limit_per_feed=limit, max_articles=args.max_articles)

    for warning in result.warnings:
        LOGGER.warning(warning)

    if not result.succeeded:
        if result.errors:
            for error in result.errors:
                LOGGER.error(error)
            return 1

        LOGGER.warning("Pipeline finished without producing content. No articles were published this run.")
        return 0

    for publication in result.publications:
        LOGGER.info(
            "Published article '%s' at %s",
            publication.slug,
            publication.published_at.isoformat(),
        )
        LOGGER.info("Storage path: %s", publication.path)
    LOGGER.info("PIPELINE_COMPLETE published=%d", result.published_count)
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(run())
