"""Integration-style tests for the tip pipeline CLI runner."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest

from app.models.content import Tip
from app.models.tip import TipDraft
from app.models.tip_context import TipGenerationContext
from app.services.pipeline import TipPipelineResult
from app.services.tip_publisher import TipPublicationResult
from app.scripts import run_tip_pipeline


def _json_from_stdout(output: str) -> dict[str, Any]:
    """Return the final JSON object emitted by the CLI."""

    lines = [line for line in output.splitlines() if line.strip()]
    json_line = next(line for line in reversed(lines) if line.lstrip().startswith("{"))
    return json.loads(json_line)


@dataclass(slots=True)
class StubPipeline:
    """Pipeline double that records calls and returns predetermined results."""

    result: TipPipelineResult
    run_calls: list[dict[str, Any]] = field(default_factory=list, init=False)
    should_raise: bool = False

    def run(self, *, published_at: datetime | None = None) -> TipPipelineResult:  # pragma: no cover - exercised in tests
        self.run_calls.append({"published_at": published_at})
        if self.should_raise:
            raise RuntimeError("pipeline exploded")
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


def _tip_context() -> TipPipelineResult:
    context = TipGenerationContext(
        notes=["Encourage a brisk walk."],
        sources=["https://example.com/walk"],
        guidance="Prompt movement before lunch.",
    )
    draft = TipDraft(title="Walk cue", body="Take a brisk 10-minute walk.", tags=["movement"])
    stored_tip = Tip(
        title="Walk cue",
        content_body="Take a brisk 10-minute walk.",
        tags=["movement"],
        published_date=datetime(2024, 2, 3, tzinfo=timezone.utc),
        id="tip-1",
    )
    publication = TipPublicationResult(tip=stored_tip, created=True)
    return TipPipelineResult(
        context=context,
        draft=draft,
        tip=stored_tip,
        publication=publication,
        warnings=["Context preset applied"],
        errors=[],
        generation_attempts=1,
        editor_feedback=["Looks good"],
    )


def test_tip_pipeline_cli_success(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """The CLI logs execution details and returns success when a tip is created."""

    pipeline = StubPipeline(result=_tip_context())
    stub_llm = object()

    monkeypatch.setattr(run_tip_pipeline, "_configure_logging", lambda: None)
    monkeypatch.setattr(
        run_tip_pipeline,
        "_create_tip_llm",
        lambda provider, *, model_name=None, allow_local_stub=False: stub_llm,
    )
    monkeypatch.setattr(run_tip_pipeline, "_build_pipeline", lambda llm: pipeline)

    with _capture_tip_pipeline_logs() as log_messages:
        exit_code = run_tip_pipeline.main(["--model-provider", "local"])
    captured = capsys.readouterr()
    payload = _json_from_stdout(captured.out)

    assert exit_code == 0
    assert pipeline.run_calls and pipeline.run_calls[0]["published_at"] is None
    assert payload["succeeded"] is True
    assert payload["created"] is True
    assert payload["context"]["guidance"] == "Prompt movement before lunch."
    assert payload["warnings"] == ["Context preset applied"]
    assert any("TIP_PIPELINE_START provider=local" in line for line in log_messages)
    assert any("TIP_PIPELINE_WARNING Context preset applied" in line for line in log_messages)
    assert any("TIP_PIPELINE_COMPLETE created=True" in line for line in log_messages)


def test_tip_pipeline_cli_failure_result(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """Failed pipeline runs should exit with a non-zero code."""

    context_result = _tip_context()
    context_result.publication = None
    context_result.tip = None
    context_result.errors = ["Failed to review tip"]
    pipeline = StubPipeline(result=context_result)

    monkeypatch.setattr(run_tip_pipeline, "_configure_logging", lambda: None)
    monkeypatch.setattr(run_tip_pipeline, "_create_tip_llm", lambda *args, **kwargs: object())
    monkeypatch.setattr(run_tip_pipeline, "_build_pipeline", lambda llm: pipeline)

    with _capture_tip_pipeline_logs() as log_messages:
        exit_code = run_tip_pipeline.main(["--model-provider", "local"])
    captured = capsys.readouterr()
    payload = _json_from_stdout(captured.out)

    assert exit_code == 1
    assert payload["succeeded"] is False
    assert payload["errors"] == ["Failed to review tip"]
    assert any("Tip pipeline failed to produce a tip" in line for line in log_messages)


def test_tip_pipeline_cli_handles_exceptions(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """Unexpected exceptions from the pipeline propagate as logged failures."""

    pipeline = StubPipeline(result=_tip_context(), should_raise=True)

    monkeypatch.setattr(run_tip_pipeline, "_configure_logging", lambda: None)
    monkeypatch.setattr(run_tip_pipeline, "_create_tip_llm", lambda *args, **kwargs: object())
    monkeypatch.setattr(run_tip_pipeline, "_build_pipeline", lambda llm: pipeline)

    exit_code = run_tip_pipeline.main(["--model-provider", "local", "--published-at", "2024-02-05T09:00:00+00:00"])
    capsys.readouterr()

    assert exit_code == 1
    assert pipeline.run_calls and pipeline.run_calls[0]["published_at"] == datetime(
        2024, 2, 5, 9, tzinfo=timezone.utc
    )
