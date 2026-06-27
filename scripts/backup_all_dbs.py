#!/usr/bin/env python3
"""
backup_all_dbs.py

Back up all non-system MySQL databases on a server using `mysqldump`.

Designed for production/automation use: each database is streamed straight into
a gzip file (low memory), dumps are consistent (`--single-transaction`), and the
script emits single-line JSON logs suitable for log aggregation.

Configuration (environment variables):
  DB_HOST      Server host (default: localhost)
  DB_PORT      Server port (default: 3306)
  DB_USER      User name (required)
  DB_PASSWORD  Password (optional; passed to subprocesses via MYSQL_PWD)
  MYSQL_PWD    Password fallback (standard MySQL env var) if DB_PASSWORD is unset
  BACKUP_DIR   Output directory (default: ./backups)
  GZIP_LEVEL   gzip compression level 1..9 (default: 6)
"""

import gzip
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import oplog

# System databases that should never be backed up.
SYSTEM_DBS = {"information_schema", "mysql", "performance_schema", "sys"}


@dataclass
class ServerConfig:
    """Connection and output settings for a full-server backup run."""

    db_host: str
    db_port: str
    db_user: str
    db_password: str
    backup_dir: str
    gzip_level: int


# Output mode for log_json; defaults to JSON, switchable via LOG_JSON (set in main).
_JSON_MODE = True


def log_json(level: str, event: str, **fields: Any) -> None:
    """Emit a structured log record (JSON by default; human text when LOG_JSON=0)."""
    oplog.log(level, event, as_json=_JSON_MODE, **fields)


def read_config() -> ServerConfig:
    """
    Read server/backup settings from the environment.

    Raises ValueError when DB_USER is missing or GZIP_LEVEL is invalid.
    """
    db_user = os.getenv("DB_USER")
    if not db_user:
        raise ValueError("DB_USER environment variable is required.")

    raw_level = os.getenv("GZIP_LEVEL", "6")
    try:
        gzip_level = int(raw_level)
    except ValueError:
        raise ValueError(f"GZIP_LEVEL must be an integer 1..9, got: {raw_level!r}") from None
    if not 1 <= gzip_level <= 9:
        raise ValueError(f"GZIP_LEVEL must be between 1 and 9, got: {gzip_level}")

    return ServerConfig(
        db_host=os.getenv("DB_HOST", "localhost"),
        db_port=os.getenv("DB_PORT", "3306"),
        db_user=db_user,
        db_password=os.getenv("DB_PASSWORD", "") or os.getenv("MYSQL_PWD", ""),
        backup_dir=os.getenv("BACKUP_DIR", "./backups"),
        gzip_level=gzip_level,
    )


def build_env(config: ServerConfig) -> dict[str, str]:
    """
    Build the subprocess environment, passing the password via MYSQL_PWD.

    Keeping the password out of argv avoids leaking it through the process list.
    """
    env = os.environ.copy()
    if config.db_password:
        env["MYSQL_PWD"] = config.db_password
    return env


def mysql_base_cmd(config: ServerConfig) -> list[str]:
    """Base `mysql` command used to enumerate databases."""
    return ["mysql", "-h", config.db_host, "-P", config.db_port, "-u", config.db_user]


def mysqldump_base_cmd(config: ServerConfig) -> list[str]:
    """Base `mysqldump` command with safe defaults for InnoDB databases."""
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
    ]


def list_databases(config: ServerConfig) -> list[str]:
    """Return the list of non-system databases available on the server."""
    cmd = mysql_base_cmd(config) + ["-N", "-e", "SHOW DATABASES;"]
    result = subprocess.run(cmd, capture_output=True, text=True, env=build_env(config), check=True)
    databases = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [db for db in databases if db not in SYSTEM_DBS]


def dump_database_gzip(config: ServerConfig, db_name: str, run_ts: str) -> str:
    """
    Create a gzipped SQL backup file for a single database.

    Streams mysqldump's stdout directly into the gzip file to keep memory usage
    low. Raises subprocess.CalledProcessError (and removes the partial file) on
    a non-zero mysqldump exit.
    """
    filename = Path(config.backup_dir) / f"{db_name}_{run_ts}.sql.gz"
    cmd = mysqldump_base_cmd(config) + [db_name]

    start = datetime.now(timezone.utc)
    log_json("info", "backup_start", db=db_name, file=str(filename), host=config.db_host)

    with gzip.open(filename, "wb", compresslevel=config.gzip_level) as gz:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=build_env(config)
        )

        try:
            assert proc.stdout is not None
            stdout = proc.stdout
            for chunk in iter(lambda: stdout.read(1024 * 64), b""):
                gz.write(chunk)

            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            code = proc.wait()

            if code != 0:
                log_json("error", "backup_failed", db=db_name, code=code, stderr=stderr.strip())
                # Remove the partial file on failure.
                try:
                    filename.unlink(missing_ok=True)
                except OSError:
                    pass
                raise subprocess.CalledProcessError(code, cmd, output=None, stderr=stderr)
        finally:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    end = datetime.now(timezone.utc)
    duration_ms = int((end - start).total_seconds() * 1000)
    size_bytes = filename.stat().st_size if filename.exists() else 0

    log_json(
        "info",
        "backup_ok",
        db=db_name,
        file=str(filename),
        duration_ms=duration_ms,
        size_bytes=size_bytes,
        gzip_level=config.gzip_level,
    )
    return str(filename)


def main(argv: list[str] | None = None) -> int:
    global _JSON_MODE
    _JSON_MODE = oplog.want_json(default=True)
    try:
        config = read_config()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    Path(config.backup_dir).mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    log_json(
        "info",
        "run_start",
        host=config.db_host,
        backup_dir=str(Path(config.backup_dir).resolve()),
        run_ts=run_ts,
    )

    try:
        databases = list_databases(config)
    except subprocess.CalledProcessError as e:
        stderr = str(e.stderr) if getattr(e, "stderr", None) else ""
        log_json(
            "error",
            "list_databases_failed",
            host=config.db_host,
            code=e.returncode,
            stderr=stderr.strip(),
        )
        return 3

    log_json("info", "databases_found", count=len(databases), dbs=databases)

    if not databases:
        log_json("warn", "nothing_to_backup")
        return 0

    ok = 0
    failed = 0
    for db in databases:
        try:
            dump_database_gzip(config, db, run_ts)
            ok += 1
        except subprocess.CalledProcessError:
            failed += 1

    log_json("info", "run_done", ok=ok, failed=failed, total=len(databases), run_ts=run_ts)
    return 0 if failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
