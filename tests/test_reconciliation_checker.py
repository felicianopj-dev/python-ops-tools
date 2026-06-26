"""Unit tests for the reconciliation checker."""

import csv
import json

import pytest

from reconciliation_checker import (
    RuntimeFailure,
    compare,
    index_by_key,
    load_json_records,
    normalize,
    write_csv_report,
)


def test_normalize_handles_types_and_whitespace():
    assert normalize(None) == ""
    assert normalize("  hi  ") == "hi"
    assert normalize(100) == "100"
    assert normalize(100.0) == "100.0"


def test_index_by_key_builds_lookup():
    records = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
    indexed = index_by_key(records, "id", "local")
    assert set(indexed) == {"a", "b"}
    assert indexed["a"]["v"] == 1


def test_index_by_key_rejects_duplicates():
    records = [{"id": "a"}, {"id": "a"}]
    with pytest.raises(RuntimeFailure, match="duplicate key"):
        index_by_key(records, "id", "local")


def test_index_by_key_rejects_missing_key():
    with pytest.raises(RuntimeFailure, match="missing key field"):
        index_by_key([{"other": 1}], "id", "api")


def test_index_by_key_rejects_empty_key():
    with pytest.raises(RuntimeFailure, match="empty key field"):
        index_by_key([{"id": "  "}], "id", "api")


def test_index_by_key_rejects_non_object():
    with pytest.raises(RuntimeFailure, match="not an object"):
        index_by_key(["just a string"], "id", "api")


def test_compare_detects_all_discrepancy_types():
    local = {
        "a": {"id": "a", "amount": "10", "status": "ok"},
        "b": {"id": "b", "amount": "20", "status": "ok"},
        "c": {"id": "c", "amount": "30", "status": "ok"},  # local-only
    }
    remote = {
        "a": {"id": "a", "amount": "10", "status": "ok"},  # identical
        "b": {"id": "b", "amount": "99", "status": "ok"},  # amount mismatch
        "d": {"id": "d", "amount": "40", "status": "ok"},  # api-only
    }
    missing_in_remote, missing_in_local, mismatches = compare(local, remote, "id", None)
    assert missing_in_remote == ["c"]
    assert missing_in_local == ["d"]
    assert len(mismatches) == 1
    assert mismatches[0]["key"] == "b"
    assert mismatches[0]["field"] == "amount"
    assert mismatches[0]["local_value"] == "20"
    assert mismatches[0]["api_value"] == "99"


def test_compare_restricts_to_named_fields():
    local = {"a": {"id": "a", "amount": "10", "status": "x"}}
    remote = {"a": {"id": "a", "amount": "10", "status": "y"}}
    # Only compare amount -> the status difference is ignored.
    _, _, mismatches = compare(local, remote, "id", ["amount"])
    assert mismatches == []


def test_compare_cross_type_values_match_after_normalize():
    # MySQL might return int 10 while JSON has "10"; these should be equal.
    local = {"a": {"id": "a", "amount": 10}}
    remote = {"a": {"id": "a", "amount": "10"}}
    _, _, mismatches = compare(local, remote, "id", None)
    assert mismatches == []


def test_load_json_records_missing_file():
    with pytest.raises(RuntimeFailure, match="file not found"):
        load_json_records("/no/such/file.json", "api")


def test_load_json_records_malformed(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    with pytest.raises(RuntimeFailure, match="malformed JSON"):
        load_json_records(str(bad), "api")


def test_load_json_records_not_a_list(tmp_path):
    obj = tmp_path / "obj.json"
    obj.write_text(json.dumps({"a": 1}))
    with pytest.raises(RuntimeFailure, match="expected a JSON array"):
        load_json_records(str(obj), "api")


def test_write_csv_report_contents(tmp_path):
    out = tmp_path / "report.csv"
    write_csv_report(
        str(out),
        missing_in_remote=["c"],
        missing_in_local=["d"],
        mismatches=[{"key": "b", "field": "amount", "local_value": "20", "api_value": "99"}],
    )
    with open(out, newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["discrepancy_type", "key", "field", "local_value", "api_value"]
    assert ["missing_in_api", "c", "", "", ""] in rows
    assert ["missing_in_local", "d", "", "", ""] in rows
    assert ["field_mismatch", "b", "amount", "20", "99"] in rows
