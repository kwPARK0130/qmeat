"""Consistent online SQLite backup. Usage: .venv/bin/python scripts/backup.py /path/backup.sqlite3"""

import sqlite3, os, sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit("Provide a new backup file path")
target = Path(sys.argv[1])
if target.exists():
    raise SystemExit("Refusing to overwrite an existing backup")
source = Path(os.getenv("QMEAT_DB", "data/qmeat.sqlite3"))
if not source.exists():
    raise SystemExit("Database does not exist")
with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
print("Backup complete:", target)
