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

