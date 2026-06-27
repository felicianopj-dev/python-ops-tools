#!/usr/bin/env python3
"""
backup_db.py

Create a consistent backup of a single MySQL database using `mysqldump`
with `--single-transaction` (a non-locking, point-in-time dump for InnoDB).

All configuration is read from environment variables so the script is safe to
run from cron or inside containers without hard-coded credentials:

  DB_NAME      Database to back up (required)
  DB_USER      User name (required)
  DB_HOST      Server host (default: localhost)
  DB_PORT      Server port (default: 3306)
  BACKUP_DIR   Output directory (default: ./backups)
  DB_PASSWORD  Password (optional; passed to mysqldump via MYSQL_PWD)
  MYSQL_PWD    Password fallback (standard MySQL env var) if DB_PASSWORD is unset

Set LOG_JSON=1 to emit machine-readable JSON lines instead of human text.

Exit codes:
  0  backup created successfully
  2  configuration error (missing required environment variables)
  3  the mysqldump command failed
"""

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


def read_config() -> BackupConfig:
    """
    Read backup settings from the environment.

    Raises ValueError when a required variable is missing.
    """
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    if not db_name:
        raise ValueError("DB_NAME environment variable is required.")
    if not db_user:
        raise ValueError("DB_USER environment variable is required.")
    return BackupConfig(
        db_name=db_name,
        db_user=db_user,
        db_host=os.getenv("DB_HOST", "localhost"),
        db_port=os.getenv("DB_PORT", "3306"),
        backup_dir=os.getenv("BACKUP_DIR", "./backups"),
    )


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

    try:
        config = read_config()
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
