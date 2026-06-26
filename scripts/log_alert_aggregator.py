#!/usr/bin/env python3
"""
log_alert_aggregator.py

Scan log files from multiple services/directories for high-severity entries
(ERROR/CRITICAL, optionally WARNING) and send a consolidated summary to a chat
webhook.

The payload is Slack-compatible (a JSON object with a "text" field), but the
webhook URL is configurable via the WEBHOOK_URL environment variable, so it also
works with Discord and most generic webhook receivers that accept a "text" body.

Supported log formats:
  - Structured JSON / JSON-lines: one JSON object per line. Severity, message,
    timestamp and service are read from common keys
    (level/levelname/severity, message/msg, timestamp/time/ts/asctime,
    service/logger/name).
  - Plain text: any line containing a CRITICAL/ERROR/WARNING keyword. A leading
    timestamp is extracted with a tolerant regex when present.

Records are grouped by (service, severity, normalized message) so that the same
error with varying ids/numbers collapses into a single counted group, with first
and last occurrence timestamps.

Configuration:
  WEBHOOK_URL   Target webhook URL. Required unless --dry-run is used.
                Never hardcode this; it is read from the environment only.

CLI examples:
  # Dry-run against the bundled sample logs (prints instead of sending)
  python3 scripts/log_alert_aggregator.py data/sample_logs --dry-run

  # Scan specific files and POST to a webhook
  WEBHOOK_URL='https://hooks.slack.com/services/XXX' \
    python3 scripts/log_alert_aggregator.py /var/log/app/api.log /var/log/app/worker.log

  # Recurse a directory, include warnings, show more samples per group
  WEBHOOK_URL='https://discord.com/api/webhooks/XXX' \
    python3 scripts/log_alert_aggregator.py /var/log -r --include-warnings --top 5

Exit codes:
  0  ran successfully (summary sent, or printed in --dry-run)
  2  configuration error (missing WEBHOOK_URL without --dry-run, no valid paths)
  3  webhook send failure
"""

import argparse
import json
import os
import re
import sys
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

# Severities we know about, ordered from most to least severe.
SEVERITY_ORDER = ["CRITICAL", "ERROR", "WARNING"]
SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITY_ORDER)}

# Process exit codes (see module docstring).
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_SEND = 3

# Keys commonly used in structured logs.
LEVEL_KEYS = ("level", "levelname", "severity", "lvl")
MESSAGE_KEYS = ("message", "msg", "log", "event")
TIMESTAMP_KEYS = ("timestamp", "time", "ts", "asctime", "@timestamp", "datetime")
SERVICE_KEYS = ("service", "logger", "name", "app", "component")

# Detect a severity keyword as a whole word in a plain-text line.
SEVERITY_RE = re.compile(r"\b(CRITICAL|FATAL|ERROR|WARNING|WARN)\b")

# Tolerant leading-timestamp matcher: ISO-ish "2026-06-26T12:00:00" /
# "2026-06-26 12:00:00" and syslog-ish "Jun 26 12:00:00".
TIMESTAMP_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
    r"|([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"
)

# Tokens replaced when normalizing a message into a grouping key, so that the
# same error with different ids/numbers collapses into one group.
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
HEX_RE = re.compile(r"\b0x[0-9a-f]+\b", re.I)
NUM_RE = re.compile(r"\b\d+\b")


def warn(message: str) -> None:
    """Print a warning to stderr without interrupting the scan."""
    print(f"[warn] {message}", file=sys.stderr)


def canonical_severity(raw: Optional[str]) -> Optional[str]:
    """
    Map a raw level string to one of CRITICAL/ERROR/WARNING.

    Aliases FATAL->CRITICAL and WARN->WARNING. Returns None when the value is
    not a severity we track.
    """
    if not raw:
        return None
    value = str(raw).strip().upper()
    if value in ("CRITICAL", "FATAL"):
        return "CRITICAL"
    if value == "ERROR":
        return "ERROR"
    if value in ("WARNING", "WARN"):
        return "WARNING"
    return None


def iter_log_files(paths: List[str], pattern: str, recursive: bool) -> List[str]:
    """
    Expand the given paths (files or directories) into a concrete list of files.

    Directories are globbed with `pattern` (recursively when `recursive` is set).
    Missing paths produce a warning and are skipped rather than aborting.
    """
    import glob

    files: List[str] = []
    seen = set()

    def add(path: str) -> None:
        real = os.path.abspath(path)
        if real not in seen and os.path.isfile(path):
            seen.add(real)
            files.append(path)

    for path in paths:
        if os.path.isdir(path):
            if recursive:
                matches = glob.glob(
                    os.path.join(path, "**", pattern), recursive=True
                )
            else:
                matches = glob.glob(os.path.join(path, pattern))
            if not matches:
                warn(f"no files matching '{pattern}' under directory: {path}")
            for match in sorted(matches):
                add(match)
        elif os.path.isfile(path):
            add(path)
        else:
            warn(f"path not found, skipping: {path}")

    return files


