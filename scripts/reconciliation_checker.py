#!/usr/bin/env python3
"""
reconciliation_checker.py

Reconcile records between two data sources to surface discrepancies.

This simulates a common fintech ops task: confirming that a local database
table (e.g. payment transactions) matches an external system's view of the
same records (e.g. a payment processor's API). It reports rows that exist on
only one side and rows that exist on both sides but disagree on one or more
fields (e.g. amount or status).

Data sources:
  - Local:  a MySQL table. Connection settings are read from environment
            variables, reusing the same pattern as backup_db.py
            (DB_NAME, DB_USER, DB_HOST, plus MYSQL_PWD for the password).
  - Remote: a JSON file representing "API records" — a list of flat objects.
            A sample_api_data.json with ~20 fake records ships in the data/
            folder for demos and testing.

Records are matched on a key field (default: transaction_id). Comparison of
the remaining fields is done on their string representation so that values
coming from MySQL (Decimal, int, datetime) and from JSON (str, number) can be
compared consistently.

CLI examples:
  # Compare MySQL table `transactions` against the bundled sample API file
  DB_NAME=payments DB_USER=ops MYSQL_PWD='secret' \
    python3 reconciliation_checker.py \
      --table transactions \
      --api-file data/sample_api_data.json

  # Custom matching key, restrict compared fields, and write a CSV report
  DB_NAME=payments DB_USER=ops MYSQL_PWD='secret' \
    python3 reconciliation_checker.py \
      --table transactions \
      --key transaction_id \
      --fields amount,status \
      --csv /tmp/reconciliation_report.csv

  # Demo without a database: use a JSON file as the local source too
  python3 reconciliation_checker.py \
      --local-file data/sample_api_data.json \
      --api-file data/sample_api_data.json

Exit codes:
  0  reconciliation ran and both sides matched perfectly
  1  reconciliation ran and discrepancies were found
  2  configuration error (missing env vars / bad arguments)
  3  runtime error (DB connection failure, malformed input, etc.)
"""

import argparse
import csv
import json
import os
import sys
from typing import Any

# Process exit codes (see module docstring).
EXIT_OK = 0
EXIT_DISCREPANCIES = 1
EXIT_CONFIG = 2
EXIT_RUNTIME = 3


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


class RuntimeFailure(Exception):
    """Raised for recoverable runtime problems (DB, file, parsing)."""


def normalize(value: Any) -> str:
    """
    Normalize a single field value to a string for cross-source comparison.

    MySQL returns Decimal/int/datetime while JSON returns str/number, so a
    naive equality check would report false mismatches. Stringifying and
    trimming whitespace gives a stable, source-agnostic comparison without
    guessing types.
    """
    if value is None:
        return ""
    return str(value).strip()


def index_by_key(
    records: list[dict[str, Any]], key: str, source_name: str
) -> dict[str, dict[str, Any]]:
    """
    Build a {key_value: record} index, validating that every record carries a
    non-empty, unique matching key.
    """
    indexed: dict[str, dict[str, Any]] = {}
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            raise RuntimeFailure(f"{source_name}: record #{i} is not an object: {record!r}")
        if key not in record:
            raise RuntimeFailure(
                f"{source_name}: record #{i} is missing key field '{key}': {record!r}"
            )
        key_value = normalize(record[key])
        if key_value == "":
            raise RuntimeFailure(f"{source_name}: record #{i} has an empty key field '{key}'")
        if key_value in indexed:
            raise RuntimeFailure(f"{source_name}: duplicate key '{key_value}' for field '{key}'")
        indexed[key_value] = record
    return indexed


