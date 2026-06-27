#!/usr/bin/env python3
"""
verify_backup.py

Validate that a MySQL backup is actually *restorable*, not merely present on
disk. An untested backup is not a backup: a dump can exist, be the right size,
and still fail to restore (truncated file, encoding issues, unsupported syntax,
missing routines). This script proves a dump can be restored by actually
restoring it into a throwaway database and running sanity checks.

What it does:
  1. Creates a temporary (throwaway) database on the target server.
  2. Restores the given dump (.sql or .sql.gz) into that database.
  3. Runs sanity checks:
       - no SQL errors during restore,
       - table count meets expectations,
       - row counts for a configurable list of "critical tables" are non-zero.
  4. Drops the temporary database afterwards — even on failure.
  5. Prints a clear PASS/FAIL report.

Connection configuration is reused from the existing backup scripts
(backup_db.py / backup_all_dbs.py) via environment variables:
  DB_HOST       Server host (default: localhost)
  DB_PORT       Server port (default: 3306)
  DB_USER       User name (required)
  DB_PASSWORD   Password (optional). MYSQL_PWD is also honored as a fallback.

The restore and all queries go through the `mysql` client, so the only external
requirement is a reachable MySQL server and the `mysql` binary on PATH — the same
tooling the backup scripts already depend on.

Note: this targets single-database dumps as produced by backup_db.py /
backup_all_dbs.py (no CREATE DATABASE / USE statements), which restore cleanly
into the chosen temporary database.

Exit codes:
  0  verification PASSED
  1  verification FAILED (restore error or a failed sanity check)
  2  configuration error (missing DB_USER, bad arguments, missing file)
  3  runtime error (could not connect / create / drop the temp database)
"""

from __future__ import annotations

import argparse
import gzip
import os
import subprocess
import sys
from datetime import datetime

import oplog

# Process exit codes (see module docstring).
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2
EXIT_RUNTIME = 3


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


class RuntimeFailure(Exception):
    """Raised when the temp database cannot be created/dropped or queried."""


class RestoreError(Exception):
    """Raised when restoring the dump into the temp database fails."""


# --------------------------------------------------------------------------- #
# Connection configuration (reused from the backup scripts)
# --------------------------------------------------------------------------- #
def get_connection_config() -> tuple[str, str, str]:
    """
    Read connection settings from the environment, mirroring the backup scripts.

    Returns (host, port, user). Raises ConfigError when DB_USER is missing.
    The password is supplied to subprocesses via build_env(), never on argv.
    """
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "3306")
    user = os.getenv("DB_USER")
    if not user:
        raise ConfigError("DB_USER environment variable is required.")
    return host, port, user


def build_env() -> dict:
    """
    Build the subprocess environment, passing the password via MYSQL_PWD.

    Accepts DB_PASSWORD (as in backup_all_dbs.py) or a pre-set MYSQL_PWD
    (as relied on by backup_db.py). Keeping the password out of argv avoids
    leaking it through the process list.
    """
    env = os.environ.copy()
    password = os.getenv("DB_PASSWORD") or os.getenv("MYSQL_PWD")
    if password:
        env["MYSQL_PWD"] = password
    return env


def mysql_base_cmd(database: str | None = None) -> list[str]:
    """Build the base `mysql` invocation, optionally selecting a database."""
    host, port, user = get_connection_config()
    cmd = ["mysql", "-h", host, "-P", port, "-u", user]
    if database is not None:
        cmd.append(database)
    return cmd


