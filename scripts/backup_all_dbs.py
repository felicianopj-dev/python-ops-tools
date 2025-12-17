import os
import json
import gzip
import subprocess
from datetime import datetime
from pathlib import Path

# Database connection settings (read from environment variables)
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")  # optional

# Backup settings
BACKUP_DIR = os.getenv("BACKUP_DIR", "./backups")

# gzip compression level: 1 (fast) .. 9 (small)
GZIP_LEVEL = int(os.getenv("GZIP_LEVEL", "6"))

# Basic validation
if not DB_USER:
    raise SystemExit("Error: DB_USER environment variable is required.")

# Ensure backup directory exists
Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)

# Timestamp used for all backups in this execution
run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

# System databases that should not be backed up
SYSTEM_DBS = {"information_schema", "mysql", "performance_schema", "sys"}

def log_json(level: str, event: str, **fields):
    """
    Emits a single-line JSON log to stdout.
    """
    payload = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "level": level,
        "event": event,
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False))


def mysql_base_cmd():
    """
    Base mysql command used to list databases.
    Password is not passed as an argument to avoid leaking it via process list.
    """
    return ["mysql", "-h", DB_HOST, "-u", DB_USER]

def mysqldump_base_cmd():
    """
    Base mysqldump command with safe defaults for InnoDB databases.
    """
    return [
        "mysqldump",
        "-h", DB_HOST,
        "-u", DB_USER,
        "--single-transaction",
        "--routines",
        "--triggers",
        "--events",
    ]


def build_env():
    """
    Builds an environment dict for subprocesses, using MYSQL_PWD when provided.
    """
    env = os.environ.copy()
    if DB_PASSWORD:
        env["MYSQL_PWD"] = DB_PASSWORD
    return env


def list_databases():
    """
    Returns a list of non-system databases available on the server.
    """
    cmd = mysql_base_cmd() + ["-N", "-e", "SHOW DATABASES;"]
    result = subprocess.run(cmd, capture_output=True, text=True, env=build_env(), check=True)
    databases = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [db for db in databases if db not in SYSTEM_DBS]


def dump_database_gzip(db_name: str):
    """
    Creates a gzipped SQL backup file for a single database.
    """
    filename = Path(BACKUP_DIR) / f"{db_name}_{run_ts}.sql.gz"
    cmd = mysqldump_base_cmd() + [db_name]

    start = datetime.utcnow()
    log_json("info", "backup_start", db=db_name, file=str(filename), host=DB_HOST)

    # Stream mysqldump stdout directly into a gzip file (no huge memory usage)
    with gzip.open(filename, "wb", compresslevel=GZIP_LEVEL) as gz:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=build_env())

        try:
            assert proc.stdout is not None
            for chunk in iter(lambda: proc.stdout.read(1024 * 64), b""):
                gz.write(chunk)

            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            code = proc.wait()

            if code != 0:
                log_json("error", "backup_failed", db=db_name, code=code, stderr=stderr.strip())
                # Remove partial file on failure
                try:
                    filename.unlink(missing_ok=True)
                except Exception:
                    pass
                raise subprocess.CalledProcessError(code, cmd, output=None, stderr=stderr)

        finally:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    end = datetime.utcnow()
    duration_ms = int((end - start).total_seconds() * 1000)
    size_bytes = filename.stat().st_size if filename.exists() else 0

    log_json(
        "info",
        "backup_ok",
        db=db_name,
        file=str(filename),
        duration_ms=duration_ms,
        size_bytes=size_bytes,
        gzip_level=GZIP_LEVEL,
    )

    return str(filename)


def main():
    log_json("info", "run_start", host=DB_HOST, backup_dir=str(Path(BACKUP_DIR).resolve()), run_ts=run_ts)

    try:
        databases = list_databases()
    except subprocess.CalledProcessError as e:
        stderr = ""
        if getattr(e, "stderr", None):
            stderr = str(e.stderr)
        log_json("error", "list_databases_failed", host=DB_HOST, code=e.returncode, stderr=stderr.strip())
        raise

    log_json("info", "databases_found", count=len(databases), dbs=databases)

    if not databases:
        log_json("warn", "nothing_to_backup")
        return

    ok = 0
    failed = 0

    for db in databases:
        try:
            dump_database_gzip(db)
            ok += 1
        except subprocess.CalledProcessError:
            failed += 1

    log_json("info", "run_done", ok=ok, failed=failed, total=len(databases), run_ts=run_ts)

if __name__ == "__main__":
    main()

