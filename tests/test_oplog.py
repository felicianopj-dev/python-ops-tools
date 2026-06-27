"""Unit tests for the shared oplog helper."""

import json
import re

import oplog


def test_utc_ts_format():
    ts = oplog.utc_ts()
    assert ts.endswith("Z")
    # YYYY-MM-DDTHH:MM:SSZ
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", ts)


def test_want_json_cli_flag_wins(monkeypatch):
    monkeypatch.delenv("LOG_JSON", raising=False)
    assert oplog.want_json(True) is True
    assert oplog.want_json(False) is False
    assert oplog.want_json(False, default=True) is True


def test_want_json_env_overrides_default(monkeypatch):
    monkeypatch.setenv("LOG_JSON", "1")
    assert oplog.want_json(False, default=False) is True
    monkeypatch.setenv("LOG_JSON", "0")
    assert oplog.want_json(False, default=True) is False
    monkeypatch.setenv("LOG_JSON", "")
    assert oplog.want_json(False, default=True) is True


def test_log_json_mode(capsys):
    oplog.log("info", "run_done", as_json=True, result="ok", count=3)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["level"] == "info"
    assert payload["event"] == "run_done"
    assert payload["result"] == "ok"
    assert payload["count"] == 3
    assert payload["ts"].endswith("Z")


def test_log_human_mode(capsys):
    oplog.log("warn", "check_failed", as_json=False, url="https://x", status=503)
    out = capsys.readouterr().out.strip()
    assert out == "[warn] check_failed url=https://x status=503"
