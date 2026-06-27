"""Unit tests for api_health_check helpers (no network required)."""

import ssl

import pytest

from api_health_check import (
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_OK,
    build_ssl_context,
    coerce_expected_value,
    main,
    parse_csv_set,
    parse_expect_json,
    read_config,
    validate_json,
)


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


def test_build_ssl_context_toggle():
    assert build_ssl_context(False) is None
    ctx = build_ssl_context(True)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


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


def test_main_missing_target_returns_config_error(monkeypatch):
    _clear_targets(monkeypatch)
    assert main() == EXIT_CONFIG


def test_main_invalid_expect_status_returns_config_error(monkeypatch):
    _clear_targets(monkeypatch)
    monkeypatch.setenv("URL", "https://a.com")
    monkeypatch.setenv("EXPECT_STATUS", "abc")
    assert main() == EXIT_CONFIG


def test_main_success_path(monkeypatch):
    _clear_targets(monkeypatch)
    monkeypatch.setenv("URL", "https://a.com")
    monkeypatch.setenv("RETRIES", "0")
    monkeypatch.setattr("api_health_check.request_once", lambda **kw: (200, b"{}", None))
    assert main() == EXIT_OK


def test_main_failure_path(monkeypatch):
    _clear_targets(monkeypatch)
    monkeypatch.setenv("URL", "https://a.com")
    monkeypatch.setenv("RETRIES", "0")
    monkeypatch.setattr(
        "api_health_check.request_once", lambda **kw: (None, None, "URLError: boom")
    )
    assert main() == EXIT_FAILED
