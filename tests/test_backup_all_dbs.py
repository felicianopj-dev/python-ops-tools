"""Unit tests for backup_all_dbs helpers (no live database required)."""

import types

import pytest

import backup_all_dbs
from backup_all_dbs import (
    ServerConfig,
    build_env,
    list_databases,
    main,
    mysql_base_cmd,
    mysqldump_base_cmd,
    parse_args,
    read_config,
)


def _clear_db_env(monkeypatch):
    for var in ("DB_USER", "DB_HOST", "DB_PORT", "DB_PASSWORD", "BACKUP_DIR", "GZIP_LEVEL"):
        monkeypatch.delenv(var, raising=False)


def _config(**overrides) -> ServerConfig:
    base = dict(
        db_host="localhost",
        db_port="3306",
        db_user="ops",
        db_password="",
        backup_dir="./backups",
        gzip_level=6,
    )
    base.update(overrides)
    return ServerConfig(**base)


def test_read_config_requires_db_user(monkeypatch):
    _clear_db_env(monkeypatch)
    with pytest.raises(ValueError, match="DB_USER"):
        read_config()


def test_read_config_defaults(monkeypatch):
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("DB_USER", "ops")
    config = read_config()
    assert config.db_host == "localhost"
    assert config.db_port == "3306"
    assert config.gzip_level == 6


def test_read_config_rejects_invalid_gzip_level(monkeypatch):
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("DB_USER", "ops")
    monkeypatch.setenv("GZIP_LEVEL", "abc")
    with pytest.raises(ValueError, match="GZIP_LEVEL"):
        read_config()


def test_read_config_rejects_out_of_range_gzip_level(monkeypatch):
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("DB_USER", "ops")
    monkeypatch.setenv("GZIP_LEVEL", "12")
    with pytest.raises(ValueError, match="between 1 and 9"):
        read_config()


def test_base_commands_include_port():
    config = _config(db_port="3307")
    assert "-P" in mysql_base_cmd(config)
    assert mysql_base_cmd(config)[mysql_base_cmd(config).index("-P") + 1] == "3307"
    dump = mysqldump_base_cmd(config)
    assert "-P" in dump and dump[dump.index("-P") + 1] == "3307"
    assert "--single-transaction" in dump


def test_build_env_sets_password_only_when_present():
    assert "MYSQL_PWD" not in build_env(_config(db_password=""))
    assert build_env(_config(db_password="secret"))["MYSQL_PWD"] == "secret"


def test_list_databases_filters_system_dbs(monkeypatch):
    fake_stdout = "information_schema\nmysql\npayments\nanalytics\nsys\n"
    monkeypatch.setattr(
        backup_all_dbs.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(stdout=fake_stdout),
    )
    assert list_databases(_config()) == ["payments", "analytics"]


def test_main_missing_db_user_returns_config_error(monkeypatch):
    _clear_db_env(monkeypatch)
    assert main([]) == 2


def test_parse_args_defaults_are_none():
    args = parse_args([])
    assert args.db_user is None
    assert args.db_host is None
    assert args.db_port is None
    assert args.backup_dir is None
    assert args.gzip_level is None


def test_flags_take_precedence_over_env(monkeypatch):
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("DB_USER", "from_env")
    monkeypatch.setenv("DB_PORT", "3306")
    monkeypatch.setenv("GZIP_LEVEL", "6")
    args = parse_args(["--db-user", "from_flag", "--db-port", "3307", "--gzip-level", "9"])
    config = read_config(args)
    assert config.db_user == "from_flag"  # flag beats env
    assert config.db_port == "3307"  # flag beats env
    assert config.gzip_level == 9  # flag beats env
    assert config.db_host == "localhost"  # falls through to default


def test_invalid_gzip_level_flag_is_rejected(monkeypatch):
    _clear_db_env(monkeypatch)
    args = parse_args(["--db-user", "ops", "--gzip-level", "12"])
    with pytest.raises(ValueError, match="between 1 and 9"):
        read_config(args)
