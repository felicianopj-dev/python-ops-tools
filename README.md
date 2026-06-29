# Python Ops Tools

[![CI](https://github.com/felicianopj-dev/python-ops-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/felicianopj-dev/python-ops-tools/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A small collection of pragmatic Python utilities for day-to-day DevOps and automation tasks.

Focused on reliability, explicit configuration via environment variables, and scripts that run well in production, cron jobs, and containers. Every tool is dependency-light, typed, and covered by tests — and each runs directly (`python3 scripts/<name>.py`) or imports cleanly for testing.

## Tools at a glance

| Tool | Purpose | Quick start |
| --- | --- | --- |
| `scripts/backup_db.py` | Consistent single-database MySQL backup | `DB_NAME=app DB_USER=u python3 scripts/backup_db.py` |
| `scripts/backup_all_dbs.py` | Back up all non-system databases (gzip + JSON logs) | `DB_USER=u python3 scripts/backup_all_dbs.py` |
| `scripts/api_health_check.py` | HTTP health checks with status/JSON validation | `python3 scripts/api_health_check.py --url https://api/health` |
| `scripts/reconciliation_checker.py` | Compare a DB table vs API records for discrepancies | `python3 scripts/reconciliation_checker.py --local-file data/sample_db_data.json --api-file data/sample_api_data.json` |
| `scripts/log_alert_aggregator.py` | Aggregate ERROR/CRITICAL logs and alert via webhook | `python3 scripts/log_alert_aggregator.py data/sample_logs --pattern '*' --dry-run` |
| `scripts/retry_client.py` | Reusable HTTP client: backoff + idempotency | `python3 scripts/retry_client.py --help` |
| `scripts/verify_backup.py` | Prove a backup is restorable, not just present | `DB_USER=u python3 scripts/verify_backup.py dump.sql.gz` |

## Requirements

- Python ≥ 3.10
- For the MySQL tools: the `mysql` / `mysqldump` client binaries on `PATH`, and a reachable MySQL server

## Installation

```bash
pip install -r requirements.txt        # all runtime deps (every tool)
```

Most tools are stdlib-only. Third-party runtime deps are scoped per tool as
optional extras, so you can install only what a given tool needs:

```bash
pip install '.[reconciliation]'   # PyMySQL — reconciliation_checker.py
pip install '.[http]'             # requests — retry_client.py, api_health_check.py
pip install '.[all]'              # every tool's runtime deps
```

(The extras install from source; `console_scripts` entry points are not set up
yet, so run the tools as `python3 scripts/<name>.py`.)

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest            # run the test suite
ruff check .      # lint
ruff format .     # auto-format
mypy scripts      # type check

pre-commit install   # one-time: run ruff lint+format automatically on commit
```

See [CHANGELOG.md](CHANGELOG.md) for release notes.

---

### MySQL Single Database Backup

Small utility to create consistent MySQL backups using `mysqldump` with `--single-transaction`.

Configuration comes from environment variables or matching CLI flags (`--help` lists them); flags take precedence over env vars. The password is env-only (`DB_PASSWORD`/`MYSQL_PWD`), so credentials never land on argv.

#### Features
Timestamped `.sql` files  
Custom backup directory  
Compatible with cron and containerized environments  
Configurable via env vars or CLI flags (flags win)  

#### Usage
```bash
# Env-var form (cron/containers)
DB_NAME=app_db \
DB_USER=backup_user \
MYSQL_PWD='strong_password' \
BACKUP_DIR=/var/backups/mysql \
python3 scripts/backup_db.py

# Flag form (ad-hoc); password still via env
MYSQL_PWD='strong_password' python3 scripts/backup_db.py \
  --db-name app_db --db-user backup_user --backup-dir /var/backups/mysql
```

### MySQL Full Server Backup (All Databases)

Utility to back up all non-system MySQL databases from a server using `mysqldump`.

Designed for production use: streaming backups, gzip compression, and structured JSON logs suitable for automation and monitoring.

Configuration comes from environment variables or matching CLI flags (`--help` lists them); flags take precedence over env vars. The password is env-only (`DB_PASSWORD`/`MYSQL_PWD`).

#### Features
Backs up all user databases (excludes system schemas)  
Consistent dumps using `--single-transaction`  
Gzipped backups with configurable compression level  
Streaming dump (low memory usage)  
Structured JSON logs (machine-readable)  
Cron and container friendly  
Configurable via env vars or CLI flags (flags win)  

#### Usage
```bash
# Env-var form (cron/containers)
DB_HOST=localhost \
DB_USER=backup_user \
DB_PASSWORD='strong_password' \
BACKUP_DIR=/var/backups/mysql \
GZIP_LEVEL=6 \
python3 scripts/backup_all_dbs.py

# Flag form (ad-hoc); password still via env
DB_PASSWORD='strong_password' python3 scripts/backup_all_dbs.py \
  --db-host localhost --db-user backup_user --backup-dir /var/backups/mysql --gzip-level 6
```

### API Health Check

Lightweight API health check utility designed for automation and monitoring.

Performs HTTP checks against one or multiple endpoints, validates expected status codes, and optionally verifies JSON response fields. Outputs structured JSON logs and exits with non-zero status on failure, making it suitable for cron jobs, CI/CD pipelines, and container health checks.

Configuration comes from environment variables or matching CLI flags (`--help` lists them); flags take precedence over env vars. The Authorization token is env-only (`HEADER_AUTH`), so it never lands on argv.

#### Features
Single or multiple endpoint checks  
Configurable timeouts and retries  
Expected HTTP status validation  
Optional JSON field validation  
Structured JSON logs (stdout)  
Cron, CI/CD, and container friendly  
Configurable via env vars or CLI flags (flags win)  

#### Usage
```bash
# Env-var form (cron/CI)
URL="https://api.example.com/health" \
EXPECT_STATUS=200 \
python3 scripts/api_health_check.py

# Flag form (ad-hoc)
python3 scripts/api_health_check.py \
  --url https://api.example.com/health --expect-status 200 --expect-json status=ok
```
#### Environment Variables / CLI Flags

Each setting can be supplied as an env var or the matching flag (flag wins):

`URL` / `--url` – single endpoint to check  
`TARGETS` / `--targets` – comma-separated list of endpoints  
`METHOD` / `--method` – HTTP method (default: GET)  
`TIMEOUT_SECONDS` / `--timeout-seconds` – request timeout in seconds (default: 5)  
`RETRIES` / `--retries` – retry attempts (default: 1)  
`RETRY_DELAY_MS` / `--retry-delay-ms` – delay between retries in milliseconds  
`EXPECT_STATUS` / `--expect-status` – expected HTTP status codes (comma-separated)  
`EXPECT_JSON` / `--expect-json` – expected top-level JSON fields  
`USER_AGENT` / `--user-agent` – User-Agent header  
`INSECURE_TLS` / `--insecure-tls` – disable TLS verification (use with caution)  
`FOLLOW_REDIRECTS` / `--follow-redirects` – follow HTTP redirects (default: on)  
`HEADER_AUTH` – Authorization header value (env-only)  
`LOG_JSON` – `0` for human-readable text instead of JSON logs (env-only)  

## Data Integrity

### Reconciliation Checker

Compares records between two data sources to surface discrepancies — a common fintech ops task such as confirming that a local payments table matches an external processor's API view (status, amounts, IDs).

Records are matched on a key field (default: `transaction_id`). The local source is a MySQL table (connection settings reuse the `backup_db.py` environment-variable pattern); the remote source is a JSON file of "API records". Two fixtures are bundled for demos and testing: `data/sample_api_data.json` (the API view) and `data/sample_db_data.json` (the local/DB view). The two differ in a handful of records — a missing transaction on each side and a few field mismatches — so the demo surfaces real discrepancies instead of a perfect match.

#### Features
Matches records by a configurable key field  
Reports records missing on each side and field-level mismatches (e.g. amount or status)  
Clean summary report to stdout, with optional CSV export  
`argparse` CLI for table name, key, compared fields, and output path  
Demo mode (`--local-file`) to run without a database  
Basic error handling for missing config, connection failures, and malformed records  

#### Usage
```bash
# Compare MySQL table `transactions` against the bundled sample API file
DB_NAME=payments \
DB_USER=ops \
MYSQL_PWD='strong_password' \
python3 scripts/reconciliation_checker.py \
  --table transactions \
  --api-file data/sample_api_data.json \
  --key transaction_id \
  --fields amount,status \
  --csv /tmp/reconciliation_report.csv

# Demo without a database: use JSON files for both sides
# (the DB view vs the API view differ, so this reports discrepancies)
python3 scripts/reconciliation_checker.py \
  --local-file data/sample_db_data.json \
  --api-file data/sample_api_data.json
```

#### CLI Options

`--table` – local MySQL table to read (uses `DB_*` env vars)  
`--local-file` – use a JSON file as the local source instead of MySQL (demos)  
`--api-file` – path to the JSON file with API records (required)  
`--key` – field used to match records across sources (default: `transaction_id`)  
`--fields` – comma-separated fields to compare (default: all non-key fields)  
`--csv` – optional path to also write a CSV discrepancy report  

#### Environment Variables (MySQL source)

`DB_NAME` – database name (required)  
`DB_USER` – database user (required)  
`DB_HOST` – database host (default: localhost)  
`DB_PORT` – database port (default: 3306)  
`MYSQL_PWD` – database password (standard MySQL env var)  

#### Exit Codes

`0` – ran successfully, both sides matched  
`1` – ran successfully, discrepancies found  
`2` – configuration error (missing env vars / bad arguments)  
`3` – runtime error (DB connection failure, malformed input)  

## Observability

### Log Alert Aggregator

Scans log files from multiple services/directories for high-severity entries (`ERROR`/`CRITICAL`, optionally `WARNING`), aggregates them by service and error type, and sends a consolidated summary to a chat webhook.

The payload is Slack-compatible (a JSON `{"text": ...}` body), but the webhook URL is configurable via the `WEBHOOK_URL` environment variable, so it also works with Discord and most generic webhook receivers. A `data/sample_logs/` folder with a plain-text and a JSON-lines fixture is bundled for demos.

#### Features
Scans individual files or whole directories (optionally recursive)  
Parses plain-text logs (ERROR/CRITICAL/WARNING keywords) and structured JSON/JSON-lines  
Groups and counts errors by service and error type, collapsing variable ids/numbers  
Shows first/last occurrence timestamps and top error messages (truncated)  
Sends via webhook (`WEBHOOK_URL`, never hardcoded), or `--dry-run` to print  
Skips unreadable files and reports webhook failures with a non-zero exit  

#### Usage
```bash
# Dry-run against the bundled sample logs (prints instead of sending).
# Use --pattern '*' so the .jsonl fixture is included alongside the .log file.
python3 scripts/log_alert_aggregator.py data/sample_logs --pattern '*' --dry-run

# Scan specific files and POST to a Slack webhook
WEBHOOK_URL='https://hooks.slack.com/services/XXX/YYY/ZZZ' \
  python3 scripts/log_alert_aggregator.py /var/log/app/api.log /var/log/app/worker.log

# Recurse a directory, include warnings, show the top 5 issues
WEBHOOK_URL='https://discord.com/api/webhooks/XXX/YYY' \
  python3 scripts/log_alert_aggregator.py /var/log -r --include-warnings --top 5
```

#### CLI Options

`paths` – one or more log files or directories to scan (required)  
`-r`, `--recursive` – recurse into subdirectories when a path is a directory  
`--pattern` – glob for selecting files in a directory (default: `*.log`)  
`--include-warnings` – also include WARNING entries (default: ERROR/CRITICAL only)  
`--top` – number of top issue groups to show (default: 3)  
`--max-msg-len` – truncate each sample message to N characters (default: 200)  
`--timeout` – webhook POST timeout in seconds (default: 10)  
`--dry-run` – print the summary to stdout instead of sending it  

#### Environment Variables

`WEBHOOK_URL` – target webhook URL (required unless `--dry-run`)  

#### Exit Codes

`0` – ran successfully (summary sent, or printed in `--dry-run`)  
`2` – configuration error (missing `WEBHOOK_URL` without `--dry-run`, no valid paths)  
`3` – webhook send failure  

## Utilities

### Resilient HTTP Client

A small, reusable HTTP client (`ResilientClient`) that wraps `requests` with two resilience patterns borrowed from payment-API design, generalized for any service-to-service call.

**Exponential backoff with jitter** — transient failures (connection errors, timeouts, and retryable status codes `500/502/503/504/408/429`) are retried with an exponentially growing delay plus random jitter, so many clients don't retry in lockstep ("thundering herd"). Client errors (`4xx`, except `408`/`429`) are **never** retried — they won't succeed on replay.

**Idempotency / request deduplication** — each request can carry an *idempotency key*. The first successful response for a key is cached; any later request with the same key returns the cached response instead of performing the call again. This is how payment APIs avoid charging a customer twice when a client retries after an ambiguous failure (e.g. a timeout where the charge may or may not have gone through). The cache is pluggable: in-memory by default, or a local JSON file for dedup that survives process restarts.

#### Library usage
```python
from retry_client import ResilientClient, RetryConfig, JsonFileCache

client = ResilientClient(
    config=RetryConfig(max_retries=5, base_delay=0.5),
    cache=JsonFileCache("/var/lib/app/idempotency.json"),  # omit for in-memory
)

# A retry of this call (same key) returns the cached response — no double charge.
resp = client.post(
    "https://api.example.com/charge",
    json={"amount": 1000, "currency": "USD"},
    idempotency_key="order-42",
)
print(resp.status_code, resp.json())
```

#### CLI demo
```bash
# Demonstrate retries + backoff against an endpoint that returns 500
python3 scripts/retry_client.py --url https://httpbin.org/status/500 --max-retries 3 --base-delay 0.2

# Demonstrate idempotent replay: the same key returns the cached response,
# so the second call shows the SAME uuid instead of a fresh one
python3 scripts/retry_client.py --url https://httpbin.org/uuid \
  --idempotency-key demo-1 --cache-file /tmp/idempotency.json
```

#### CLI Options

`--url` – URL to call (default: `https://httpbin.org/status/500`, returns 500 to show retries)  
`--method` – HTTP method (default: GET)  
`--max-retries` – maximum retries (default: 3)  
`--base-delay` – base backoff delay in seconds (default: 0.5)  
`--idempotency-key` – if set, the demo issues the request twice to show cached replay  
`--cache-file` – persist the idempotency cache to this JSON file (default: in-memory)  
`--timeout` – per-request timeout in seconds (default: 10)  

#### Exit Codes

`0` – request succeeded  
`2` – bad arguments / configuration  
`3` – request ultimately failed (after retries)  

## Backup Verification

### Verify Backup

Validates that a database backup (produced by `backup_db.py` or `backup_all_dbs.py`) is actually **restorable** — not merely present on disk.

**Why this matters: an untested backup is not a backup.** A dump can exist, be the expected size, and still be useless — truncated mid-write, corrupted, encoded wrong, or missing routines/triggers. The only way to know a backup will save you during an incident is to restore it. This script does exactly that: it restores the dump into a throwaway database, runs sanity checks, and tears the database down again — turning "we have backups" into "we have *verified, restorable* backups". Run it on a schedule (cron) against your latest dumps so a broken backup is discovered in advance, not during a disaster.

#### What it checks
Restore completes with no SQL errors  
Table count meets expectations (exact via `--expected-tables`, or a floor via `--min-tables`)  
Row counts for a configurable list of `--critical-tables` are non-zero  
The temporary database is always dropped afterwards, even on failure  

The restore and all queries go through the `mysql` client (same tooling as the backup scripts), so the only requirements are a reachable MySQL server and `mysql` on PATH. Both plain `.sql` and gzipped `.sql.gz` dumps are supported (detected by content). This targets single-database dumps as produced by the backup scripts.

#### Usage
```bash
DB_HOST=localhost \
DB_USER=verify_user \
DB_PASSWORD='strong_password' \
python3 scripts/verify_backup.py /var/backups/mysql/app_db_20260626_010000.sql.gz \
  --critical-tables users,transactions,accounts \
  --min-tables 5
```

#### CLI Options

`backup_file` – path to the dump to verify, `.sql` or `.sql.gz` (required)  
`--critical-tables` – comma-separated tables that must exist and have non-zero rows  
`--expected-tables` – exact number of tables the restored database must contain  
`--min-tables` – minimum number of tables required (default: 1)  
`--temp-db` – override the generated temporary database name  
`--keep-temp-db` – keep the temp database afterwards (for debugging)  

#### Environment Variables

`DB_HOST` – server host (default: localhost)  
`DB_PORT` – server port (default: 3306)  
`DB_USER` – user name, needs privileges to create/drop databases (required)  
`DB_PASSWORD` – password (optional; `MYSQL_PWD` is honored as a fallback)  

#### Exit Codes

`0` – verification PASSED  
`1` – verification FAILED (restore error or a failed sanity check)  
`2` – configuration error (missing `DB_USER`, bad arguments, missing/empty file)  
`3` – runtime error (could not connect / create / drop the temp database)  

## Automation

These tools are built to run unattended. Configuration comes from the environment (so no secrets live in your crontab or shell history), and every tool returns a meaningful exit code — a non-zero exit fails the cron mail, systemd unit, or CI job on its own, turning a problem into an alert without any extra code.

Keep credentials in a root-owned env file with restricted permissions and source it, rather than inlining passwords (the password always reaches MySQL via `MYSQL_PWD`, never on the command line):

```bash
# /etc/ops-tools.env  (chmod 600)
DB_HOST=127.0.0.1
DB_USER=backup_user
MYSQL_PWD=...                 # password via env, never on argv
WEBHOOK_URL=https://hooks.slack.com/services/XXX
```

### cron

```cron
# Health check every 5 minutes (a non-zero exit makes cron email you)
*/5 * * * *  cd /opt/python-ops-tools && python3 scripts/api_health_check.py --url https://api.example.com/health --expect-status 200

# Back up all databases nightly at 02:00
0 2 * * *    set -a; . /etc/ops-tools.env; set +a; cd /opt/python-ops-tools && BACKUP_DIR=/var/backups/mysql python3 scripts/backup_all_dbs.py

# Weekly: prove the most recent dump is actually restorable (Sun 03:00)
0 3 * * 0    set -a; . /etc/ops-tools.env; set +a; cd /opt/python-ops-tools && python3 scripts/verify_backup.py "$(ls -t /var/backups/mysql/*.sql.gz | head -1)" --critical-tables users,orders
```

### systemd timer

A `.service` (what to run) plus a `.timer` (when) — the modern alternative to cron, with output captured in `journalctl` and `EnvironmentFile=` for secrets.

```ini
# /etc/systemd/system/api-health.service
[Unit]
Description=API health check

[Service]
Type=oneshot
EnvironmentFile=/etc/ops-tools.env
WorkingDirectory=/opt/python-ops-tools
ExecStart=/usr/bin/python3 scripts/api_health_check.py --url https://api.example.com/health --expect-status 200
```

```ini
# /etc/systemd/system/api-health.timer
[Unit]
Description=Run the API health check every 5 minutes

[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now api-health.timer
systemctl list-timers api-health.timer    # confirm the next scheduled run
```

### GitHub Actions (scheduled)

Drop this into `.github/workflows/` to run a check on a schedule in CI (and on demand via the "Run workflow" button). The job turns red when the endpoint is unhealthy, so the Actions tab doubles as a lightweight uptime history.

```yaml
name: Scheduled health check
on:
  schedule:
    - cron: "*/30 * * * *"     # every 30 minutes
  workflow_dispatch:            # plus a manual trigger

jobs:
  health:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: python3 scripts/api_health_check.py --url https://example.com --expect-status 200
```

