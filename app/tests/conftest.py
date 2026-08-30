"""Shared fixtures and helpers for the test suite."""

import socket

import pytest

from app.main import coach_rate_limiter
from app.services.research.http import reset_rate_limits


@pytest.fixture(autouse=True)
def _reset_coach_rate_limiter() -> None:
    """Keep the shared in-process rate limiter from leaking between tests."""

    coach_rate_limiter.reset()
    yield
    coach_rate_limiter.reset()


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a real socket.

    The evidence layer talks to PubMed, Europe PMC and ClinicalTrials.gov. Without this
    guard a client built with the wrong transport reaches the live internet from CI, the
    suite becomes dependent on a third party being up, and the offline corpus quietly
    stops being offline. ``httpx.MockTransport`` never opens a socket, so tests that fake
    HTTP are unaffected.

    Loopback is left alone: asyncio builds its event-loop self-pipe out of a real socket
    pair on Windows, so blocking every connection would take the whole async test suite
    down with the network.

    Mark a test ``@pytest.mark.live`` to opt out; those are excluded from CI.
    """

    if request.node.get_closest_marker("live"):
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection

    def _is_loopback(address: object) -> bool:
        host = address[0] if isinstance(address, tuple) and address else address
        if not isinstance(host, str):
            return True  # AF_UNIX and the like never leave the machine.
        return host in ("127.0.0.1", "::1", "localhost", "0.0.0.0", "", "testserver")

    def _refuse(address: object):
        raise RuntimeError(
            f"Network access is disabled in tests (attempted {address!r}). Use "
            "httpx.MockTransport, or mark the test @pytest.mark.live if it genuinely "
            "needs the network."
        )

    def _guarded_connect(self: socket.socket, address, *args, **kwargs):
        if not _is_loopback(address):
            _refuse(address)
        return real_connect(self, address, *args, **kwargs)

    def _guarded_connect_ex(self: socket.socket, address, *args, **kwargs):
        if not _is_loopback(address):
            _refuse(address)
        return real_connect_ex(self, address, *args, **kwargs)

    def _guarded_create_connection(address, *args, **kwargs):
        if not _is_loopback(address):
            _refuse(address)
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)


@pytest.fixture(autouse=True)
def _reset_research_rate_limits() -> None:
    """Host rate limiters are module-level; keep one test from stalling the next."""

    reset_rate_limits()
    yield
    reset_rate_limits()
