"""Tests for the shared research HTTP layer.

The cache and the rate limiter are not conveniences: NCBI throttles callers that exceed
three requests a second, and the offline corpus depends on responses being replayable.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.services.research.http import (
    ResearchHttpClient,
    ResearchRequestError,
    reset_rate_limits,
)

URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"


def _client(handler, tmp_path: Path, **overrides) -> ResearchHttpClient:
    defaults = dict(
        source="pubmed",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        cache_dir=tmp_path / "cache",
        rate_per_second=0.0,
        sleep=lambda _seconds: None,
    )
    defaults.update(overrides)
    return ResearchHttpClient(**defaults)


def test_a_response_is_fetched_once_and_replayed_from_cache(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text='{"ok": true}')

    with _client(handler, tmp_path) as client:
        first = client.get_json(URL, {"term": "longevity"})
        second = client.get_json(URL, {"term": "longevity"})

    assert first == second == {"ok": True}
    assert len(calls) == 1


def test_different_parameters_are_cached_separately(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="body")

    with _client(handler, tmp_path) as client:
        client.get_text(URL, {"term": "longevity"})
        client.get_text(URL, {"term": "senescence"})

    assert len(calls) == 2


def test_an_expired_cache_entry_is_refetched(tmp_path: Path) -> None:
    calls: list[str] = []
    clock = {"now": 1_000.0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="body")

    with _client(handler, tmp_path, cache_ttl_seconds=60.0, now=lambda: clock["now"]) as client:
        client.get_text(URL, {"term": "longevity"})
        clock["now"] += 3_600
        client.get_text(URL, {"term": "longevity"})

    assert len(calls) == 2


def test_caching_can_be_turned_off(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="body")

    with _client(handler, tmp_path, cache_ttl_seconds=0.0) as client:
        client.get_text(URL)
        client.get_text(URL)

    assert len(calls) == 2
    assert not (tmp_path / "cache").exists()


def test_a_transient_failure_is_retried(tmp_path: Path) -> None:
    responses = [httpx.Response(503), httpx.Response(200, text="recovered")]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    with _client(handler, tmp_path) as client:
        assert client.get_text(URL) == "recovered"


def test_exhausted_retries_raise_rather_than_look_empty(tmp_path: Path) -> None:
    """A retrieval failure is not "no new research"; the two have different outcomes."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with _client(handler, tmp_path, max_attempts=2) as client:
        with pytest.raises(ResearchRequestError) as excinfo:
            client.get_text(URL)

    assert "unreachable" in str(excinfo.value)


def test_a_client_error_is_not_retried(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(404)

    with _client(handler, tmp_path) as client:
        with pytest.raises(ResearchRequestError):
            client.get_text(URL)

    assert len(calls) == 1


def test_transport_errors_are_retried_then_reported(tmp_path: Path) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("no route to host")

    with _client(handler, tmp_path, max_attempts=3) as client:
        with pytest.raises(ResearchRequestError):
            client.get_text(URL)

    assert len(attempts) == 3


def test_malformed_json_is_reported_as_a_source_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with _client(handler, tmp_path) as client:
        with pytest.raises(ResearchRequestError):
            client.get_json(URL)


def test_the_rate_limiter_is_shared_across_clients_for_one_host(tmp_path: Path) -> None:
    """Two clients must not double the request rate NCBI sees from us."""

    reset_rate_limits()
    waits: list[float] = []
    ticks = {"now": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="body")

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        ticks["now"] += seconds

    def monotonic() -> float:
        return ticks["now"]

    first = _client(
        handler, tmp_path / "a", rate_per_second=2.0, sleep=sleep, monotonic=monotonic
    )
    second = _client(
        handler, tmp_path / "b", rate_per_second=2.0, sleep=sleep, monotonic=monotonic
    )

    with first, second:
        first.get_text(URL, {"term": "one"})
        second.get_text(URL, {"term": "two"})

    assert waits, "the second client should have waited behind the first"
    assert waits[0] == pytest.approx(0.5)


def test_the_strictest_rate_for_a_host_wins(tmp_path: Path) -> None:
    reset_rate_limits()
    waits: list[float] = []
    ticks = {"now": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="body")

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        ticks["now"] += seconds

    relaxed = _client(
        handler, tmp_path / "a", rate_per_second=10.0, sleep=sleep, monotonic=lambda: ticks["now"]
    )
    strict = _client(
        handler, tmp_path / "b", rate_per_second=1.0, sleep=sleep, monotonic=lambda: ticks["now"]
    )

    with relaxed, strict:
        relaxed.get_text(URL, {"term": "one"})
        strict.get_text(URL, {"term": "two"})
        relaxed.get_text(URL, {"term": "three"})

    assert waits[-1] == pytest.approx(1.0)


def test_the_network_guard_blocks_a_client_built_without_a_transport() -> None:
    """Proves the conftest guard actually bites, so CI cannot reach PubMed by accident.

    The guard raises ``RuntimeError`` rather than a transport error on purpose: a test
    that reaches the network is a broken test, and dressing it up as "source unavailable"
    would let it pass quietly as a retrieval failure.
    """

    with ResearchHttpClient(source="pubmed", cache_ttl_seconds=0.0, max_attempts=1) as client:
        with pytest.raises(RuntimeError, match="Network access is disabled"):
            client.get_text("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi")