# --------------------------------------------------------------------------- #
# Low-level mysql helpers
# --------------------------------------------------------------------------- #
def run_sql(statement: str, database: str | None = None) -> str:
    """
    Execute a single SQL statement via the mysql client and return stdout.

    Uses -N (skip column names) so callers can parse scalar results directly.
    Raises RuntimeFailure on a non-zero exit.
    """
    cmd = mysql_base_cmd(database) + ["-N", "-e", statement]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=build_env())
    if proc.returncode != 0:
        raise RuntimeFailure(f"mysql command failed ({statement!r}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def scalar_int(statement: str, database: str | None = None) -> int:
    """Run a statement expected to return a single integer and parse it."""
    out = run_sql(statement, database)
    try:
        return int(out.splitlines()[0]) if out else 0
    except (ValueError, IndexError):
        raise RuntimeFailure(f"expected an integer from {statement!r}, got: {out!r}") from None


def quote_ident(name: str) -> str:
    """
    Safely quote a SQL identifier (database/table name) with backticks.

    Backticks inside the identifier are escaped by doubling, matching MySQL's
    quoting rules, so a crafted critical-table name cannot break out.
    """
    return "`" + name.replace("`", "``") + "`"


# --------------------------------------------------------------------------- #
# Temp database lifecycle + restore
# --------------------------------------------------------------------------- #
def make_temp_db_name(override: str | None) -> str:
    """Generate a unique, identifiable temp database name (or use override)."""
    if override:
        return override
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"verify_tmp_{stamp}_{os.getpid()}"


def create_temp_db(name: str) -> None:
    """Create the throwaway database. Raises RuntimeFailure on error."""
    run_sql(f"CREATE DATABASE {quote_ident(name)};")


def drop_temp_db(name: str) -> None:
    """Drop the throwaway database. Raises RuntimeFailure on error."""
    run_sql(f"DROP DATABASE IF EXISTS {quote_ident(name)};")


def restore_dump(path: str, database: str) -> None:
    """
    Restore a .sql or .sql.gz dump into `database` by streaming it into mysql.

    gzip.open transparently handles both plain and gzipped files in binary mode,
    so a single code path covers backup_db.py (.sql) and backup_all_dbs.py
    (.sql.gz) output. Raises RestoreError if mysql reports a non-zero exit.
    """
    opener = gzip.open if is_gzip(path) else open
    cmd = mysql_base_cmd(database)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=build_env(),
    )
    try:
        assert proc.stdin is not None
        with opener(path, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                proc.stdin.write(chunk)
        proc.stdin.close()
    except OSError as e:
        proc.kill()
        proc.wait()
        raise RestoreError(f"could not read dump file {path}: {e}") from e

    _, stderr = proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace").strip()
        raise RestoreError(f"restore failed: {msg or 'mysql exited non-zero'}")


def is_gzip(path: str) -> bool:
    """Return True if the file is gzip-compressed (by magic bytes, not name)."""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except OSError:
        # Fall back to the extension if the file cannot be sniffed.
        return path.endswith(".gz")


# --------------------------------------------------------------------------- #
# Sanity checks
# --------------------------------------------------------------------------- #
def count_tables(database: str) -> int:
    """Return the number of base tables in the restored database."""
    return scalar_int(
        "SELECT COUNT(*) FROM information_schema.tables "
        f"WHERE table_schema = '{database}' AND table_type = 'BASE TABLE';"
    )


def count_rows(database: str, table: str) -> int:
    """Return the row count of a single table in the restored database."""
    return scalar_int(f"SELECT COUNT(*) FROM {quote_ident(table)};", database)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify that a MySQL backup dump is actually restorable.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "backup_file",
        help="Path to the backup dump to verify (.sql or .sql.gz).",
    )
    parser.add_argument(
        "--critical-tables",
        default="",
        help="Comma-separated tables that must exist and have a non-zero row count.",
    )
    parser.add_argument(
        "--expected-tables",
        type=int,
        help="Exact number of tables the restored database must contain.",
    )
    parser.add_argument(
        "--min-tables",
        type=int,
        default=1,
        help="Minimum number of tables the restored database must contain.",
    )
    parser.add_argument(
        "--temp-db",
        help="Override the generated temporary database name.",
    )
    parser.add_argument(
        "--keep-temp-db",
        action="store_true",
        help="Do not drop the temp database afterwards (for debugging).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also emit a machine-readable JSON summary line (or set LOG_JSON=1).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    as_json = oplog.want_json(args.json)

    # Validate inputs early.
    if not os.path.isfile(args.backup_file):
        print(f"Error: backup file not found: {args.backup_file}", file=sys.stderr)
        return EXIT_CONFIG
    if os.path.getsize(args.backup_file) == 0:
        print(f"Error: backup file is empty: {args.backup_file}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        get_connection_config()  # validates DB_USER, surfaces config errors early
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return EXIT_CONFIG

    critical_tables = [t.strip() for t in args.critical_tables.split(",") if t.strip()]
    temp_db = make_temp_db_name(args.temp_db)

    # Each entry is (check_name, passed, detail).
    results: list[tuple[str, bool, str]] = []
    created = False

    print(f"Verifying backup: {args.backup_file}")
    print(f"Temporary database: {temp_db}")
    print("-" * 60)

    try:
        # Create the throwaway database.
        try:
            create_temp_db(temp_db)
            created = True
        except RuntimeFailure as e:
            print(f"Error: could not create temp database: {e}", file=sys.stderr)
            return EXIT_RUNTIME

        # Restore — this is itself a check (no SQL errors during restore).
        try:
            restore_dump(args.backup_file, temp_db)
            results.append(("restore", True, "dump restored without SQL errors"))
        except RestoreError as e:
            results.append(("restore", False, str(e)))
            # Without a successful restore the remaining checks are meaningless.
            return _finish(results, temp_db, created, args.keep_temp_db, as_json=as_json)

        # Table count checks.
        try:
            n_tables = count_tables(temp_db)
        except RuntimeFailure as e:
            print(f"Error: could not query restored database: {e}", file=sys.stderr)
            return _finish(
                results, temp_db, created, args.keep_temp_db, runtime=True, as_json=as_json
            )

        if args.expected_tables is not None:
            ok = n_tables == args.expected_tables
            results.append(
                (
                    "table_count",
                    ok,
                    f"found {n_tables}, expected exactly {args.expected_tables}",
                )
            )
        else:
            ok = n_tables >= args.min_tables
            results.append(
                (
                    "table_count",
                    ok,
                    f"found {n_tables}, require at least {args.min_tables}",
                )
            )

        # Critical-table row-count checks.
        for table in critical_tables:
            try:
                rows = count_rows(temp_db, table)
                results.append(
                    (
                        f"rows:{table}",
                        rows > 0,
                        f"{rows} row(s)" if rows > 0 else "table is empty",
                    )
                )
            except RuntimeFailure as e:
                # A missing/unqueryable critical table is a failed check.
                results.append((f"rows:{table}", False, f"query error: {e}"))

        return _finish(results, temp_db, created, args.keep_temp_db, as_json=as_json)

    finally:
        # Cleanup always runs, even on unexpected errors above.
        if created and not args.keep_temp_db:
            try:
                drop_temp_db(temp_db)
            except RuntimeFailure as e:
                print(
                    f"[warn] could not drop temp database {temp_db}: {e}",
                    file=sys.stderr,
                )


def _finish(
    results: list[tuple[str, bool, str]],
    temp_db: str,
    created: bool,
    keep_temp_db: bool,
    runtime: bool = False,
    as_json: bool = False,
) -> int:
    """Print the PASS/FAIL report and return the appropriate exit code."""
    print_report(results, temp_db, keep_temp_db)
    all_passed = bool(results) and all(passed for _, passed, _ in results)
    if as_json:
        failed = [name for name, passed, _ in results if not passed]
        oplog.log(
            "info" if all_passed and not runtime else "error",
            "verify_result",
            as_json=True,
            overall="PASS" if all_passed and not runtime else "FAIL",
            checks=len(results),
            failed=failed,
            temp_db=temp_db,
            runtime_error=runtime,
        )
    if runtime:
        return EXIT_RUNTIME
    return EXIT_PASS if all_passed else EXIT_FAIL


def print_report(results: list[tuple[str, bool, str]], temp_db: str, keep_temp_db: bool) -> None:
    """Print a clear, human-readable PASS/FAIL report."""
    print("-" * 60)
    print("VERIFICATION REPORT")
    print("-" * 60)
    for name, passed, detail in results:
        marker = "PASS" if passed else "FAIL"
        print(f"  [{marker}] {name}: {detail}")
    print("-" * 60)
    all_passed = bool(results) and all(passed for _, passed, _ in results)
    overall = "PASS" if all_passed else "FAIL"
    print(f"OVERALL: {overall}")
    if keep_temp_db:
        print(f"(temp database '{temp_db}' was kept for inspection)")
    print("-" * 60)


if __name__ == "__main__":
    sys.exit(main())