def load_json_records(path: str, source_name: str) -> list[dict[str, Any]]:
    """Load and validate a JSON file containing a list of flat records."""
    if not os.path.isfile(path):
        raise RuntimeFailure(f"{source_name}: file not found: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeFailure(f"{source_name}: malformed JSON in {path}: {e}") from e
    except OSError as e:
        raise RuntimeFailure(f"{source_name}: cannot read {path}: {e}") from e

    if not isinstance(data, list):
        raise RuntimeFailure(
            f"{source_name}: expected a JSON array of records, got {type(data).__name__}"
        )
    return data


def load_mysql_records(table: str) -> list[dict[str, Any]]:
    """
    Load all rows from a MySQL table as a list of dicts.

    Connection settings follow the backup_db.py convention:
      DB_NAME   (required)  database name
      DB_USER   (required)  user name
      DB_HOST   (optional)  host, defaults to localhost
      MYSQL_PWD (optional)  password (standard MySQL env var)
      DB_PORT   (optional)  port, defaults to 3306
    """
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_host = os.getenv("DB_HOST", "localhost")
    db_password = os.getenv("MYSQL_PWD", "")
    try:
        db_port = int(os.getenv("DB_PORT", "3306"))
    except ValueError:
        raise ConfigError("DB_PORT must be an integer.") from None

    if not db_name:
        raise ConfigError("DB_NAME environment variable is required.")
    if not db_user:
        raise ConfigError("DB_USER environment variable is required.")

    try:
        import pymysql
        import pymysql.cursors
    except ImportError:
        raise RuntimeFailure(
            "pymysql is required to read from MySQL. "
            "Install it with: pip install -r requirements.txt"
        ) from None

    # Validate the table identifier ourselves: table names cannot be passed as
    # bound parameters, so we only allow a conservative identifier charset to
    # avoid SQL injection via --table.
    if not all(c.isalnum() or c in ("_", "$") for c in table) or table == "":
        raise ConfigError(f"Invalid table name: {table!r}")

    try:
        connection = pymysql.connect(
            host=db_host,
            port=db_port,
            user=db_user,
            password=db_password,
            database=db_name,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.MySQLError as e:
        raise RuntimeFailure(f"MySQL connection failed: {e}") from e

    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT * FROM `{table}`")
            rows = cursor.fetchall()
    except pymysql.MySQLError as e:
        raise RuntimeFailure(f"MySQL query failed on table '{table}': {e}") from e
    finally:
        connection.close()

    return list(rows)


def compare(
    local: dict[str, dict[str, Any]],
    remote: dict[str, dict[str, Any]],
    key: str,
    fields: list[str] | None,
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """
    Compare two keyed record sets.

    Returns a tuple of:
      - missing_in_remote: keys present locally but absent in the API records
      - missing_in_local:  keys present in the API records but absent locally
      - mismatches:        per-field differences for keys present on both sides

    When `fields` is None the comparison covers the union of all non-key fields
    seen across both matching records.
    """
    local_keys = set(local)
    remote_keys = set(remote)

    missing_in_remote = sorted(local_keys - remote_keys)
    missing_in_local = sorted(remote_keys - local_keys)

    mismatches: list[dict[str, Any]] = []
    for k in sorted(local_keys & remote_keys):
        l_rec = local[k]
        r_rec = remote[k]

        if fields is not None:
            compare_fields = fields
        else:
            compare_fields = sorted((set(l_rec) | set(r_rec)) - {key})

        for field in compare_fields:
            l_val = normalize(l_rec.get(field))
            r_val = normalize(r_rec.get(field))
            if l_val != r_val:
                mismatches.append(
                    {
                        "key": k,
                        "field": field,
                        "local_value": l_val,
                        "api_value": r_val,
                    }
                )

    return missing_in_remote, missing_in_local, mismatches


def print_report(
    key: str,
    local_count: int,
    remote_count: int,
    missing_in_remote: list[str],
    missing_in_local: list[str],
    mismatches: list[dict[str, Any]],
) -> None:
    """Print a clean, human-readable reconciliation report to stdout."""
    line = "=" * 60
    print(line)
    print("RECONCILIATION REPORT")
    print(line)
    print(f"Matching key      : {key}")
    print(f"Local records     : {local_count}")
    print(f"API records       : {remote_count}")
    print()

    print(f"[1] Missing in API (present locally only): {len(missing_in_remote)}")
    for k in missing_in_remote:
        print(f"    - {k}")
    print()

    print(f"[2] Missing locally (present in API only): {len(missing_in_local)}")
    for k in missing_in_local:
        print(f"    - {k}")
    print()

    print(f"[3] Field mismatches: {len(mismatches)}")
    for m in mismatches:
        print(f"    - {m['key']} | {m['field']}: local={m['local_value']!r} api={m['api_value']!r}")
    print()

    total_issues = len(missing_in_remote) + len(missing_in_local) + len(mismatches)
    print(line)
    print("SUMMARY")
    print(line)
    print(f"Missing in API    : {len(missing_in_remote)}")
    print(f"Missing locally   : {len(missing_in_local)}")
    print(f"Field mismatches  : {len(mismatches)}")
    print(f"Total issues      : {total_issues}")
    print(f"Status            : {'OK' if total_issues == 0 else 'DISCREPANCIES FOUND'}")
    print(line)


def write_csv_report(
    path: str,
    missing_in_remote: list[str],
    missing_in_local: list[str],
    mismatches: list[dict[str, Any]],
) -> None:
    """
    Write all discrepancies to a single CSV file.

    Each row is tagged with a `discrepancy_type` so the three categories can
    coexist in one flat file that is easy to filter in a spreadsheet.
    """
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["discrepancy_type", "key", "field", "local_value", "api_value"])
            for k in missing_in_remote:
                writer.writerow(["missing_in_api", k, "", "", ""])
            for k in missing_in_local:
                writer.writerow(["missing_in_local", k, "", "", ""])
            for m in mismatches:
                writer.writerow(
                    [
                        "field_mismatch",
                        m["key"],
                        m["field"],
                        m["local_value"],
                        m["api_value"],
                    ]
                )
    except OSError as e:
        raise RuntimeFailure(f"Cannot write CSV report to {path}: {e}") from e


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcile a local MySQL table against API records (JSON).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--table",
        help="Local MySQL table to read (uses DB_* env vars for connection).",
    )
    parser.add_argument(
        "--local-file",
        help="Use a JSON file as the local source instead of MySQL (for demos).",
    )
    parser.add_argument(
        "--api-file",
        required=True,
        help="Path to the JSON file with API records.",
    )
    parser.add_argument(
        "--key",
        default="transaction_id",
        help="Field used to match records across both sources.",
    )
    parser.add_argument(
        "--fields",
        help="Comma-separated fields to compare. Defaults to all non-key fields.",
    )
    parser.add_argument(
        "--csv",
        dest="csv_path",
        help="Optional path to also write a CSV discrepancy report.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.table and not args.local_file:
        print(
            "Error: provide a local source via --table (MySQL) or --local-file (JSON).",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    if args.table and args.local_file:
        print(
            "Error: --table and --local-file are mutually exclusive.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    fields = None
    if args.fields:
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]
        if not fields:
            print("Error: --fields was empty after parsing.", file=sys.stderr)
            return EXIT_CONFIG

    try:
        # Load the local source.
        if args.local_file:
            local_records = load_json_records(args.local_file, "local")
        else:
            local_records = load_mysql_records(args.table)

        # Load the API source.
        api_records = load_json_records(args.api_file, "api")

        # Index both sides by the matching key.
        local_index = index_by_key(local_records, args.key, "local")
        api_index = index_by_key(api_records, args.key, "api")
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except RuntimeFailure as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_RUNTIME

    missing_in_remote, missing_in_local, mismatches = compare(
        local_index, api_index, args.key, fields
    )

    print_report(
        key=args.key,
        local_count=len(local_index),
        remote_count=len(api_index),
        missing_in_remote=missing_in_remote,
        missing_in_local=missing_in_local,
        mismatches=mismatches,
    )

    if args.csv_path:
        try:
            write_csv_report(args.csv_path, missing_in_remote, missing_in_local, mismatches)
        except RuntimeFailure as e:
            print(f"Error: {e}", file=sys.stderr)
            return EXIT_RUNTIME
        print(f"\nCSV report written to: {args.csv_path}")

    total_issues = len(missing_in_remote) + len(missing_in_local) + len(mismatches)
    return EXIT_OK if total_issues == 0 else EXIT_DISCREPANCIES


if __name__ == "__main__":
    sys.exit(main())
