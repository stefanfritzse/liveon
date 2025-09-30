"""Integration-style tests for the tip pipeline CLI runner."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

import pytest

from app.models.aggregator import AggregatedContent
from app.models.content import Tip
from app.models.tip import TipDraft
from app.scripts import run_tip_pipeline
from app.services.aggregator import AggregationResult
from app.services.pipeline import TipPipeline
from app.services.tip_publisher import TipPublicationResult


def _json_from_stdout(output: str) -> dict[str, Any]:
    """Return the final JSON object emitted by the CLI."""

    lines = [line for line in output.splitlines() if line.strip()]
    json_line = next(line for line in reversed(lines) if line.lstrip().startswith("{"))
    return json.loads(json_line)


def _aggregated_item() -> AggregatedContent:
    """Return a deterministic aggregated content instance for tests."""

    return AggregatedContent(
        title="Daily Movement",
        url="https://example.com/longevity/daily-movement",
        summary="Short walk guidance",
        published_at=datetime(2024, 2, 2, tzinfo=timezone.utc),
        source="Integration Feed",
        topic="habits",
        raw={"id": "daily-movement"},
    )


@dataclass(slots=True)
class StubAggregator:
    """Aggregator that records invocations and returns canned items."""

    items: list[AggregatedContent]
    errors: list[str] = field(default_factory=list)
    calls: list[int] = field(default_factory=list, init=False)

    def gather(self, *, limit_per_feed: int = 5) -> AggregationResult:
        self.calls.append(limit_per_feed)
        return AggregationResult(items=list(self.items), errors=list(self.errors))


@dataclass(slots=True)
class StubGenerator:
    """Generator that records aggregated inputs and returns a fixed draft."""

    draft: TipDraft
    calls: list[Sequence[AggregatedContent]] = field(default_factory=list, init=False)

    def generate(self, items: Sequence[AggregatedContent]) -> TipDraft:
        self.calls.append(tuple(items))
        return self.draft


@dataclass(slots=True)
class StubPublisher:
    """Publisher that records drafts and returns a predetermined result."""

    result: TipPublicationResult
    calls: list[dict[str, Any]] = field(default_factory=list, init=False)

    def publish(self, draft: TipDraft, *, published_at: datetime | None = None) -> TipPublicationResult:
        self.calls.append({"draft": draft, "published_at": published_at})
        return self.result


@contextmanager
def _capture_tip_pipeline_logs() -> Iterator[list[str]]:
    logger = logging.getLogger("liveon.tip_pipeline")
    handler = _ListHandler()
    logger.addHandler(handler)
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401 - simple collector
        self.messages.append(record.getMessage())


def test_tip_pipeline_cli_success(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """The CLI logs execution details and returns success when a tip is created."""

    aggregator = StubAggregator(items=[_aggregated_item()], errors=["Feed timeout"])
    generator = StubGenerator(
        draft=TipDraft(title="Move today", body="Take a brisk 10 minute walk.", tags=["movement", "cardio"])
    )
    stored_tip = Tip(
        title="Move today",
        content_body="Take a brisk 10 minute walk.",
        tags=["movement", "cardio"],
        published_date=datetime(2024, 2, 3, tzinfo=timezone.utc),
        id="tip-1",
    )
    publisher = StubPublisher(result=TipPublicationResult(tip=stored_tip, created=True))

    pipeline = TipPipeline(aggregator=aggregator, generator=generator, publisher=publisher)
    stub_llm = object()

    monkeypatch.setattr(run_tip_pipeline, "_configure_logging", lambda: None)
    monkeypatch.setattr(
        run_tip_pipeline,
        "_create_tip_llm",
        lambda provider, *, model_name=None, allow_local_stub=False: stub_llm,
    )
    monkeypatch.setattr(run_tip_pipeline, "_build_pipeline", lambda llm: pipeline)

    with _capture_tip_pipeline_logs() as log_messages:
        exit_code = run_tip_pipeline.main(["--limit-per-feed", "4", "--model-provider", "local"])
    captured = capsys.readouterr()
    payload = _json_from_stdout(captured.out)

    assert exit_code == 0
    assert aggregator.calls == [4]
    assert len(generator.calls) == 1 and generator.calls[0][0].title == "Daily Movement"
    assert len(publisher.calls) == 1 and publisher.calls[0]["draft"].title == "Move today"

    assert payload["succeeded"] is True
    assert payload["created"] is True
    assert payload["aggregation"]["errors"] == ["Feed timeout"]
    assert payload["tip"]["title"] == "Move today"

    assert any("TIP_PIPELINE_START limit_per_feed=4" in line for line in log_messages)
    assert any("TIP_PIPELINE_WARNING Feed timeout" in line for line in log_messages)
    assert any("TIP_PIPELINE_COMPLETE created=True" in line for line in log_messages)


def test_tip_pipeline_cli_idempotent(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """Existing tips should trigger idempotent warnings without failing the CLI."""

    aggregator = StubAggregator(items=[_aggregated_item()])
    generator = StubGenerator(
        draft=TipDraft(title="Move today", body="Take a brisk 10 minute walk.", tags=["movement", "cardio"])
    )
    existing_tip = Tip(
        title="Move today",
        content_body="Take a brisk 10 minute walk.",
        tags=["movement", "cardio"],
        published_date=datetime(2024, 2, 1, tzinfo=timezone.utc),
        id="existing-tip",
    )
    publisher = StubPublisher(result=TipPublicationResult(tip=existing_tip, created=False))

    pipeline = TipPipeline(aggregator=aggregator, generator=generator, publisher=publisher)

    monkeypatch.setattr(run_tip_pipeline, "_configure_logging", lambda: None)
    monkeypatch.setattr(run_tip_pipeline, "_create_tip_llm", lambda *args, **kwargs: object())
    monkeypatch.setattr(run_tip_pipeline, "_build_pipeline", lambda llm: pipeline)

    with _capture_tip_pipeline_logs() as log_messages:
        exit_code = run_tip_pipeline.main(["--limit-per-feed", "2", "--model-provider", "local"])
    captured = capsys.readouterr()
    payload = _json_from_stdout(captured.out)

    assert exit_code == 0
    assert payload["created"] is False
    assert payload["succeeded"] is True
    assert any(
        "TIP_PIPELINE_WARNING Tip already exists; skipped creating a duplicate." in line
        for line in log_messages
    )


def test_tip_pipeline_cli_failure(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """Failures surface structured error logs and a non-zero exit code."""

    aggregator = StubAggregator(items=[_aggregated_item()])

    class FailingGenerator(StubGenerator):
        def generate(self, items: Sequence[AggregatedContent]) -> TipDraft:  # type: ignore[override]
            raise RuntimeError("LLM offline")

    generator = FailingGenerator(draft=TipDraft(title="", body="", tags=[]))
    publisher = StubPublisher(
        result=TipPublicationResult(
            tip=Tip(title="placeholder", content_body="", tags=[], published_date=datetime.now(timezone.utc)),
            created=True,
        )
    )

    pipeline = TipPipeline(aggregator=aggregator, generator=generator, publisher=publisher)

    monkeypatch.setattr(run_tip_pipeline, "_configure_logging", lambda: None)
    monkeypatch.setattr(run_tip_pipeline, "_create_tip_llm", lambda *args, **kwargs: object())
    monkeypatch.setattr(run_tip_pipeline, "_build_pipeline", lambda llm: pipeline)

    with _capture_tip_pipeline_logs() as log_messages:
        exit_code = run_tip_pipeline.main(["--model-provider", "local"])
    captured = capsys.readouterr()
    payload = _json_from_stdout(captured.out)

    assert exit_code == 1
    assert payload["succeeded"] is False
    assert payload["errors"] and "Tip generator failed" in payload["errors"][0]
    assert any("TIP_PIPELINE_ERROR Tip generator failed: LLM offline" in line for line in log_messages)
    assert any("Tip pipeline failed to produce a tip" in line for line in log_messages)
