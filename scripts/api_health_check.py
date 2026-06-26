#!/usr/bin/env python3
"""
api_health_check.py

Simple API health checker for ops/cron usage.

- Checks one or multiple endpoints
- Supports expected HTTP status codes and optional JSON checks
- Emits single-line JSON logs (stdout)
- Exits non-zero when any check fails (cron/CI friendly)

Environment variables:
  TARGETS            Comma-separated list of URLs (or use URL + optional /health path)
  URL                Single URL (alternative to TARGETS)
  METHOD             HTTP method (default: GET)
  TIMEOUT_SECONDS    Request timeout in seconds (default: 5)
  RETRIES            Number of retries on failure (default: 1)
  RETRY_DELAY_MS     Delay between retries in ms (default: 250)
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
  2  one or more checks failed
  3  misconfiguration (missing URL/TARGETS, invalid EXPECT_* etc.)
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log_json(level: str, event: str, **fields) -> None:
    payload = {"ts": utc_ts(), "level": level, "event": event, **fields}
    print(json.dumps(payload, ensure_ascii=False))


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


def build_ssl_context(insecure_tls: bool) -> ssl.SSLContext | None:
    if not insecure_tls:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def coerce_expected_value(v: str):
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


def safe_decode_body(data: bytes, content_type: str) -> str:
    # Prefer utf-8; fallback to latin-1 to avoid crashes on weird payloads
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def request_once(
    url: str,
    method: str,
    headers: dict[str, str],
    timeout_seconds: int,
    follow_redirects: bool,
    ssl_context: ssl.SSLContext | None,
) -> tuple[int | None, bytes | None, str | None]:
    """
    Returns: (status_code, body_bytes, error_string)
    """
    req = urllib.request.Request(url=url, method=method.upper(), headers=headers)

    handlers = []

    # Attach TLS context via HTTPSHandler (opener.open doesn't accept `context=...`)
    if ssl_context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ssl_context))

    if not follow_redirects:
        # Disable redirects by installing a redirect handler that raises.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                raise urllib.error.HTTPError(req.full_url, code, "redirect_disabled", hdrs, fp)

        handlers.append(NoRedirect())

    opener = urllib.request.build_opener(*handlers)

    try:
        with opener.open(req, timeout=timeout_seconds) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read()
            return status, body, None

    except urllib.error.HTTPError as e:
        # HTTPError still has a status code and body
        try:
            body = e.read() if hasattr(e, "read") else b""
        except Exception:
            body = b""
        return int(getattr(e, "code", 0) or 0), body, f"HTTPError: {e}"

    except urllib.error.URLError as e:
        return None, None, f"URLError: {e}"

    except Exception as e:
        return None, None, f"Error: {e}"


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


def main() -> int:
    url = (os.getenv("URL") or "").strip()
    targets_raw = (os.getenv("TARGETS") or "").strip()
    method = (os.getenv("METHOD") or "GET").strip().upper()

    try:
        timeout_seconds = env_int("TIMEOUT_SECONDS", 5)
        retries = env_int("RETRIES", 1)
        retry_delay_ms = env_int("RETRY_DELAY_MS", 250)
        insecure_tls = env_bool("INSECURE_TLS", False)
        follow_redirects = env_bool("FOLLOW_REDIRECTS", True)
        expect_status = parse_csv_set(os.getenv("EXPECT_STATUS", ""), [200, 204])
        expect_json_rules = parse_expect_json(os.getenv("EXPECT_JSON", ""))
    except ValueError as e:
        log_json("error", "config_error", error=str(e))
        return 3

    # Resolve targets
    targets: list[str] = []
    if targets_raw:
        targets = [t.strip() for t in targets_raw.split(",") if t.strip()]
    elif url:
        targets = [url]

    if not targets:
        log_json("error", "config_error", error="URL or TARGETS is required")
        return 3

    # Headers
    user_agent = os.getenv("USER_AGENT", "python-ops-tools/1.0")
    headers: dict[str, str] = {"User-Agent": user_agent}

    auth = os.getenv("HEADER_AUTH")
    if auth and auth.strip():
        headers["Authorization"] = auth.strip()

    ssl_context = build_ssl_context(insecure_tls)

    log_json(
        "info",
        "run_start",
        targets=targets,
        method=method,
        timeout_seconds=timeout_seconds,
        retries=retries,
        expect_status=expect_status,
        expect_json=expect_json_rules,
        insecure_tls=insecure_tls,
        follow_redirects=follow_redirects,
    )

    any_failed = False

    for target in targets:
        attempt = 0
        ok = False
        last_err = ""
        last_status: int | None = None
        last_body_snippet = ""

        while attempt <= retries:
            attempt += 1
            start = time.time()

            status, body, err = request_once(
                url=target,
                method=method,
                headers=headers,
                timeout_seconds=timeout_seconds,
                follow_redirects=follow_redirects,
                ssl_context=ssl_context,
            )

            duration_ms = int((time.time() - start) * 1000)
            last_status = status
            last_err = err or ""

            content_type = ""
            body_text = ""
            if body is not None:
                # Best-effort content-type inference for decoding
                # (urllib doesn't easily expose headers on errors consistently)
                content_type = "application/octet-stream"
                body_text = safe_decode_body(body, content_type)
                last_body_snippet = body_text[:300].replace("\n", "\\n")

            status_ok = status in expect_status if status is not None else False
            json_ok, json_reason = True, ""
            if status_ok and expect_json_rules:
                json_ok, json_reason = validate_json(body_text, expect_json_rules)

            ok = bool(status_ok and json_ok and not err)

            if ok:
                log_json(
                    "info",
                    "check_ok",
                    url=target,
                    status=status,
                    duration_ms=duration_ms,
                )
                break

            reason = ""
            if err:
                reason = err
            elif not status_ok:
                reason = f"unexpected_status:{status}"
            elif not json_ok:
                reason = f"json_check_failed:{json_reason}"
            else:
                reason = "unknown_failure"

            log_json(
                "warn",
                "check_failed",
                url=target,
                status=status,
                duration_ms=duration_ms,
                attempt=attempt,
                retries=retries,
                reason=reason,
                body_snippet=last_body_snippet,
            )

            if attempt <= retries:
                time.sleep(max(0, retry_delay_ms) / 1000.0)

        if not ok:
            any_failed = True
            log_json(
                "error",
                "endpoint_unhealthy",
                url=target,
                status=last_status,
                error=last_err,
            )

    if any_failed:
        log_json("error", "run_done", result="failed")
        return 2

    log_json("info", "run_done", result="ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
