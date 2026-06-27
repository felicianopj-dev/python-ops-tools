"""
Integration tests that exercise the DB tools against a live MySQL.

These are gated and skip automatically unless a server is available. A MySQL is
sourced in one of two ways:

1. External server via env vars (used in CI's `services: mysql`):
   DB_HOST, DB_PORT, DB_USER, MYSQL_PWD must point at a reachable server whose
   user can CREATE/DROP databases.
2. Local convenience via `testcontainers` + Docker, when no env server is set.

They also require the `mysql`/`mysqldump` client binaries on PATH (the backup and
verify tools shell out to them).

Run just these with:  pytest -m integration
"""

import os
import shutil

import pytest

import backup_all_dbs
import backup_db
import reconciliation_checker
import verify_backup

pytestmark = pytest.mark.integration

_HAVE_CLIENTS = bool(shutil.which("mysql") and shutil.which("mysqldump"))
TEST_DB = "opstest"


def _seed_schema() -> None:
    """Create the test database and a small seeded table via PyMySQL."""
    import pymysql

    conn = pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ.get("MYSQL_PWD", ""),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
            cur.execute(f"CREATE DATABASE {TEST_DB}")
            cur.execute(f"USE {TEST_DB}")
            cur.execute("CREATE TABLE widgets (id INT PRIMARY KEY, name VARCHAR(50), qty INT)")
            cur.executemany(
                "INSERT INTO widgets (id, name, qty) VALUES (%s, %s, %s)",
                [(1, "alpha", 10), (2, "beta", 20), (3, "gamma", 30)],
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def mysql_env():
    """Ensure a live MySQL (env or testcontainers), seed it, or skip."""
    if not _HAVE_CLIENTS:
        pytest.skip("mysql/mysqldump client binaries not found on PATH")

    container = None
    if not (os.getenv("DB_HOST") and os.getenv("DB_USER")):
        # No external server: try a throwaway container.
        try:
            from testcontainers.mysql import MySqlContainer
        except Exception:
            pytest.skip("no DB_* env server and testcontainers is not installed")
        try:
            container = MySqlContainer("mysql:8", username="root", password="ops")
            container.start()
        except Exception as e:  # Docker missing / cannot pull image, etc.
            pytest.skip(f"could not start a MySQL testcontainer: {e}")
        host = container.get_container_host_ip()
        # The mysql/mysqldump CLIs treat "localhost" as a unix socket and ignore
        # -P/the TCP port; force a TCP host so the exposed container port is used.
        os.environ["DB_HOST"] = "127.0.0.1" if host in ("localhost", "") else host
        os.environ["DB_PORT"] = str(container.get_exposed_port(3306))
        os.environ["DB_USER"] = "root"
        os.environ["MYSQL_PWD"] = "ops"

    try:
        _seed_schema()
    except Exception as e:
        if container is not None:
            container.stop()
        pytest.skip(f"could not connect/seed MySQL: {e}")

    try:
        yield
    finally:
        if container is not None:
            container.stop()


def test_backup_db_creates_nonempty_dump(mysql_env, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_NAME", TEST_DB)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    assert backup_db.main([]) == backup_db.EXIT_OK
    dumps = list(tmp_path.glob(f"{TEST_DB}_*.sql"))
    assert len(dumps) == 1
    assert dumps[0].stat().st_size > 0


def test_backup_then_verify_roundtrip(mysql_env, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_NAME", TEST_DB)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    assert backup_db.main([]) == backup_db.EXIT_OK
    dump = next(tmp_path.glob(f"{TEST_DB}_*.sql"))

    # The restored dump must contain at least one table with rows.
    rc = verify_backup.main([str(dump), "--min-tables", "1", "--critical-tables", "widgets"])
    assert rc == verify_backup.EXIT_PASS


def test_backup_all_dbs_dumps_test_db(mysql_env, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    assert backup_all_dbs.main([]) == 0
    assert list(tmp_path.glob(f"{TEST_DB}_*.sql.gz"))


def test_reconciliation_matches_self(mysql_env, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_NAME", TEST_DB)
    # An API snapshot identical to the table -> zero discrepancies.
    api_file = tmp_path / "api.json"
    api_file.write_text(
        '[{"id": 1, "name": "alpha", "qty": 10},'
        ' {"id": 2, "name": "beta", "qty": 20},'
        ' {"id": 3, "name": "gamma", "qty": 30}]'
    )
    rc = reconciliation_checker.main(
        ["--table", "widgets", "--key", "id", "--api-file", str(api_file)]
    )
    assert rc == reconciliation_checker.EXIT_OK
