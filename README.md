# Python Ops Tools

A small collection of pragmatic Python utilities for day-to-day DevOps and infrastructure tasks.

Focused on reliability, explicit configuration via environment variables, and scripts that work well in production environments, cron jobs, and containers.

### MySQL Single Database Backup

Small utility to create consistent MySQL backups using `mysqldump` with `--single-transaction`.

Reads all configuration from environment variables and avoids hard-coded credentials.

#### Features
Timestamped `.sql` files  
Custom backup directory  
Compatible with cron and containerized environments  

#### Usage
```bash
DB_NAME=app_db \
DB_USER=backup_user \
MYSQL_PWD='strong_password' \
BACKUP_DIR=/var/backups/mysql \
python3 backup.py
```

### MySQL Full Server Backup (All Databases)

Utility to back up all non-system MySQL databases from a server using `mysqldump`.

Designed for production use: streaming backups, gzip compression, and structured JSON logs suitable for automation and monitoring.

#### Features
Backs up all user databases (excludes system schemas)  
Consistent dumps using `--single-transaction`  
Gzipped backups with configurable compression level  
Streaming dump (low memory usage)  
Structured JSON logs (machine-readable)  
Cron and container friendly  

#### Usage
```bash
DB_HOST=localhost \
DB_USER=backup_user \
DB_PASSWORD='strong_password' \
BACKUP_DIR=/var/backups/mysql \
GZIP_LEVEL=6 \
python3 backup_all_dbs.py
```

### API Health Check

Lightweight API health check utility designed for automation and monitoring.

Performs HTTP checks against one or multiple endpoints, validates expected status codes, and optionally verifies JSON response fields. Outputs structured JSON logs and exits with non-zero status on failure, making it suitable for cron jobs, CI/CD pipelines, and container health checks.

#### Features
Single or multiple endpoint checks  
Configurable timeouts and retries  
Expected HTTP status validation  
Optional JSON field validation  
Structured JSON logs (stdout)  
Cron, CI/CD, and container friendly  

#### Usage
```bash
URL="https://api.example.com/health" \
EXPECT_STATUS=200 \
python3 api_health_check.py
```
#### Environment Variables

`URL` – single endpoint to check  
`TARGETS` – comma-separated list of endpoints  
`METHOD` – HTTP method (default: GET)  
`TIMEOUT_SECONDS` – request timeout in seconds (default: 5)  
`RETRIES` – retry attempts (default: 1)  
`RETRY_DELAY_MS` – delay between retries in milliseconds  
`EXPECT_STATUS` – expected HTTP status codes (comma-separated)  
`EXPECT_JSON` – expected top-level JSON fields  
`HEADER_AUTH` – Authorization header value  
`INSECURE_TLS` – disable TLS verification (use with caution)  
`FOLLOW_REDIRECTS` – follow HTTP redirects (default: 1)  

## Data Integrity

### Reconciliation Checker

Compares records between two data sources to surface discrepancies — a common fintech ops task such as confirming that a local payments table matches an external processor's API view (status, amounts, IDs).

Records are matched on a key field (default: `transaction_id`). The local source is a MySQL table (connection settings reuse the `backup_db.py` environment-variable pattern); the remote source is a JSON file of "API records". A `data/sample_api_data.json` file with ~20 fake records is bundled for demos and testing.

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
python3 scripts/reconciliation_checker.py \
  --local-file data/sample_api_data.json \
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

