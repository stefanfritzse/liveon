"""Regression tests for coach endpoint stability and error handling.

These cover the failure modes that used to take the whole site down with them:
a synchronous model call blocking the event loop, an unbounded model call, error
classes that escaped the handler, and internal exception text reaching the browser.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app, get_coach_agent
from app.models.coach import CoachAnswer
from app.services import coach as coach_module
from app.services.coach import (
    CoachError,
    CoachTimeoutError,
    CoachUnavailableError,
    classify_llm_error,
)


class _StubAgent:
    """Coach agent stub that returns a fixed answer or raises a fixed error."""

    def __init__(self, *, answer: CoachAnswer | None = None, error: Exception | None = None) -> None:
        self._answer = answer
        self._error = error

    def ask(self, question: str) -> CoachAnswer:
        if self._error is not None:
            raise self._error
        assert self._answer is not None
        return self._answer


@pytest.fixture()
def coach_client():
    """Yield a factory that installs a coach agent override for one test."""

    clients: list[TestClient] = []

    def _factory(agent: object) -> TestClient:
        app.dependency_overrides[get_coach_agent] = lambda: agent
        created = TestClient(app)
        clients.append(created)
        return created

    yield _factory

    for created in clients:
        created.close()
    app.dependency_overrides.pop(get_coach_agent, None)


# ----------------------------------------------------------------------
# Event loop
# ----------------------------------------------------------------------


def test_slow_coach_answer_does_not_block_other_requests() -> None:
    """The site must keep serving while the coach is generating.

    The endpoint used to call the synchronous model client directly from an
    ``async def`` handler, which pinned the event loop for the whole generation —
    freezing every other visitor and starving the container liveness probe into
    restarting the pod mid-answer.

    The stub below only completes once ``/healthz`` has been served, so a blocked
    event loop cannot produce a passing run: the wait times out and the coach
    request fails instead.
    """

    health_served = threading.Event()

    class _BlockingAgent:
        def ask(self, question: str) -> CoachAnswer:
            if not health_served.wait(timeout=5.0):
                raise AssertionError(
                    "event loop was blocked: /healthz could not be served while the "
                    "coach was generating"
                )
            return CoachAnswer(message="Answered without stalling the loop.", disclaimer="")

    app.dependency_overrides[get_coach_agent] = lambda: _BlockingAgent()

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            ask = asyncio.create_task(client.post("/api/ask", json={"question": "Hello"}))
            await asyncio.sleep(0.05)  # let the request reach the agent
            health = await client.get("/healthz")
            health_served.set()
            return health, await ask

    try:
        health, answer = asyncio.run(scenario())
    finally:
        health_served.set()
        app.dependency_overrides.pop(get_coach_agent, None)

    assert health.status_code == 200
    assert answer.status_code == 200
    assert answer.json()["answer"] == "Answered without stalling the loop."


def test_a_slow_answer_is_bounded_by_the_configured_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LIVEON_LLM_TIMEOUT`` bounds the request even when the client never gives up.

    The model client's own timeout only limits the gap between streamed tokens, so a
    model that keeps emitting slowly would run unbounded. The wall-clock ceiling in the
    endpoint is what actually guarantees the request terminates.

    Timing is measured inside the event loop on purpose: ``TestClient`` tears down a
    fresh loop per request, and that teardown joins the abandoned worker thread, which
    would mask the very behaviour under test.
    """

    monkeypatch.setenv("LIVEON_LLM_TIMEOUT", "0.3")

    class _NeverFinishesInTime:
        def ask(self, question: str) -> CoachAnswer:
            time.sleep(3.0)
            return CoachAnswer(message="far too late", disclaimer="")

    app.dependency_overrides[get_coach_agent] = lambda: _NeverFinishesInTime()

    async def scenario() -> tuple[httpx.Response, float, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            started = time.perf_counter()
            response = await client.post("/api/ask", json={"question": "Hello"})
            elapsed = time.perf_counter() - started
            # The abandoned worker must not wedge the app.
            health = await client.get("/healthz")
            return response, elapsed, health

    try:
        response, elapsed, health = asyncio.run(scenario())
    finally:
        app.dependency_overrides.pop(get_coach_agent, None)

    assert response.status_code == 504
    assert elapsed < 2.0, "the ceiling did not cut the request short"
    assert "too long" in response.json()["detail"]["message"].lower()
    assert health.status_code == 200


# ----------------------------------------------------------------------
# Error classification -> status codes
# ----------------------------------------------------------------------


def test_unreachable_model_returns_503(coach_client) -> None:
    client = coach_client(_StubAgent(error=CoachUnavailableError("Connection refused")))

    response = client.post("/api/ask", json={"question": "How do I sleep better?"})

    assert response.status_code == 503
    assert "offline" in response.json()["detail"]["message"].lower()


def test_model_timeout_returns_504(coach_client) -> None:
    client = coach_client(_StubAgent(error=CoachTimeoutError("Read timed out")))

    response = client.post("/api/ask", json={"question": "How do I sleep better?"})

    assert response.status_code == 504
    assert "too long" in response.json()["detail"]["message"].lower()


def test_other_model_errors_return_503(coach_client) -> None:
    client = coach_client(_StubAgent(error=CoachError("model 'phi3' not found")))

    response = client.post("/api/ask", json={"question": "How do I sleep better?"})

    assert response.status_code == 503


def test_unexpected_error_returns_500_without_leaking(coach_client) -> None:
    """A bug in our own code is a 500, and still must not leak its message."""

    client = coach_client(_StubAgent(error=ValueError("secret internal state")))

    response = client.post("/api/ask", json={"question": "How do I sleep better?"})

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "secret internal state" not in json.dumps(detail)
    assert detail["reference"]


# ----------------------------------------------------------------------
# Error payload redaction
# ----------------------------------------------------------------------


def test_error_detail_is_redacted_by_default(coach_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVEON_DEBUG_ERRORS", raising=False)
    client = coach_client(_StubAgent(error=CoachUnavailableError("connect to 10.1.2.3 refused")))

    response = client.post("/api/ask", json={"question": "Hello"})
    detail = response.json()["detail"]

    assert "debug" not in detail
    assert "10.1.2.3" not in json.dumps(detail)
    assert len(detail["reference"]) == 12


def test_error_detail_includes_debug_when_enabled(
    coach_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIVEON_DEBUG_ERRORS", "1")
    client = coach_client(_StubAgent(error=CoachUnavailableError("connect to 10.1.2.3 refused")))

    response = client.post("/api/ask", json={"question": "Hello"})
    detail = response.json()["detail"]

    assert detail["debug"]["type"] == "CoachUnavailableError"
    assert "10.1.2.3" in detail["debug"]["message"]


def test_each_error_gets_a_distinct_reference(coach_client) -> None:
    client = coach_client(_StubAgent(error=CoachUnavailableError("down")))

    first = client.post("/api/ask", json={"question": "Hello"}).json()["detail"]
    second = client.post("/api/ask", json={"question": "Hello"}).json()["detail"]

    assert first["reference"] != second["reference"]


def test_successful_answer_is_unaffected(coach_client) -> None:
    answer = CoachAnswer(message="Walk after meals.", disclaimer="Educational only.")
    client = coach_client(_StubAgent(answer=answer))

    response = client.post("/api/ask", json={"question": "Glucose tips?"})

    assert response.status_code == 200
    assert response.json() == {"answer": "Walk after meals.", "disclaimer": "Educational only."}


# ----------------------------------------------------------------------
# Timeout configuration and classification helpers
# ----------------------------------------------------------------------


def test_llm_timeout_defaults_to_a_generous_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVEON_LLM_TIMEOUT", raising=False)
    assert coach_module.resolve_llm_timeout() == 180.0


@pytest.mark.parametrize("raw", ["nonsense", "0", "-5", ""])
def test_llm_timeout_rejects_unusable_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("LIVEON_LLM_TIMEOUT", raw)
    assert coach_module.resolve_llm_timeout() == 180.0


def test_llm_timeout_honours_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_LLM_TIMEOUT", "42.5")
    assert coach_module.resolve_llm_timeout() == 42.5


def test_http_fallback_client_adopts_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_LLM_TIMEOUT", "99")
    chat = coach_module.OllamaHTTPChat(model="test-model")
    assert chat.timeout == 99.0


def test_create_coach_llm_passes_timeout_to_chat_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_build(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setenv("LIVEON_LLM_TIMEOUT", "120")
    monkeypatch.setattr(coach_module, "build_chat_ollama", _fake_build)

    coach_module.create_coach_llm()

    assert captured["timeout"] == 120.0


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused"),
        ConnectionRefusedError("refused"),
        ConnectionError("refused"),
    ],
)
def test_connection_failures_classify_as_unavailable(error: Exception) -> None:
    assert isinstance(classify_llm_error(error), CoachUnavailableError)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("slow"),
        TimeoutError("slow"),
    ],
)
def test_timeouts_classify_as_timeout(error: Exception) -> None:
    assert isinstance(classify_llm_error(error), CoachTimeoutError)


