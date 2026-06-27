#!/usr/bin/env python3
"""
api_health_check.py

Simple API health checker for ops/cron usage.

- Checks one or multiple endpoints
- Supports expected HTTP status codes and optional JSON checks
- Emits single-line JSON logs (stdout)
- Exits non-zero when any check fails (cron/CI friendly)

HTTP is delegated to `retry_client.ResilientClient`, so retries/backoff are shared
with the rest of the toolkit. Transient failures (timeouts, connection errors and
retryable statuses 5xx/408/429) are retried; other unexpected statuses (e.g. 404)
are treated as a failed check without pointless retries.

Environment variables:
  TARGETS            Comma-separated list of URLs (or use URL + optional /health path)
  URL                Single URL (alternative to TARGETS)
  METHOD             HTTP method (default: GET)
  TIMEOUT_SECONDS    Request timeout in seconds (default: 5)
  RETRIES            Number of retries on transient failure (default: 1)
  RETRY_DELAY_MS     Base backoff delay in ms (default: 250)
  EXPECT_STATUS      Expected status code(s), comma-separated (default: 200,204)
  EXPECT_JSON        Optional JSON validation rules (default: empty)
                     Format: "key=value,key2=value2" (top-level keys only)
                     Example: "status=ok,healthy=true"
  HEADER_AUTH        Optional Authorization header value (e.g. "Bearer xxx")
  USER_AGENT         User-Agent header (default: python-ops-tools/1.0)
  INSECURE_TLS       "1" to skip TLS verification (default: 0)  [use with caution]
  FOLLOW_REDIRECTS   "1" to follow redirects (default: 1)

Examples:
  URL="https://api.example.com/health" python3 api_health_check.py

  TARGETS="https://a.com/health,https://b.com/health" \
  EXPECT_STATUS="200" \
  EXPECT_JSON="status=ok" \
  python3 api_health_check.py

Exit codes:
  0  all checks ok
  1  one or more checks failed (ran but found problems)
  2  misconfiguration (missing URL/TARGETS, invalid EXPECT_* etc.)
"""

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import requests

import oplog
from retry_client import ResilientClient, RetryConfig

# Process exit codes (see module docstring).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

# Output mode for log_json; defaults to JSON, switchable via LOG_JSON (set in main).
_JSON_MODE = True


def log_json(level: str, event: str, **fields: Any) -> None:
    """Emit a structured log record (JSON by default; human text when LOG_JSON=0)."""
    oplog.log(level, event, as_json=_JSON_MODE, **fields)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got: {raw!r}") from None


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip() in {"1", "true", "TRUE", "yes", "YES", "on", "ON"}


def parse_csv_set(raw: str, default: list[int]) -> list[int]:
    if not raw or raw.strip() == "":
        return default
    out: list[int] = []
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        if not p.isdigit():
            raise ValueError(f"EXPECT_STATUS must be numeric codes, got: {p!r}")
        out.append(int(p))
    return out or default


def parse_expect_json(raw: str) -> dict[str, str]:
    """
    Parses "key=value,key2=value2" into a dict. Values are kept as strings,
    but the checker will compare against JSON primitives conservatively.
    """
    out: dict[str, str] = {}
    if not raw or raw.strip() == "":
        return out
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"EXPECT_JSON must be key=value pairs, got: {pair!r}")
        k, v = pair.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            raise ValueError(f"EXPECT_JSON has empty key in: {pair!r}")
        out[k] = v
    return out


def coerce_expected_value(v: str) -> bool | int | float | str | None:
    """
    Coerce "true"/"false"/"null"/numbers to comparable Python types.
    Otherwise keep as string.
    """
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null":
        return None
    # Try int then float
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def validate_json(body_text: str, rules: dict[str, str]) -> tuple[bool, str]:
    if not rules:
        return True, ""
    try:
        data = json.loads(body_text)
    except json.JSONDecodeError as e:
        return False, f"invalid_json: {e}"

    if not isinstance(data, dict):
        return False, "json_not_object"

    for key, expected_raw in rules.items():
        if key not in data:
            return False, f"missing_key:{key}"
        expected = coerce_expected_value(expected_raw)
        actual = data.get(key)

        # Conservative comparison: strict equality
        if actual != expected:
            return False, f"mismatch:{key}:expected={expected!r}:actual={actual!r}"

    return True, ""


@dataclass
class Config:
    """Resolved settings for a health-check run."""

    targets: list[str]
    method: str
    timeout_seconds: int
    retries: int
    retry_delay_ms: int
    insecure_tls: bool
    follow_redirects: bool
    expect_status: list[int]
    expect_json_rules: dict[str, str]
    headers: dict[str, str] = field(default_factory=dict)


