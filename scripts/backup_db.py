#!/usr/bin/env python3
"""
backup_db.py

Create a consistent backup of a single MySQL database using `mysqldump`
with `--single-transaction` (a non-locking, point-in-time dump for InnoDB).

Configuration comes from environment variables or matching CLI flags, so the
script is safe to run from cron or inside containers without hard-coded
credentials. Flags take precedence over environment variables, which take
precedence over the defaults. Secrets are env-only (never passed via argv, to
avoid leaking through the process list):

  DB_NAME / --db-name        Database to back up (required)
  DB_USER / --db-user        User name (required)
  DB_HOST / --db-host        Server host (default: localhost)
  DB_PORT / --db-port        Server port (default: 3306)
  BACKUP_DIR / --backup-dir  Output directory (default: ./backups)
  DB_PASSWORD                 Password (optional; passed to mysqldump via MYSQL_PWD)
  MYSQL_PWD                   Password fallback (standard MySQL env var) if DB_PASSWORD is unset

Set LOG_JSON=1 to emit machine-readable JSON lines instead of human text.

Exit codes:
  0  backup created successfully
  2  configuration error (missing required environment variables)
  3  the mysqldump command failed
"""

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime

import oplog

# Process exit codes (see module docstring).
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_DUMP = 3


@dataclass
class BackupConfig:
    """Connection and output settings for a single-database backup."""

    db_name: str
    db_user: str
    db_host: str
    db_port: str
    backup_dir: str


def _resolve(flag: str | None, env: str, default: str | None = None) -> str | None:
    """Resolve a setting with flag > env var > default precedence."""
    if flag is not None:
        return flag
    value = os.getenv(env)
    if value is not None and value != "":
        return value
    return default


def read_config(args: argparse.Namespace | None = None) -> BackupConfig:
    """
    Resolve backup settings from CLI flags (if given), then the environment,
    then defaults.

    Raises ValueError when a required setting (db name/user) is missing.
    """
    db_name = _resolve(getattr(args, "db_name", None), "DB_NAME")
    db_user = _resolve(getattr(args, "db_user", None), "DB_USER")
    if not db_name:
        raise ValueError("DB_NAME is required (set --db-name or the DB_NAME env var).")
    if not db_user:
        raise ValueError("DB_USER is required (set --db-user or the DB_USER env var).")
    return BackupConfig(
        db_name=db_name,
        db_user=db_user,
        db_host=_resolve(getattr(args, "db_host", None), "DB_HOST", "localhost") or "localhost",
        db_port=_resolve(getattr(args, "db_port", None), "DB_PORT", "3306") or "3306",
        backup_dir=_resolve(getattr(args, "backup_dir", None), "BACKUP_DIR", "./backups")
        or "./backups",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Back up a single MySQL database with mysqldump (--single-transaction).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Flags override the matching env vars. The password is env-only "
        "(DB_PASSWORD or MYSQL_PWD).",
    )
    # Defaults are None so an omitted flag falls back to the env var, then the
    # hardcoded default in read_config (flag > env > default).
    parser.add_argument("--db-name", help="Database to back up (env: DB_NAME).")
    parser.add_argument("--db-user", help="User name (env: DB_USER).")
    parser.add_argument("--db-host", help="Server host (env: DB_HOST, default: localhost).")
    parser.add_argument("--db-port", help="Server port (env: DB_PORT, default: 3306).")
    parser.add_argument(
        "--backup-dir", help="Output directory (env: BACKUP_DIR, default: ./backups)."
    )
    return parser.parse_args(argv)


def build_env(config: BackupConfig) -> dict[str, str]:
    """
    Build the subprocess environment, passing the password via MYSQL_PWD.

    Accepts DB_PASSWORD or a pre-set MYSQL_PWD. Keeping the password out of argv
    avoids leaking it through the process list.
    """
    env = os.environ.copy()
    password = os.getenv("DB_PASSWORD") or os.getenv("MYSQL_PWD")
    if password:
        env["MYSQL_PWD"] = password
    return env


def build_command(config: BackupConfig) -> list[str]:
    """Build the mysqldump argument list for the given configuration."""
    return [
        "mysqldump",
        "-h",
        config.db_host,
        "-P",
        config.db_port,
        "-u",
        config.db_user,
        "--single-transaction",
        "--routines",
        "--triggers",
        "--events",
        config.db_name,
    ]


def run_backup(config: BackupConfig) -> str:
    """
    Run mysqldump and write the dump to a timestamped .sql file.

    Returns the path of the created file. Raises subprocess.CalledProcessError
    if mysqldump exits non-zero (the partial file is removed in that case);
    the captured stderr is attached so callers can report the reason.
    """
    os.makedirs(config.backup_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(config.backup_dir, f"{config.db_name}_{timestamp}.sql")

    command = build_command(config)
    try:
        with open(filename, "w") as f:
            subprocess.run(
                command,
                stdout=f,
                stderr=subprocess.PIPE,
                env=build_env(config),
                check=True,
            )
    except subprocess.CalledProcessError:
        # Do not leave a partial/corrupt dump behind on failure.
        if os.path.exists(filename):
            os.remove(filename)
        raise
    return filename


def main(argv: list[str] | None = None) -> int:
    as_json = oplog.want_json()
    args = parse_args(argv)

    try:
        config = read_config(args)
    except ValueError as e:
        if as_json:
            oplog.log("error", "config_error", as_json=True, error=str(e), stream=sys.stderr)
        else:
            print(f"Error: {e}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        filename = run_backup(config)
    except subprocess.CalledProcessError as e:
        reason = ""
        if e.stderr:
            stderr = (
                e.stderr.decode("utf-8", errors="replace")
                if isinstance(e.stderr, bytes)
                else e.stderr
            )
            reason = stderr.strip()
        if as_json:
            oplog.log(
                "error",
                "backup_failed",
                as_json=True,
                db=config.db_name,
                returncode=e.returncode,
                error=reason or None,
                stream=sys.stderr,
            )
        else:
            detail = f": {reason}" if reason else ""
            print(f"Error: mysqldump failed (exit {e.returncode}){detail}.", file=sys.stderr)
        return EXIT_DUMP

    if as_json:
        oplog.log("info", "backup_ok", as_json=True, db=config.db_name, file=filename)
    else:
        print(f"Backup created: {filename}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