def service_name_for(path: str) -> str:
    """Derive a default service label from a file's name (without extension)."""
    base = os.path.basename(path)
    stem, _ = os.path.splitext(base)
    return stem or base


def _first_present(data: dict, keys: Tuple[str, ...]) -> Optional[str]:
    """Return the first key's value present in `data`, as a string."""
    for key in keys:
        if key in data and data[key] is not None:
            return str(data[key])
    return None


def parse_line(raw: str, default_service: str) -> Optional[Dict[str, str]]:
    """
    Parse a single log line into a normalized record.

    Tries JSON first; falls back to plain-text severity detection. Returns a dict
    with keys {service, severity, message, timestamp}, or None when the line has
    no severity we track (or is blank/unparseable as a log entry).
    """
    line = raw.strip()
    if not line:
        return None

    # Try structured JSON first.
    if line.startswith("{"):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            severity = canonical_severity(_first_present(obj, LEVEL_KEYS))
            if severity is None:
                return None
            message = _first_present(obj, MESSAGE_KEYS) or line
            timestamp = _first_present(obj, TIMESTAMP_KEYS) or ""
            service = _first_present(obj, SERVICE_KEYS) or default_service
            return {
                "service": service,
                "severity": severity,
                "message": message.strip(),
                "timestamp": timestamp.strip(),
            }
        # Not a JSON object; fall through to plain-text handling.

    # Plain text: look for a severity keyword.
    match = SEVERITY_RE.search(line.upper())
    if not match:
        return None
    severity = canonical_severity(match.group(1))
    if severity is None:
        return None

    ts_match = TIMESTAMP_RE.search(line)
    timestamp = (ts_match.group(0) if ts_match else "").strip()

    return {
        "service": default_service,
        "severity": severity,
        "message": line,
        "timestamp": timestamp,
    }


def normalize_message_key(message: str) -> str:
    """
    Reduce a message to a stable grouping key by masking variable parts
    (UUIDs, hex addresses, numbers) so similar errors collapse together.
    """
    key = UUID_RE.sub("<uuid>", message)
    key = HEX_RE.sub("<hex>", key)
    key = NUM_RE.sub("<n>", key)
    return " ".join(key.split())[:300]


def aggregate(records: List[Dict[str, str]]) -> "OrderedDict":
    """
    Group records by (service, severity, normalized message).

    Returns an OrderedDict keyed by that tuple, with each value holding count,
    first/last timestamp, and a representative sample message. Groups are sorted
    by severity (most severe first) then by descending count.
    """
    groups: Dict[Tuple[str, str, str], dict] = {}

    for rec in records:
        key = (rec["service"], rec["severity"], normalize_message_key(rec["message"]))
        ts = rec["timestamp"]
        group = groups.get(key)
        if group is None:
            groups[key] = {
                "service": rec["service"],
                "severity": rec["severity"],
                "count": 1,
                "first_ts": ts,
                "last_ts": ts,
                "sample": rec["message"],
            }
        else:
            group["count"] += 1
            # Track first/last by string comparison; ISO timestamps sort
            # correctly, and empty strings naturally sort lowest.
            if ts:
                if not group["first_ts"] or ts < group["first_ts"]:
                    group["first_ts"] = ts
                if ts > group["last_ts"]:
                    group["last_ts"] = ts

    ordered = OrderedDict(
        sorted(
            groups.items(),
            key=lambda kv: (SEVERITY_RANK.get(kv[1]["severity"], 99), -kv[1]["count"]),
        )
    )
    return ordered


def _truncate(text: str, max_len: int) -> str:
    """Collapse whitespace and truncate `text` to `max_len` characters."""
    flat = " ".join(text.split())
    if len(flat) <= max_len:
        return flat
    return flat[: max_len - 1].rstrip() + "…"