def read_config() -> Config:
    """
    Read and validate settings from the environment.

    Raises ValueError on misconfiguration (missing URL/TARGETS, invalid EXPECT_*,
    non-integer numeric vars), which main() maps to EXIT_CONFIG.
    """
    url = (os.getenv("URL") or "").strip()
    targets_raw = (os.getenv("TARGETS") or "").strip()

    targets: list[str] = []
    if targets_raw:
        targets = [t.strip() for t in targets_raw.split(",") if t.strip()]
    elif url:
        targets = [url]
    if not targets:
        raise ValueError("URL or TARGETS is required")

    headers: dict[str, str] = {"User-Agent": os.getenv("USER_AGENT", "python-ops-tools/1.0")}
    auth = os.getenv("HEADER_AUTH")
    if auth and auth.strip():
        headers["Authorization"] = auth.strip()

    return Config(
        targets=targets,
        method=(os.getenv("METHOD") or "GET").strip().upper(),
        timeout_seconds=env_int("TIMEOUT_SECONDS", 5),
        retries=env_int("RETRIES", 1),
        retry_delay_ms=env_int("RETRY_DELAY_MS", 250),
        insecure_tls=env_bool("INSECURE_TLS", False),
        follow_redirects=env_bool("FOLLOW_REDIRECTS", True),
        expect_status=parse_csv_set(os.getenv("EXPECT_STATUS", ""), [200, 204]),
        expect_json_rules=parse_expect_json(os.getenv("EXPECT_JSON", "")),
        headers=headers,
    )


def build_client(config: Config) -> ResilientClient:
    """Create a ResilientClient tuned from the health-check config."""
    retry_config = RetryConfig(
        max_retries=config.retries,
        base_delay=max(0, config.retry_delay_ms) / 1000.0,
        # Deterministic backoff for a health check; jitter matters for fleets.
        jitter=False,
    )
    return ResilientClient(
        config=retry_config,
        timeout=config.timeout_seconds,
        logger=lambda msg: log_json("debug", "client", msg=msg),
    )


def _suppress_insecure_tls_warning() -> None:
    """Silence urllib3's warning when TLS verification is intentionally disabled."""
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass


def check_target(config: Config, client: ResilientClient, target: str) -> bool:
    """Run one endpoint check, log the outcome, and return True when healthy."""
    start = time.time()
    try:
        resp: Any = client.request(
            config.method,
            target,
            headers=config.headers,
            verify=not config.insecure_tls,
            allow_redirects=config.follow_redirects,
        )
    except requests.RequestException as e:
        duration_ms = int((time.time() - start) * 1000)
        log_json(
            "error",
            "endpoint_unhealthy",
            url=target,
            status=None,
            duration_ms=duration_ms,
            error=f"{type(e).__name__}: {e}",
        )
        return False

    duration_ms = int((time.time() - start) * 1000)
    status = resp.status_code
    status_ok = status in config.expect_status

    json_ok, json_reason = True, ""
    if status_ok and config.expect_json_rules:
        json_ok, json_reason = validate_json(resp.text, config.expect_json_rules)

    if status_ok and json_ok:
        log_json("info", "check_ok", url=target, status=status, duration_ms=duration_ms)
        return True

    if not status_ok:
        reason = f"unexpected_status:{status}"
    else:
        reason = f"json_check_failed:{json_reason}"
    snippet = resp.text[:300].replace("\n", "\\n")
    log_json(
        "error",
        "endpoint_unhealthy",
        url=target,
        status=status,
        duration_ms=duration_ms,
        reason=reason,
        body_snippet=snippet,
    )
    return False


def run_checks(config: Config, client: ResilientClient) -> bool:
    """Check every target. Returns True if any check failed."""
    any_failed = False
    for target in config.targets:
        if not check_target(config, client, target):
            any_failed = True
    return any_failed


def main(argv: list[str] | None = None) -> int:
    global _JSON_MODE
    _JSON_MODE = oplog.want_json(default=True)
    try:
        config = read_config()
    except ValueError as e:
        log_json("error", "config_error", error=str(e))
        return EXIT_CONFIG

    if config.insecure_tls:
        _suppress_insecure_tls_warning()

    log_json(
        "info",
        "run_start",
        targets=config.targets,
        method=config.method,
        timeout_seconds=config.timeout_seconds,
        retries=config.retries,
        expect_status=config.expect_status,
        expect_json=config.expect_json_rules,
        insecure_tls=config.insecure_tls,
        follow_redirects=config.follow_redirects,
    )

    client = build_client(config)
    any_failed = run_checks(config, client)

    if any_failed:
        log_json("error", "run_done", result="failed")
        return EXIT_FAILED

    log_json("info", "run_done", result="ok")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
