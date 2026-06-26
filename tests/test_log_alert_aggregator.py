"""Unit tests for the log alert aggregator."""

import json

from log_alert_aggregator import (
    aggregate,
    build_summary,
    canonical_severity,
    iter_log_files,
    normalize_message_key,
    parse_line,
    scan_files,
)


def test_canonical_severity_aliases():
    assert canonical_severity("error") == "ERROR"
    assert canonical_severity("FATAL") == "CRITICAL"
    assert canonical_severity("warn") == "WARNING"
    assert canonical_severity("info") is None
    assert canonical_severity(None) is None


def test_parse_line_json_record():
    line = json.dumps(
        {
            "level": "error",
            "message": "boom",
            "timestamp": "2026-01-01T00:00:00Z",
            "service": "svc-a",
        }
    )
    rec = parse_line(line, "fallback")
    assert rec is not None
    assert rec["severity"] == "ERROR"
    assert rec["service"] == "svc-a"  # JSON service wins over fallback
    assert rec["message"] == "boom"


def test_parse_line_json_info_is_ignored():
    line = json.dumps({"level": "info", "message": "ok"})
    assert parse_line(line, "svc") is None


def test_parse_line_plain_text_with_timestamp():
    rec = parse_line("2026-06-26 09:07:44 ERROR gateway timeout", "payments")
    assert rec is not None
    assert rec["severity"] == "ERROR"
    assert rec["service"] == "payments"  # falls back to file-derived service
    assert rec["timestamp"] == "2026-06-26 09:07:44"


def test_parse_line_plain_text_without_severity():
    assert parse_line("2026-06-26 09:00:00 INFO all good", "svc") is None


def test_parse_line_blank():
    assert parse_line("   ", "svc") is None


def test_normalize_message_key_collapses_ids_and_numbers():
    a = normalize_message_key("Payment gateway timeout for TXN-1002 after 5000ms")
    b = normalize_message_key("Payment gateway timeout for TXN-9999 after 250ms")
    assert a == b  # variable parts masked -> same grouping key


def test_aggregate_counts_and_timestamps():
    records = [
        {
            "service": "p",
            "severity": "ERROR",
            "message": "timeout id 1",
            "timestamp": "2026-06-26 09:02:00",
        },
        {
            "service": "p",
            "severity": "ERROR",
            "message": "timeout id 2",
            "timestamp": "2026-06-26 09:08:00",
        },
        {
            "service": "p",
            "severity": "CRITICAL",
            "message": "disk full",
            "timestamp": "2026-06-26 09:10:00",
        },
    ]
    groups = aggregate(records)
    # The two timeouts collapse into one group with count 2.
    timeout_groups = [g for g in groups.values() if g["severity"] == "ERROR"]
    assert len(timeout_groups) == 1
    g = timeout_groups[0]
    assert g["count"] == 2
    assert g["first_ts"] == "2026-06-26 09:02:00"
    assert g["last_ts"] == "2026-06-26 09:08:00"
    # CRITICAL is ordered before ERROR.
    severities = [grp["severity"] for grp in groups.values()]
    assert severities[0] == "CRITICAL"


def test_build_summary_clean_when_empty():
    summary = build_summary(
        aggregate([]), scanned_files=2, include_warnings=False, top=3, max_msg_len=200
    )
    assert "no high-severity entries found" in summary


def test_build_summary_includes_counts_and_services():
    records = [
        {"service": "auth", "severity": "ERROR", "message": "bad token", "timestamp": "t1"},
        {"service": "pay", "severity": "CRITICAL", "message": "kms down", "timestamp": "t2"},
    ]
    summary = build_summary(
        aggregate(records),
        scanned_files=2,
        include_warnings=False,
        top=5,
        max_msg_len=200,
    )
    assert "CRITICAL: 1" in summary
    assert "ERROR: 1" in summary
    assert "auth" in summary and "pay" in summary


def test_iter_log_files_expands_directory(tmp_path):
    (tmp_path / "a.log").write_text("x")
    (tmp_path / "b.log").write_text("y")
    (tmp_path / "c.txt").write_text("z")
    files = iter_log_files([str(tmp_path)], pattern="*.log", recursive=False)
    assert len(files) == 2
    assert all(f.endswith(".log") for f in files)


def test_iter_log_files_skips_missing_paths(tmp_path):
    (tmp_path / "a.log").write_text("x")
    files = iter_log_files(
        [str(tmp_path / "a.log"), "/no/such/path.log"], pattern="*.log", recursive=False
    )
    assert len(files) == 1


def test_scan_files_severity_gating(tmp_path):
    log = tmp_path / "svc.log"
    log.write_text(
        "2026-01-01 00:00:00 INFO ok\n"
        "2026-01-01 00:00:01 WARNING slow\n"
        "2026-01-01 00:00:02 ERROR boom\n"
    )
    without = scan_files([str(log)], include_warnings=False)
    assert {r["severity"] for r in without} == {"ERROR"}

    with_warn = scan_files([str(log)], include_warnings=True)
    assert {r["severity"] for r in with_warn} == {"ERROR", "WARNING"}
