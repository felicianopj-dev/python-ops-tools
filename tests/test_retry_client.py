"""Unit tests for the ResilientClient (retry + idempotency)."""

import pytest
import requests

from retry_client import (
    JsonFileCache,
    MemoryCache,
    ResilientClient,
    RetryConfig,
)


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code: int, text: str = "ok", url: str = "http://x") -> None:
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = {"Content-Type": "text/plain"}

    @property
    def ok(self) -> bool:
        return self.status_code < 400


class FakeSession:
    """Session that returns/raises a scripted sequence of results per call."""

    def __init__(self, sequence) -> None:
        self.sequence = list(sequence)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        item = self.sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item, text=f"resp-{item}")


def make_client(sequence, **cfg):
    """Build a client wired to a FakeSession and a no-op sleeper."""
    delays = []
    session = FakeSession(sequence)
    config = RetryConfig(jitter=False, **cfg)
    client = ResilientClient(config=config, session=session, sleeper=delays.append)
    return client, session, delays


def test_retries_5xx_then_succeeds():
    client, session, delays = make_client([503, 503, 200], max_retries=3, base_delay=0.5)
    resp = client.request("GET", "http://x")
    assert session.calls == 3
    assert resp.status_code == 200
    # Exponential growth without jitter: 0.5, then 1.0.
    assert delays == [0.5, 1.0]


def test_4xx_not_retried():
    client, session, delays = make_client([404, 200], max_retries=3)
    resp = client.request("GET", "http://x")
    assert session.calls == 1
    assert resp.status_code == 404
    assert delays == []


@pytest.mark.parametrize("retryable_status", [408, 429])
def test_408_and_429_are_retried(retryable_status):
    client, session, _ = make_client([retryable_status, 200], max_retries=2)
    resp = client.request("GET", "http://x")
    assert session.calls == 2
    assert resp.status_code == 200


def test_retries_exhausted_returns_last_response():
    client, session, _ = make_client([500, 500, 500, 500], max_retries=3)
    resp = client.request("GET", "http://x")
    assert session.calls == 4  # initial + 3 retries
    assert resp.status_code == 500


def test_connection_error_then_success():
    client, session, _ = make_client(
        [requests.exceptions.ConnectionError("boom"), 200], max_retries=3
    )
    resp = client.request("GET", "http://x")
    assert session.calls == 2
    assert resp.status_code == 200


def test_persistent_timeout_reraises():
    client, _, _ = make_client([requests.exceptions.Timeout("t")] * 4, max_retries=3)
    with pytest.raises(requests.exceptions.Timeout):
        client.request("GET", "http://x")


def test_idempotency_replay_served_from_cache():
    client, session, _ = make_client([200, 200], max_retries=1)
    first = client.request("POST", "http://x", idempotency_key="k1")
    assert session.calls == 1
    assert getattr(first, "from_cache", False) is False

    second = client.request("POST", "http://x", idempotency_key="k1")
    assert session.calls == 1  # no second network call
    assert second.from_cache is True
    assert second.status_code == 200


def test_non_2xx_is_not_cached():
    client, session, _ = make_client([404, 200], max_retries=1)
    client.request("GET", "http://x", idempotency_key="k2")
    resp = client.request("GET", "http://x", idempotency_key="k2")
    assert session.calls == 2  # the 404 was not cached
    assert resp.status_code == 200


def test_json_file_cache_persists_across_instances(tmp_path):
    cache_file = str(tmp_path / "idem.json")

    session = FakeSession([200])
    client = ResilientClient(
        config=RetryConfig(jitter=False),
        session=session,
        cache=JsonFileCache(cache_file),
        sleeper=lambda _d: None,
    )
    client.request("GET", "http://x", idempotency_key="kf")
    assert session.calls == 1

    # A brand-new client + empty session: a cache miss would raise IndexError.
    empty_session = FakeSession([])
    client2 = ResilientClient(
        config=RetryConfig(jitter=False),
        session=empty_session,
        cache=JsonFileCache(cache_file),
        sleeper=lambda _d: None,
    )
    resp = client2.request("GET", "http://x", idempotency_key="kf")
    assert empty_session.calls == 0
    assert resp.from_cache is True


def test_malformed_cache_file_starts_empty(tmp_path):
    cache_file = tmp_path / "broken.json"
    cache_file.write_text("{ not valid json")
    cache = JsonFileCache(str(cache_file))
    assert cache.get("anything") is None


def test_compute_delay_jitter_is_bounded():
    client = ResilientClient(config=RetryConfig(base_delay=2.0, jitter=True))
    for _ in range(100):
        assert 0.0 <= client._compute_delay(0) <= 2.0


def test_compute_delay_respects_max_delay():
    client = ResilientClient(config=RetryConfig(base_delay=10.0, max_delay=5.0, jitter=False))
    assert client._compute_delay(3) == 5.0


def test_memory_cache_roundtrip():
    cache = MemoryCache()
    assert cache.get("missing") is None
