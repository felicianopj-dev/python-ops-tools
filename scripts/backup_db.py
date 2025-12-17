import os
import subprocess
from datetime import datetime

DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_HOST = os.getenv("DB_HOST", "localhost")
BACKUP_DIR = os.getenv("BACKUP_DIR", "./backups")

# Validate required environment variables
if not DB_NAME:
    raise SystemExit("Error: DB_NAME environment variable is required.")

if not DB_USER:
    raise SystemExit("Error: DB_USER environment variable is required.")

os.makedirs(BACKUP_DIR, exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
filename = f"{BACKUP_DIR}/{DB_NAME}_{timestamp}.sql"

command = [
    "mysqldump",
    "-h", DB_HOST,
    "-u", DB_USER,
    "--single-transaction",
    DB_NAME,
]

with open(filename, "w") as f:
    subprocess.run(command, stdout=f, check=True)

print(f"Backup created: {filename}")

