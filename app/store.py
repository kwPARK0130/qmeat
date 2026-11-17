import sqlite3, os, json, hashlib
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(x):
    return hashlib.sha256(dumps(x).encode()).hexdigest()


def dbpath():
    return os.getenv("QMEAT_DB", "data/qmeat.sqlite3")


@contextmanager
def db():
    Path(dbpath()).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(dbpath(), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS observations(key TEXT PRIMARY KEY, source TEXT, period TEXT, code TEXT, country TEXT, species TEXT, storage TEXT, payload TEXT, hash TEXT, fetched_at TEXT);
        CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY, key TEXT, previous TEXT, current TEXT, job_id TEXT, changed_at TEXT);
        CREATE TABLE IF NOT EXISTS coverage(key TEXT PRIMARY KEY, source TEXT, request TEXT, status TEXT, checked_at TEXT, job_id TEXT);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, fingerprint TEXT, request TEXT, status TEXT, created_at TEXT, updated_at TEXT, lease_until REAL DEFAULT 0, owner TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, job_id TEXT, request TEXT, status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0, counts TEXT, error TEXT, FOREIGN KEY(job_id) REFERENCES jobs(id));
        CREATE TABLE IF NOT EXISTS raw_refs(hash TEXT PRIMARY KEY, source TEXT, request TEXT, fetched_at TEXT, content TEXT);
        CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, org TEXT, kind TEXT, payload TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY, org TEXT, hash TEXT, payload TEXT, committed INTEGER DEFAULT 0, created_at TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE IF NOT EXISTS fx(key TEXT PRIMARY KEY, date TEXT, currency TEXT, payload TEXT, fetched_at TEXT);
        """)


def upsert(c, row, job_id):
    dimensions = [
        row[k]
        for k in (
            "source",
            "series",
            "classification_version",
            "code",
            "country_role",
            "country",
            "period",
            "species",
            "storage",
        )
    ]
    key = digest(dimensions)
    h = digest(row)
    old = c.execute("SELECT * FROM observations WHERE key=?", (key,)).fetchone()
    state = "unchanged"
    if not old:
        state = "new"
    elif old["hash"] != h:
        state = "revised"
        c.execute(
            "INSERT INTO revisions(key,previous,current,job_id,changed_at) VALUES(?,?,?,?,?)",
            (key, old["payload"], dumps(row), job_id, now()),
        )
    c.execute(
        "INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            key,
            row["source"],
            row["period"],
            row["code"],
            row["country"],
            row["species"],
            row["storage"],
            dumps(row),
            h,
            now(),
        ),
    )
    return state
