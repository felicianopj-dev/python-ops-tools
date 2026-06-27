#!/usr/bin/env python3
"""
retry_client.py

A small, reusable HTTP client that brings two resilience patterns from
payment-API design into a general-purpose tool:

1. Exponential backoff with jitter
   Transient failures (connection errors, timeouts, and retryable status codes
   such as 5xx / 408 / 429) are retried with an exponentially growing delay plus
   random jitter, so a fleet of clients does not retry in lockstep ("thundering
   herd"). Client errors (4xx, except 408/429) are never retried — they will not
   succeed on replay.

2. Idempotency / request deduplication
   Each request may carry an idempotency key. The first successful response for a
   key is cached; any later request with the same key returns the cached response
   instead of performing the call again. This is exactly how payment APIs avoid
   charging a customer twice when a client retries after an ambiguous failure.

The cache is pluggable: in-memory by default, or a local JSON file for dedup that
survives process restarts.

This module is meant to be imported as a library (ResilientClient / RetryConfig)
and also ships a small CLI demo (see `python3 scripts/retry_client.py --help`).

Exit codes (CLI demo):
  0  request succeeded
  2  bad arguments / configuration
  3  request ultimately failed (after retries)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

import oplog


# --------------------------------------------------------------------------- #
# Cached response value object
# --------------------------------------------------------------------------- #
@dataclass
class CachedResponse:
    """
    A minimal, JSON-serializable snapshot of a successful HTTP response.

    Stored by the idempotency cache and replayed (wrapped in
    `_CachedHTTPResponse`) when a request reuses a known idempotency key.
    """

    status_code: int
    headers: dict[str, str]
    body: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON persistence."""
        return {
            "status_code": self.status_code,
            "headers": self.headers,
            "body": self.body,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CachedResponse:
        """Reconstruct from a dict produced by `to_dict`."""
        return cls(
            status_code=int(data["status_code"]),
            headers=dict(data.get("headers", {})),
            body=str(data.get("body", "")),
            url=str(data.get("url", "")),
        )


class _CachedHTTPResponse:
    """
    Lightweight stand-in for `requests.Response` served from the cache.

    Exposes only the attributes callers commonly read, so cached and live
    responses can be handled uniformly. `from_cache` marks replays.
    """

    def __init__(self, cached: CachedResponse) -> None:
        self.status_code: int = cached.status_code
        self.headers: dict[str, str] = cached.headers
        self.text: str = cached.body
        self.url: str = cached.url
        self.from_cache: bool = True

    @property
    def ok(self) -> bool:
        """Mirror `requests.Response.ok` (True for status < 400)."""
        return self.status_code < 400

    def json(self) -> Any:
        """Parse the cached body as JSON (mirrors `requests.Response.json`)."""
        return json.loads(self.text)


# --------------------------------------------------------------------------- #
# Idempotency cache backends
# --------------------------------------------------------------------------- #
class IdempotencyCache:
    """Abstract idempotency cache interface."""

    def get(self, key: str) -> CachedResponse | None:
        """Return the cached response for `key`, or None if absent."""
        raise NotImplementedError

    def set(self, key: str, value: CachedResponse) -> None:
        """Store `value` under `key`."""
        raise NotImplementedError


class MemoryCache(IdempotencyCache):
    """In-process idempotency cache. Lives only for the current run."""

    def __init__(self) -> None:
        self._store: dict[str, CachedResponse] = {}

    def get(self, key: str) -> CachedResponse | None:
        return self._store.get(key)

    def set(self, key: str, value: CachedResponse) -> None:
        self._store[key] = value


class JsonFileCache(IdempotencyCache):
    """
    Idempotency cache persisted to a JSON file.

    The whole file is loaded into memory on init and rewritten atomically on each
    `set` (temp file + os.replace) so a crash mid-write cannot corrupt it. A
    missing or malformed file is tolerated by starting from an empty cache.
    """

    def __init__(self, path: str) -> None:
        self.path: str = path
        self._store: dict[str, CachedResponse] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"[warn] could not read cache file {self.path}, starting empty: {e}",
                file=sys.stderr,
            )
            return
        if isinstance(raw, dict):
            for key, value in raw.items():
                try:
                    self._store[key] = CachedResponse.from_dict(value)
                except (KeyError, TypeError, ValueError):
                    # Skip malformed entries rather than failing the whole load.
                    continue

    def _flush(self) -> None:
        serializable = {k: v.to_dict() for k, v in self._store.items()}
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2)
        os.replace(tmp_path, self.path)

    def get(self, key: str) -> CachedResponse | None:
        return self._store.get(key)

    def set(self, key: str, value: CachedResponse) -> None:
        self._store[key] = value
        self._flush()


