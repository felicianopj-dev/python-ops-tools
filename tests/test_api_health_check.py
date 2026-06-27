"""Unit tests for api_health_check (no network required)."""

import pytest
import requests

from api_health_check import (
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_OK,
    Config,
    check_target,
    coerce_expected_value,
    main,
    parse_csv_set,
    parse_expect_json,
    read_config,
    run_checks,
    validate_json,
)
from retry_client import ResilientClient, RetryConfig


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_parse_csv_set_parses_and_falls_back_to_default():
    assert parse_csv_set("200,204", [500]) == [200, 204]
    assert parse_csv_set("", [200, 204]) == [200, 204]
    assert parse_csv_set("   ", [200]) == [200]


def test_parse_csv_set_rejects_non_numeric():
    with pytest.raises(ValueError):
        parse_csv_set("200,abc", [200])


def test_parse_expect_json_parses_pairs():
    assert parse_expect_json("status=ok,healthy=true") == {"status": "ok", "healthy": "true"}
    assert parse_expect_json("") == {}


def test_parse_expect_json_rejects_malformed():
    with pytest.raises(ValueError):
        parse_expect_json("statusok")
    with pytest.raises(ValueError):
        parse_expect_json("=value")


def test_coerce_expected_value_types():
    assert coerce_expected_value("true") is True
    assert coerce_expected_value("false") is False
    assert coerce_expected_value("null") is None
    assert coerce_expected_value("42") == 42
    assert coerce_expected_value("3.14") == 3.14
    assert coerce_expected_value("ok") == "ok"


def test_validate_json_ok_and_failures():
    assert validate_json('{"status": "ok"}', {"status": "ok"}) == (True, "")
    assert validate_json("{}", {}) == (True, "")

    ok, reason = validate_json('{"status": "ok"}', {"missing": "x"})
    assert ok is False and reason == "missing_key:missing"

    ok, reason = validate_json('{"status": "down"}', {"status": "ok"})
    assert ok is False and reason.startswith("mismatch:status")

    ok, reason = validate_json("not json", {"status": "ok"})
    assert ok is False and reason.startswith("invalid_json")

    ok, reason = validate_json("[1, 2]", {"status": "ok"})
    assert ok is False and reason == "json_not_object"


def test_validate_json_coerces_booleans():
    assert validate_json('{"healthy": true}', {"healthy": "true"}) == (True, "")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _clear_targets(monkeypatch):
    monkeypatch.delenv("URL", raising=False)
    monkeypatch.delenv("TARGETS", raising=False)


def test_read_config_requires_target(monkeypatch):
    _clear_targets(monkeypatch)
    with pytest.raises(ValueError):
        read_config()


def test_read_config_builds_headers_and_targets(monkeypatch):
    _clear_targets(monkeypatch)
    monkeypatch.setenv("TARGETS", "https://a.com, https://b.com")
    monkeypatch.setenv("HEADER_AUTH", "Bearer xyz")
    config = read_config()
    assert config.targets == ["https://a.com", "https://b.com"]
    assert config.headers["Authorization"] == "Bearer xyz"


# --------------------------------------------------------------------------- #
# Check logic via an injected fake session (no real HTTP)
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class FakeSession:
    """Minimal stand-in for requests.Session. Returns/raises a fixed result."""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _config(**overrides) -> Config:
    base = dict(
        targets=["https://svc/health"],
        method="GET",
        timeout_seconds=5,
        retries=0,
        retry_delay_ms=0,
        insecure_tls=False,
        follow_redirects=True,
        expect_status=[200, 204],
        expect_json_rules={},
        headers={},
    )
    base.update(overrides)
    return Config(**base)


def _client(result) -> ResilientClient:
    return ResilientClient(
        config=RetryConfig(max_retries=0),
        session=FakeSession(result),
        sleeper=lambda _s: None,
    )


def test_check_target_healthy():
    assert check_target(_config(), _client(FakeResponse(200, "{}")), "https://svc/health") is True


def test_check_target_unexpected_status():
    cfg = _config()
    assert check_target(cfg, _client(FakeResponse(503, "")), "https://svc/health") is False


def test_check_target_json_mismatch():
    cfg = _config(expect_json_rules={"status": "ok"})
    bad = _client(FakeResponse(200, '{"status": "down"}'))
    assert check_target(cfg, bad, "https://svc/health") is False


def test_check_target_transport_error():
    cfg = _config()
    boom = _client(requests.ConnectionError("refused"))
    assert check_target(cfg, boom, "https://svc/health") is False


def test_run_checks_aggregates_failures():
    cfg = _config(targets=["https://a", "https://b"])
    assert run_checks(cfg, _client(FakeResponse(200, "{}"))) is False
    assert run_checks(cfg, _client(FakeResponse(500, ""))) is True


# --------------------------------------------------------------------------- #
# main() exit codes
# --------------------------------------------------------------------------- #
def test_main_missing_target_returns_config_error(monkeypatch):
    _clear_targets(monkeypatch)
    assert main() == EXIT_CONFIG


def test_main_invalid_expect_status_returns_config_error(monkeypatch):
    _clear_targets(monkeypatch)
    monkeypatch.setenv("URL", "https://a.com")
    monkeypatch.setenv("EXPECT_STATUS", "abc")
    assert main() == EXIT_CONFIG


def test_main_success_and_failure_paths(monkeypatch):
    _clear_targets(monkeypatch)
    monkeypatch.setenv("URL", "https://a.com")
    monkeypatch.setenv("RETRIES", "0")

    monkeypatch.setattr("api_health_check.build_client", lambda cfg: _client(FakeResponse(200, "{}")))
    assert main() == EXIT_OK

    monkeypatch.setattr("api_health_check.build_client", lambda cfg: _client(FakeResponse(404, "")))
    assert main() == EXIT_FAILED