def test_requests_style_errors_classify_without_importing_requests() -> None:
    """``langchain-community`` raises ``requests`` errors; match them by convention."""

    class ConnectionError_(OSError):  # mimics requests.exceptions.ConnectionError
        pass

    class ReadTimeout(OSError):  # mimics requests.exceptions.ReadTimeout
        pass

    assert isinstance(classify_llm_error(ConnectionError_("refused")), CoachUnavailableError)
    assert isinstance(classify_llm_error(ReadTimeout("slow")), CoachTimeoutError)


def test_unknown_errors_classify_as_generic_coach_error() -> None:
    classified = classify_llm_error(ValueError("model not found"))

    assert type(classified) is CoachError
    assert "model not found" in str(classified)


def test_classification_inspects_the_exception_chain() -> None:
    """Wrapped transport errors still classify correctly."""

    try:
        try:
            raise httpx.ConnectError("refused")
        except httpx.ConnectError as inner:
            raise ValueError("chat model call failed") from inner
    except ValueError as outer:
        classified = classify_llm_error(outer)

    assert isinstance(classified, CoachUnavailableError)


def test_agent_wraps_client_errors_into_coach_errors() -> None:
    """``CoachAgent.ask`` is the single place that normalises model failures."""

    class _ExplodingLLM:
        def invoke(self, messages: object) -> str:
            raise httpx.ConnectError("connection refused")

    agent = coach_module.CoachAgent(llm=_ExplodingLLM())

    with pytest.raises(CoachUnavailableError):
        agent.ask("Will this be classified?")


def test_coach_error_remains_a_runtime_error() -> None:
    """Existing callers guarding on RuntimeError keep working."""

    assert issubclass(CoachError, RuntimeError)