# --------------------------------------------------------------------------- #
# Retry configuration and client
# --------------------------------------------------------------------------- #
@dataclass
class RetryConfig:
    """Tunable retry/backoff parameters for `ResilientClient`."""

    max_retries: int = 3
    base_delay: float = 0.5
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    jitter: bool = True
    # Status codes worth retrying: server errors plus request-timeout / too-many-
    # requests. All other 4xx are treated as permanent and not retried.
    retry_statuses: frozenset = field(
        default_factory=lambda: frozenset({500, 502, 503, 504, 408, 429})
    )


# Exceptions that represent transient transport problems worth retrying.
RETRYABLE_EXCEPTIONS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)


class ResilientClient:
    """
    HTTP client wrapping `requests` with exponential backoff and idempotency.

    Example:
        client = ResilientClient(RetryConfig(max_retries=5))
        resp = client.post(
            "https://api.example.com/charge",
            json={"amount": 1000},
            idempotency_key="order-42",
        )
    """

    def __init__(
        self,
        config: RetryConfig | None = None,
        cache: IdempotencyCache | None = None,
        session: requests.Session | None = None,
        timeout: float = 10.0,
        sleeper: Callable[[float], None] = time.sleep,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.config: RetryConfig = config or RetryConfig()
        self.cache: IdempotencyCache = cache or MemoryCache()
        self.session: requests.Session = session or requests.Session()
        self.timeout: float = timeout
        # Injectable sleep so tests/demos can run without real delays.
        self._sleep: Callable[[float], None] = sleeper
        # Optional attempt logger; no-op by default.
        self._log: Callable[[str], None] = logger or (lambda _msg: None)

    def _compute_delay(self, attempt: int) -> float:
        """
        Delay before the retry following `attempt` (0-based).

        Exponential growth capped at `max_delay`, then full jitter — a uniform
        random value in [0, delay] — to spread out concurrent retriers.
        """
        delay = self.config.base_delay * (self.config.backoff_factor**attempt)
        delay = min(delay, self.config.max_delay)
        if self.config.jitter:
            delay = random.uniform(0, delay)
        return delay

    def _is_retryable_status(self, status_code: int) -> bool:
        """True when a status code should trigger a retry."""
        return status_code in self.config.retry_statuses

    def request(
        self,
        method: str,
        url: str,
        *,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Perform an HTTP request with retries and optional idempotency.

        When `idempotency_key` is set and a successful response is already cached
        for it, that cached response (a `_CachedHTTPResponse`) is returned without
        making a network call. Otherwise the request is attempted up to
        `max_retries + 1` times; only a final successful (2xx) response is cached.

        Raises the last transport exception if every attempt fails with a
        connection/timeout error. Non-retryable HTTP errors (e.g. 4xx) are
        returned as-is for the caller to inspect.
        """
        # Idempotent replay: serve a previously cached success.
        if idempotency_key is not None:
            cached = self.cache.get(idempotency_key)
            if cached is not None:
                self._log(f"idempotency hit for key '{idempotency_key}' (served from cache)")
                return _CachedHTTPResponse(cached)

        kwargs.setdefault("timeout", self.timeout)
        last_exc: BaseException | None = None
        response: requests.Response | None = None

        # Total tries = initial attempt + max_retries.
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.session.request(method.upper(), url, **kwargs)
            except RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
                self._log(f"attempt {attempt + 1} failed: {type(exc).__name__}: {exc}")
                response = None
            else:
                last_exc = None
                if not self._is_retryable_status(response.status_code):
                    # Success or a permanent error (e.g. 4xx): stop immediately.
                    self._log(f"attempt {attempt + 1} -> HTTP {response.status_code} (final)")
                    break
                self._log(f"attempt {attempt + 1} -> HTTP {response.status_code} (retryable)")

            # If we are out of retries, stop looping.
            if attempt >= self.config.max_retries:
                break

            delay = self._compute_delay(attempt)
            self._log(f"backing off {delay:.3f}s before retry")
            self._sleep(delay)

        # Every attempt raised a transport error: re-raise the last one.
        if response is None:
            assert last_exc is not None
            raise last_exc

        # Cache only final successes (2xx) under the idempotency key.
        if idempotency_key is not None and 200 <= response.status_code < 300:
            self.cache.set(
                idempotency_key,
                CachedResponse(
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    body=response.text,
                    url=response.url,
                ),
            )

        return response

    # Convenience verb wrappers ------------------------------------------------
    def get(self, url: str, **kwargs: Any) -> Any:
        """Send a GET request (see `request`)."""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        """Send a POST request (see `request`)."""
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Any:
        """Send a PUT request (see `request`)."""
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        """Send a DELETE request (see `request`)."""
        return self.request("DELETE", url, **kwargs)


# --------------------------------------------------------------------------- #
# CLI demo
# --------------------------------------------------------------------------- #
def _parse_args(argv: list | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Demo the ResilientClient: retry with backoff + idempotency.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--url",
        default="https://httpbin.org/status/500",
        help="URL to call. The default returns 500 to demonstrate retries.",
    )
    parser.add_argument("--method", default="GET", help="HTTP method.")
    parser.add_argument("--max-retries", type=int, default=3, help="Maximum retries.")
    parser.add_argument(
        "--base-delay", type=float, default=0.5, help="Base backoff delay (seconds)."
    )
    parser.add_argument(
        "--idempotency-key",
        help="If set, the demo issues the request twice to show cached replay.",
    )
    parser.add_argument(
        "--cache-file",
        help="Persist the idempotency cache to this JSON file (default: in-memory).",
    )
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="Per-request timeout (seconds)."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also emit a machine-readable JSON summary line (or set LOG_JSON=1).",
    )
    return parser.parse_args(argv)


def _describe(label: str, resp: Any) -> None:
    """Print a short, uniform summary of a live or cached response."""
    from_cache = getattr(resp, "from_cache", False)
    tag = " (from cache)" if from_cache else ""
    snippet = " ".join(resp.text.split())[:120]
    print(f"{label}: HTTP {resp.status_code}{tag} | {snippet}")


def main(argv: list | None = None) -> int:
    args = _parse_args(argv)

    if args.max_retries < 0:
        print("Error: --max-retries must be >= 0.", file=sys.stderr)
        return 2

    cache: IdempotencyCache = JsonFileCache(args.cache_file) if args.cache_file else MemoryCache()
    config = RetryConfig(max_retries=args.max_retries, base_delay=args.base_delay)
    client = ResilientClient(
        config=config,
        cache=cache,
        timeout=args.timeout,
        logger=lambda msg: print(f"  · {msg}"),
    )

    print(f"Calling {args.method.upper()} {args.url}")
    try:
        resp = client.request(args.method, args.url, idempotency_key=args.idempotency_key)
    except requests.RequestException as exc:
        print(f"Request failed after retries: {exc}", file=sys.stderr)
        return 3

    _describe("Result", resp)

    # Demonstrate idempotent replay: a second keyed call is served from cache.
    if args.idempotency_key:
        print("\nRepeating the same request with the same idempotency key...")
        try:
            resp2 = client.request(args.method, args.url, idempotency_key=args.idempotency_key)
        except requests.RequestException as exc:
            print(f"Replay failed: {exc}", file=sys.stderr)
            return 3
        _describe("Replay", resp2)

    if oplog.want_json(args.json):
        oplog.log(
            "info" if resp.ok else "error",
            "retry_result",
            as_json=True,
            url=args.url,
            method=args.method.upper(),
            status=resp.status_code,
            ok=bool(resp.ok),
            from_cache=getattr(resp, "from_cache", False),
        )

    return 0 if resp.ok else 3


if __name__ == "__main__":
    sys.exit(main())
