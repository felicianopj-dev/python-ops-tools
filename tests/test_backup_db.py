"""Unit tests for backup_db helpers (no live database required)."""

import pytest

from backup_db import (
    EXIT_CONFIG,
    BackupConfig,
    build_command,
    build_env,
    main,
    parse_args,
    read_config,
)


def _clear_db_env(monkeypatch):
    for var in ("DB_NAME", "DB_USER", "DB_HOST", "DB_PORT", "BACKUP_DIR"):
        monkeypatch.delenv(var, raising=False)


def test_read_config_requires_db_name(monkeypatch):
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("DB_USER", "ops")
    with pytest.raises(ValueError, match="DB_NAME"):
        read_config()


def test_read_config_requires_db_user(monkeypatch):
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("DB_NAME", "payments")
    with pytest.raises(ValueError, match="DB_USER"):
        read_config()


def test_read_config_defaults(monkeypatch):
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("DB_NAME", "payments")
    monkeypatch.setenv("DB_USER", "ops")
    config = read_config()
    assert config.db_host == "localhost"
    assert config.db_port == "3306"
    assert config.backup_dir == "./backups"


def test_build_command_includes_port_and_flags():
    config = BackupConfig(
        db_name="payments",
        db_user="ops",
        db_host="db.internal",
        db_port="3307",
        backup_dir="./backups",
    )
    cmd = build_command(config)
    assert cmd[0] == "mysqldump"
    assert "-P" in cmd and cmd[cmd.index("-P") + 1] == "3307"
    assert "--single-transaction" in cmd
    assert {"--routines", "--triggers", "--events"} <= set(cmd)
    assert cmd[-1] == "payments"


def test_build_env_uses_db_password(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.delenv("MYSQL_PWD", raising=False)
    config = BackupConfig("d", "u", "h", "3306", "./b")
    assert build_env(config)["MYSQL_PWD"] == "secret"


def test_build_env_falls_back_to_mysql_pwd(monkeypatch):
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    monkeypatch.setenv("MYSQL_PWD", "fallback")
    config = BackupConfig("d", "u", "h", "3306", "./b")
    assert build_env(config)["MYSQL_PWD"] == "fallback"


def test_main_missing_config_returns_config_error(monkeypatch):
    _clear_db_env(monkeypatch)
    assert main([]) == EXIT_CONFIG


def test_parse_args_defaults_are_none():
    # Every flag defaults to None so an omitted flag falls back to env, then default.
    args = parse_args([])
    assert args.db_name is None
    assert args.db_user is None
    assert args.db_host is None
    assert args.db_port is None
    assert args.backup_dir is None


def test_flags_take_precedence_over_env(monkeypatch):
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("DB_NAME", "from_env")
    monkeypatch.setenv("DB_PORT", "3306")
    args = parse_args(["--db-name", "from_flag", "--db-user", "ops", "--db-port", "3307"])
    config = read_config(args)
    assert config.db_name == "from_flag"  # flag beats env
    assert config.db_port == "3307"  # flag beats env
    assert config.db_user == "ops"  # flag-only (no env)
    assert config.db_host == "localhost"  # falls through to default
