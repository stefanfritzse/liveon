"""Shared politeness layer for the research APIs.

Every literature client goes through here, for three reasons:

* **Rate limits are per host, not per client.** NCBI allows three requests a second
  without an API key and ten with one, counted across everything we run. A limiter owned
  by each client would multiply the ceiling by the number of clients, so the limiter is
  module-level and keyed by host.
* **The cache is what makes the benchmark runnable.** Responses are stored on disk, so
  re-extraction, replays, and the offline test corpus cost nothing and reach no network.
* **Failures must be distinguishable.** A transport error is not an empty result. The
  pipeline has separate outcomes for "nothing new" and "could not retrieve"
  (improvements.md item 9), and it can only tell them apart if this layer raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import httpx

LOGGER = logging.getLogger(__name__)

__all__ = ["ResearchHttpClient", "ResearchRequestError", "cache_root", "reset_rate_limits"]

#: Repo-root ``cache/research`` unless told otherwise; the directory already exists.
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[3] / "cache" / "research"

_DEFAULT_TTL_HOURS = 168.0
_DEFAULT_TIMEOUT = 20.0
_DEFAULT_USER_AGENT = "LiveOnEvidenceLayer/1.0 (+https://liveon.health)"

#: Status codes worth retrying: transient upstream trouble or an explicit slow-down.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ResearchRequestError(RuntimeError):
    """Raised when a research source could not be reached or answered with an error."""


@dataclass(slots=True)
class _RateLimiter:
    """Minimum spacing between requests to one host."""

    min_interval: float
    _last_call: float = field(default=0.0)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self, *, monotonic: Callable[[], float], sleep: Callable[[float], None]) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = monotonic()
            wait = self._last_call + self.min_interval - now
            if wait > 0:
                sleep(wait)
                now = monotonic()
            self._last_call = now


_LIMITERS: dict[str, _RateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def _limiter_for(host: str, rate_per_second: float) -> _RateLimiter:
    interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get(host)
        if limiter is None:
            limiter = _RateLimiter(min_interval=interval)
            _LIMITERS[host] = limiter
        elif interval > limiter.min_interval:
            # The strictest requirement seen for this host wins.
            limiter.min_interval = interval
        return limiter


def reset_rate_limits() -> None:
    """Forget every host limiter. For tests; never call this from the pipeline."""

    with _LIMITERS_LOCK:
        _LIMITERS.clear()


def cache_root() -> Path:
    raw = (os.getenv("LIVEON_RESEARCH_CACHE_DIR") or "").strip()
    return Path(raw) if raw else _DEFAULT_CACHE_DIR


def _cache_ttl_seconds() -> float:
    raw = (os.getenv("LIVEON_RESEARCH_CACHE_TTL_HOURS") or "").strip()
    try:
        hours = float(raw) if raw else _DEFAULT_TTL_HOURS
    except ValueError:
        LOGGER.warning("Ignoring invalid LIVEON_RESEARCH_CACHE_TTL_HOURS=%r", raw)
        hours = _DEFAULT_TTL_HOURS
    return max(0.0, hours) * 3600.0


class ResearchHttpClient:
    """A cached, rate-limited, retrying HTTP client for one research source."""

    def __init__(
        self,
        *,
        source: str,
        rate_per_second: float = 3.0,
        client: httpx.Client | None = None,
        cache_dir: Path | None = None,
        cache_ttl_seconds: float | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        max_attempts: int = 3,
        user_agent: str = _DEFAULT_USER_AGENT,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._source = source
        self._rate = rate_per_second
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": user_agent}
        )
        self._cache_dir = cache_dir if cache_dir is not None else cache_root() / source
        self._cache_ttl = cache_ttl_seconds if cache_ttl_seconds is not None else _cache_ttl_seconds()
        self._max_attempts = max(1, max_attempts)
        self._sleep = sleep
        self._monotonic = monotonic
        self._now = now

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ResearchHttpClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- requests ------------------------------------------------------

    def get_text(self, url: str, params: Mapping[str, Any] | None = None) -> str:
        """GET ``url`` and return the body, from cache when it is still fresh."""

        query = {key: value for key, value in (params or {}).items() if value is not None}
        cache_path = self._cache_path(url, query)

        cached = self._read_cache(cache_path)
        if cached is not None:
            return cached

        body = self._fetch(url, query)
        self._write_cache(cache_path, url, query, body)
        return body

    def get_json(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        body = self.get_text(url, params)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ResearchRequestError(f"{self._source} returned malformed JSON: {exc}") from exc

    def _fetch(self, url: str, params: Mapping[str, Any]) -> str:
        host = urlparse(url).netloc or self._source
        limiter = _limiter_for(host, self._rate)
        last_error: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            limiter.acquire(monotonic=self._monotonic, sleep=self._sleep)
            try:
                response = self._client.get(url, params=dict(params))
            except httpx.HTTPError as exc:
                last_error = exc
                LOGGER.warning(
                    "Research request failed (%s attempt %s/%s): %s",
                    self._source,
                    attempt,
                    self._max_attempts,
                    exc,
                    extra={"event": "research.request_failed", "source": self._source},
                )
            else:
                if response.status_code in _RETRYABLE_STATUS:
                    last_error = ResearchRequestError(
                        f"{self._source} responded {response.status_code}"
                    )
                    LOGGER.warning(
                        "Research source %s responded %s (attempt %s/%s)",
                        self._source,
                        response.status_code,
                        attempt,
                        self._max_attempts,
                        extra={"event": "research.retryable_status", "source": self._source},
                    )
                elif response.status_code >= 400:
                    # A 404 or 400 is an answer, not an outage; retrying cannot help.
                    raise ResearchRequestError(
                        f"{self._source} responded {response.status_code} for {url}"
                    )
                else:
                    return response.text

            if attempt < self._max_attempts:
                self._sleep(min(2.0 ** (attempt - 1), 8.0))

        raise ResearchRequestError(
            f"{self._source} unreachable after {self._max_attempts} attempts: {last_error}"
        ) from last_error

    # -- cache ---------------------------------------------------------

    def _cache_path(self, url: str, params: Mapping[str, Any]) -> Path:
        signature = json.dumps({"url": url, "params": dict(sorted(params.items()))}, sort_keys=True)
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.json"

    def _read_cache(self, path: Path) -> str | None:
        if self._cache_ttl <= 0 or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        fetched_at = payload.get("fetched_at")
        body = payload.get("body")
        if not isinstance(fetched_at, (int, float)) or not isinstance(body, str):
            return None
        if self._now() - fetched_at > self._cache_ttl:
            return None
        return body

    def _write_cache(self, path: Path, url: str, params: Mapping[str, Any], body: str) -> None:
        if self._cache_ttl <= 0:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "source": self._source,
                        "url": url,
                        "params": dict(sorted(params.items())),
                        "fetched_at": self._now(),
                        "body": body,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - a full disk must not fail a run
            LOGGER.warning(
                "Could not write research cache entry: %s",
                exc,
                extra={"event": "research.cache_write_failed", "source": self._source},
            )
