#!/usr/bin/env python3
"""
oplog.py

Tiny shared logging helper for the ops tools, supporting two output modes:

- structured single-line JSON (for log aggregation)
- human-readable text

Tools that have always emitted JSON (`backup_all_dbs.py`, `api_health_check.py`)
default to JSON and can be switched to text with `LOG_JSON=0`. Tools that print a
human report can opt into a machine-readable summary line via a `--json` flag or
the `LOG_JSON` env var.

This is a deliberately small module (no logging framework) shared across the
otherwise self-contained scripts.
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import IO, Any

_TRUTHY = {"1", "true", "TRUE", "yes", "YES", "on", "ON"}
_FALSY = {"0", "false", "FALSE", "no", "NO", "off", "OFF"}


def utc_ts() -> str:
    """Current UTC time as an ISO-8601 string with a trailing Z."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def want_json(cli_flag: bool = False, *, default: bool = False) -> bool:
    """
    Decide whether to emit JSON.

    Precedence: an explicit ``--json`` CLI flag wins; otherwise the ``LOG_JSON``
    env var (truthy/falsy) decides; otherwise ``default`` applies.
    """
    if cli_flag:
        return True
    raw = os.getenv("LOG_JSON")
    if raw is None or raw.strip() == "":
        return default
    token = raw.strip()
    if token in _TRUTHY:
        return True
    if token in _FALSY:
        return False
    return default


def log(
    level: str,
    event: str,
    *,
    as_json: bool,
    stream: IO[str] | None = None,
    **fields: Any,
) -> None:
    """Emit one log record as JSON or as a short human line."""
    out = stream if stream is not None else sys.stdout
    if as_json:
        payload = {"ts": utc_ts(), "level": level, "event": event, **fields}
        print(json.dumps(payload, ensure_ascii=False), file=out)
    else:
        extra = " ".join(f"{k}={v}" for k, v in fields.items())
        line = f"[{level}] {event}" + (f" {extra}" if extra else "")
        print(line, file=out)
