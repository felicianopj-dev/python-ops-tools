# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Per-tool packaging extras: `reconciliation` (PyMySQL) and `http` (requests),
  plus an `all` convenience extra. Core `dependencies` is now empty (most tools
  are stdlib-only); `requirements.txt` still installs everything.
- Shared `scripts/oplog.py` logging helper (structured JSON or human text). Tools
  that print human reports gained a `--json` flag (and respect `LOG_JSON`) to also
  emit a machine-readable summary line; the JSON-emitting tools accept `LOG_JSON=0`
  to switch to human text. Removes the duplicated `utc_ts`/`log_json` helpers.

### Changed

- `api_health_check.py` now performs HTTP via `retry_client.ResilientClient`
  instead of `urllib`, unifying retry/backoff behaviour. Retries now apply only
  to transient failures (timeouts, connection errors, 5xx/408/429); other
  unexpected statuses fail fast instead of being retried.

## [0.2.0]

### Added

- `mypy` type checking in CI (pragmatic `[tool.mypy]` over `scripts/`) plus
  `types-requests`/`types-PyMySQL` stubs.
- `.pre-commit-config.yaml` running the `ruff` lint + format hooks locally.
- Test suites for the three earliest scripts (`api_health_check`, `backup_db`,
  `backup_all_dbs`); the suite grew from 47 to 76 tests.

### Changed

- Aligned the three earliest scripts to the conventions of the newer tools:
  `main(argv=None)` signatures and shared connection handling.
- `backup_db.py` and `backup_all_dbs.py` now honour `DB_PORT` and
  `DB_PASSWORD`/`MYSQL_PWD` (via `build_env`), mirroring `verify_backup.py`;
  `backup_db.py` also dumps routines/triggers/events and surfaces `mysqldump`
  stderr on failure.

### Fixed

- `api_health_check.py` exit codes now follow the repo scheme: `1` when checks
  ran but found problems, `2` for configuration errors (previously `2`/`3`).

## [0.1.0]

### Added

- Initial release: `backup_db.py`, `backup_all_dbs.py`, `api_health_check.py`,
  `reconciliation_checker.py`, `log_alert_aggregator.py`, `retry_client.py`,
  `verify_backup.py`, with a pytest suite and a ruff + pytest CI workflow.

[Unreleased]: https://github.com/felicianopj-dev/python-ops-tools/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/felicianopj-dev/python-ops-tools/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/felicianopj-dev/python-ops-tools/releases/tag/v0.1.0
