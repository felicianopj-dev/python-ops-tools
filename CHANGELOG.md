# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0]

### Added

- CLI flags for the three previously env-only tools (`backup_db.py`,
  `backup_all_dbs.py`, `api_health_check.py`): each now has a `parse_args()`
  layer and `--help`, with precedence **flag > env var > default**. Existing
  env/cron usage is unchanged. Secrets stay env-only (the DB password and
  `api_health_check`'s `HEADER_AUTH` token are never accepted on argv).
- `data/sample_db_data.json`, a local/DB-view fixture for the reconciliation
  checker. The no-database demo now compares it against the API-view fixture and
  surfaces real discrepancies instead of a perfect self-match; a regression test
  pins the exact differences.
- `Automation` section in the README with copy-paste scheduling examples (cron, a
  systemd service + timer, and a scheduled GitHub Actions workflow snippet running
  `api_health_check` against a public URL).

### Changed

- Sharpened the README intro toward automation and noted that each tool runs
  directly or imports cleanly for testing.
- Removed the only emojis in the codebase (the `log_alert_aggregator` summary
  header) in favour of plain text.

## [0.2.0]

### Added

- `mypy` type checking in CI (pragmatic `[tool.mypy]` over `scripts/`) plus
  `types-requests`/`types-PyMySQL` stubs.
- `.pre-commit-config.yaml` running the `ruff` lint + format hooks locally.
- Test suites for the three earliest scripts (`api_health_check`, `backup_db`,
  `backup_all_dbs`); the unit suite grew from 47 to 84 tests.
- Shared `scripts/oplog.py` logging helper (structured JSON or human text). Tools
  that print human reports gained a `--json` flag (and respect `LOG_JSON`) to also
  emit a machine-readable summary line; the JSON-emitting tools accept `LOG_JSON=0`
  to switch to human text. Removes the duplicated `utc_ts`/`log_json` helpers.
- Per-tool packaging extras: `reconciliation` (PyMySQL) and `http` (requests),
  plus an `all` convenience extra. Core `dependencies` is now empty (most tools
  are stdlib-only); `requirements.txt` still installs everything.
- Gated DB integration tests (`pytest -m integration`) that exercise the backup,
  verify and reconciliation tools against a live MySQL (via env vars or
  `testcontainers`), plus a CI `integration` job using a `services: mysql`.

### Changed

- Aligned the three earliest scripts to the conventions of the newer tools:
  `main(argv=None)` signatures and shared connection handling.
- `backup_db.py` and `backup_all_dbs.py` now honour `DB_PORT` and
  `DB_PASSWORD`/`MYSQL_PWD` (via `build_env`), mirroring `verify_backup.py`;
  `backup_db.py` also dumps routines/triggers/events and surfaces `mysqldump`
  stderr on failure.
- `api_health_check.py` now performs HTTP via `retry_client.ResilientClient`
  instead of `urllib`, unifying retry/backoff behaviour. Retries now apply only
  to transient failures (timeouts, connection errors, 5xx/408/429); other
  unexpected statuses fail fast instead of being retried.

### Fixed

- `api_health_check.py` exit codes now follow the repo scheme: `1` when checks
  ran but found problems, `2` for configuration errors (previously `2`/`3`).
- `verify_backup.py` restore no longer raises `ValueError: flush of closed file`
  on Python 3.12: it drains stderr and waits instead of calling `communicate()`
  after closing the mysql process's stdin (found by the new integration tests).

## [0.1.0]

### Added

- Initial release: `backup_db.py`, `backup_all_dbs.py`, `api_health_check.py`,
  `reconciliation_checker.py`, `log_alert_aggregator.py`, `retry_client.py`,
  `verify_backup.py`, with a pytest suite and a ruff + pytest CI workflow.

[Unreleased]: https://github.com/felicianopj-dev/python-ops-tools/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/felicianopj-dev/python-ops-tools/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/felicianopj-dev/python-ops-tools/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/felicianopj-dev/python-ops-tools/releases/tag/v0.1.0