def build_summary(
    groups: "OrderedDict",
    scanned_files: int,
    include_warnings: bool,
    top: int,
    max_msg_len: int,
) -> str:
    """
    Build a readable text summary from aggregated groups.

    Shows total counts by severity, a per-service breakdown, and the top error
    groups (by count) with their occurrence window and a truncated sample.
    """
    severities = ["CRITICAL", "ERROR"] + (["WARNING"] if include_warnings else [])

    totals = {sev: 0 for sev in severities}
    per_service: Dict[str, Dict[str, int]] = {}
    for group in groups.values():
        sev = group["severity"]
        if sev not in totals:
            continue
        totals[sev] += group["count"]
        per_service.setdefault(group["service"], {s: 0 for s in severities})
        per_service[group["service"]][sev] += group["count"]

    total_issues = sum(totals.values())

    lines: List[str] = []
    header = "✅ Log Alert Summary — no high-severity entries found" if total_issues == 0 \
        else "🚨 Log Alert Summary"
    lines.append(header)
    counts_str = "  ".join(f"{sev}: {totals[sev]}" for sev in severities)
    lines.append(f"Scanned {scanned_files} file(s) — {counts_str}")

    if total_issues == 0:
        return "\n".join(lines)

    # Per-service breakdown.
    lines.append("")
    lines.append("By service:")
    for service in sorted(per_service):
        counts = per_service[service]
        if not any(counts.values()):
            continue
        parts = ", ".join(f"{sev} {counts[sev]}" for sev in severities if counts[sev])
        lines.append(f"  • {service}: {parts}")

    # Top error groups.
    relevant = [g for g in groups.values() if g["severity"] in totals]
    lines.append("")
    lines.append(f"Top {min(top, len(relevant))} issues:")
    for group in relevant[:top]:
        when = ""
        if group["first_ts"] or group["last_ts"]:
            first = group["first_ts"] or "?"
            last = group["last_ts"] or "?"
            when = f" (first: {first}, last: {last})"
        lines.append(
            f"  [{group['severity']}] {group['service']} ×{group['count']}{when}"
        )
        lines.append(f"      {_truncate(group['sample'], max_msg_len)}")

    return "\n".join(lines)


def build_payload(summary_text: str) -> dict:
    """
    Wrap the summary in a Slack-compatible JSON body.

    Slack and most generic receivers accept a top-level "text" field. Discord
    uses "content"; we include both so the same payload works across services.
    """
    return {"text": summary_text, "content": summary_text}


def send_webhook(url: str, payload: dict, timeout: int) -> None:
    """
    POST the payload to the webhook URL.

    Raises RuntimeError on connection problems or a non-2xx response.
    """
    try:
        import requests
    except ImportError:
        raise RuntimeError(
            "requests is required to send webhooks. "
            "Install it with: pip install -r requirements.txt"
        )

    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"webhook request failed: {e}")

    if not (200 <= resp.status_code < 300):
        body = (resp.text or "").strip()[:200]
        raise RuntimeError(
            f"webhook returned HTTP {resp.status_code}: {body or '<empty body>'}"
        )


def scan_files(files: List[str], include_warnings: bool) -> List[Dict[str, str]]:
    """
    Read every file and collect normalized high-severity records.

    Unreadable files produce a warning and are skipped. WARNING entries are
    dropped unless `include_warnings` is set.
    """
    records: List[Dict[str, str]] = []
    for path in files:
        service = service_name_for(path)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    rec = parse_line(raw, service)
                    if rec is None:
                        continue
                    if rec["severity"] == "WARNING" and not include_warnings:
                        continue
                    records.append(rec)
        except OSError as e:
            warn(f"could not read {path}: {e}")
    return records


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate ERROR/CRITICAL log entries and alert via webhook.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Log files or directories to scan.",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Recurse into subdirectories when a path is a directory.",
    )
    parser.add_argument(
        "--pattern",
        default="*.log",
        help="Glob used to select files when scanning a directory.",
    )
    parser.add_argument(
        "--include-warnings",
        action="store_true",
        help="Also include WARNING-level entries (default: ERROR/CRITICAL only).",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=3,
        help="Number of top issue groups to include in the summary.",
    )
    parser.add_argument(
        "--max-msg-len",
        type=int,
        default=200,
        help="Truncate each sample message to this many characters.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Webhook POST timeout in seconds.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the summary to stdout instead of sending it.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    webhook_url = (os.getenv("WEBHOOK_URL") or "").strip()
    if not args.dry_run and not webhook_url:
        print(
            "Error: WEBHOOK_URL environment variable is required "
            "(or use --dry-run to print instead).",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    files = iter_log_files(args.paths, args.pattern, args.recursive)
    if not files:
        print("Error: no readable log files found in the given paths.", file=sys.stderr)
        return EXIT_CONFIG

    records = scan_files(files, args.include_warnings)
    groups = aggregate(records)
    summary = build_summary(
        groups,
        scanned_files=len(files),
        include_warnings=args.include_warnings,
        top=args.top,
        max_msg_len=args.max_msg_len,
    )

    if args.dry_run:
        print(summary)
        return EXIT_OK

    payload = build_payload(summary)
    try:
        send_webhook(webhook_url, payload, args.timeout)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_SEND

    print(f"Summary sent to webhook ({len(files)} file(s) scanned).")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
