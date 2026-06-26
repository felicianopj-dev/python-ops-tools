"""Unit tests for verify_backup helpers (no live database required)."""

import gzip

from verify_backup import (
    EXIT_CONFIG,
    is_gzip,
    main,
    make_temp_db_name,
    quote_ident,
)


def test_quote_ident_escapes_backticks():
    assert quote_ident("payments") == "`payments`"
    assert quote_ident("weird`name") == "`weird``name`"


def test_is_gzip_detects_by_magic_bytes(tmp_path):
    plain = tmp_path / "dump.sql"
    plain.write_text("CREATE TABLE t (id INT);")
    assert is_gzip(str(plain)) is False

    gzipped = tmp_path / "dump.sql.gz"
    with gzip.open(gzipped, "wb") as f:
        f.write(b"CREATE TABLE t (id INT);")
    assert is_gzip(str(gzipped)) is True


def test_is_gzip_renamed_plain_file_is_not_gzip(tmp_path):
    # A plain file with a .gz name must still be detected as not gzip (by content).
    fake = tmp_path / "notreally.gz"
    fake.write_text("plain text")
    assert is_gzip(str(fake)) is False


def test_make_temp_db_name_unique_and_prefixed():
    name = make_temp_db_name(None)
    assert name.startswith("verify_tmp_")
    assert make_temp_db_name("custom") == "custom"


def test_main_missing_file_returns_config_error():
    assert main(["/no/such/backup.sql"]) == EXIT_CONFIG


def test_main_empty_file_returns_config_error(tmp_path):
    empty = tmp_path / "empty.sql"
    empty.write_text("")
    assert main([str(empty)]) == EXIT_CONFIG


def test_main_missing_db_user_returns_config_error(tmp_path, monkeypatch):
    dump = tmp_path / "dump.sql"
    dump.write_text("CREATE TABLE t (id INT);")
    monkeypatch.delenv("DB_USER", raising=False)
    assert main([str(dump)]) == EXIT_CONFIG
