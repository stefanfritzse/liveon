"""Execute the multi-agent content pipeline and publish results to Firestore.

This utility composes the aggregator, summariser, editor, and publisher agents
so that the pipeline can be run manually or on a schedule (for example via a
Kubernetes CronJob). When optional LangChain integrations for Vertex AI or
OpenAI are available the script will prefer those chat models. For local
development it falls back to a deterministic JSON responder, allowing the
pipeline to be exercised without external LLM access.
"""
from __future__ import annotations

import json
import logging, sys
import os
from typing import Protocol, Sequence

from app.utils.langchain_compat import AIMessage, BaseMessage

from app.models.aggregator import FeedSource
from app.services.aggregator import LongevityNewsAggregator
from app.services.editor import EditorAgent
from app.services.firestore import FirestoreContentRepository
from app.services.pipeline import ContentPipeline
from app.services.publisher import FirestorePublisher
from app.services.summarizer import SummarizerAgent

LOGGER = logging.getLogger("liveon.pipeline")
if not LOGGER.handlers:  # avoid dupes on re-import
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
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


def _create_llm(agent_label: str) -> "SupportsInvoke":
    """Instantiate a LangChain compatible chat model for the given agent."""

    provider = os.getenv(f"LIVEON_{agent_label.upper()}_MODEL", "local").lower()

    if provider == "vertex":  # pragma: no cover - optional dependency
        try:
            from langchain_google_vertexai import ChatVertexAI
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise SystemExit(
                "Install langchain-google-vertexai to use the Vertex AI chat model"
            ) from exc

        return ChatVertexAI(
            model=os.getenv("VERTEX_MODEL", "chat-bison"),
            temperature=float(os.getenv("LIVEON_MODEL_TEMPERATURE", "0.2")),
        )

    if provider in {"openai", "gpt"}:  # pragma: no cover - optional dependency
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise SystemExit("Install langchain-openai to use the OpenAI chat model") from exc

        return ChatOpenAI(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=float(os.getenv("LIVEON_MODEL_TEMPERATURE", "0.2")),
        )

    # Default: fall back to a deterministic responder that emits JSON payloads so
    # the pipeline can be executed in development environments without external
    # LLM access.
    return LocalJSONResponder(agent_label)


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
        return AIMessage(content=json.dumps(payload, ensure_ascii=False))

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
            decoder = json.JSONDecoder()
            index = 0
            found: dict[str, object] | None = None

            while True:
                brace = text.find("{", index)
                if brace == -1:
                    break
                try:
                    payload, end = decoder.raw_decode(text, brace)
                except json.JSONDecodeError:
                    index = brace + 1
                    continue
                if isinstance(payload, dict):
                    found = payload
                    if not prefer_last:
                        return found
                index = end

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
            "title": base.get("title", "Longevity Insights").strip() or "Longevity Insights",
            "summary": base.get("summary", "Latest longevity guidance.").strip() or "Latest longevity guidance.",
            "body": base.get("body", "Stay tuned for curated longevity research.").strip(),
            "takeaways": base.get("takeaways", []) or ["Stay curious about healthy aging."],
            "sources": base.get("sources", []),
            "tags": tags,
            "disclaimer": disclaimer,
        }


def _build_pipeline() -> ContentPipeline:
    feeds = _load_feeds()
    aggregator = LongevityNewsAggregator(feeds)
    summarizer = SummarizerAgent(llm=_create_llm("summarizer"))
    editor = EditorAgent(llm=_create_llm("editor"))
    repository = FirestoreContentRepository()
    publisher = FirestorePublisher(repository=repository)
    return ContentPipeline(
        aggregator=aggregator,
        summarizer=summarizer,
        editor=editor,
        publisher=publisher,
        repository=repository,
    )


def run() -> int:
    _configure_logging()
    pipeline = _build_pipeline()

    limit = int(os.getenv("LIVEON_FEED_LIMIT", "5"))
    result = pipeline.run(limit_per_feed=limit)

    for warning in result.warnings:
        LOGGER.warning(warning)

    if not result.succeeded:
        if result.errors:
            for error in result.errors:
                LOGGER.error(error)
            return 1

        LOGGER.warning(
            "Pipeline finished without producing content. No articles were published this run."
        )
        return 0

    publication = result.publication
    assert publication is not None  # for mypy

    LOGGER.info("Published article '%s' at %s", publication.slug, publication.published_at.isoformat())
    LOGGER.info("Firestore path: %s", publication.path)
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(run())
