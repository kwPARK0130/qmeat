import json, time, uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from . import store, providers
from .domain import chunks, shift

TERMINAL = (
    "succeeded_changed",
    "succeeded_unchanged",
    "partial_failed",
    "failed",
    "cancelled",
)


def enqueue(request, retry_tasks=None):
    sources = request.get("sources", ["customs", "exim"])
    tasks = []
    for source in sources:
        if source not in providers.SOURCES:
            raise ValueError("지원하지 않는 제공처")
        if source == "exim":
            end = datetime.now(ZoneInfo("Asia/Seoul")).date()
            tasks += [
                {"source": source, "date": str(end - timedelta(days=n))}
                for n in range(14)
            ]
        else:
            code = request.get("code", "")
            country = request.get("country", "")
            if not code or not country:
                tasks.append(
                    {
                        "source": source,
                        "start": request["start"],
                        "end": request["end"],
                        "code": code,
                        "country": country,
                        "version": request.get("version", "HS2022"),
                    }
                )
            else:
                tasks += [
                    {
                        "source": source,
                        "start": a,
                        "end": b,
                        "code": code,
                        "country": country,
                        "version": request.get("version", "HS2022"),
                    }
                    for a, b in chunks(request["start"], request["end"])
                ]
    if retry_tasks is not None:
        tasks = retry_tasks
    fingerprint = store.digest({"request": request, "retry": retry_tasks})
    jid = uuid.uuid4().hex
    with store.db() as c:
        c.execute("BEGIN IMMEDIATE")
        existing = c.execute(
            "SELECT id FROM jobs WHERE fingerprint=? AND status IN ('queued','running')",
            (fingerprint,),
        ).fetchone()
        if existing:
            return existing["id"]
        c.execute(
            "INSERT INTO jobs(id,fingerprint,request,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (
                jid,
                fingerprint,
                store.dumps(request),
                "queued",
                store.now(),
                store.now(),
            ),
        )
        for t in tasks:
            c.execute(
                "INSERT INTO tasks(job_id,request) VALUES(?,?)", (jid, store.dumps(t))
            )
    return jid


def run_one(owner):
    stamp = time.time()
    with store.db() as c:
        c.execute("BEGIN IMMEDIATE")
        job = c.execute(
            "SELECT * FROM jobs WHERE status IN ('queued','running') AND lease_until<? ORDER BY created_at LIMIT 1",
            (stamp,),
        ).fetchone()
        if not job:
            return False
        c.execute(
            "UPDATE jobs SET owner=?,lease_until=?,status='running',updated_at=? WHERE id=?",
            (owner, stamp + 120, store.now(), job["id"]),
        )
        task = c.execute(
            "SELECT * FROM tasks WHERE job_id=? AND status IN ('queued','running') AND next_at<=? ORDER BY id LIMIT 1",
            (job["id"], stamp),
        ).fetchone()
        if task:
            c.execute(
                "UPDATE tasks SET status='running',attempts=attempts+1 WHERE id=?",
                (task["id"],),
            )
    if task:
        req = json.loads(task["request"])
        rows = []
        error = None
        transient = False
        retry_after = None
        try:
            if req["source"] != "exim" and (
                not req.get("code") or not req.get("country")
            ):
                raise providers.SourceError(
                    "품목코드와 국가를 지정한 저장 검색이 필요합니다"
                )
            rows, raw = providers.collect(req)
        except providers.SourceError as exc:
            error = str(exc)
            transient = exc.transient
            retry_after = exc.retry_after
        except Exception:
            error = "제공처 응답 검증 실패: 형식과 단위를 확인하세요"
        with store.db() as c:
            current = c.execute(
                "SELECT status,owner FROM jobs WHERE id=?", (job["id"],)
            ).fetchone()
            if current["status"] == "cancelled" or current["owner"] != owner:
                return True
            if error:
                retry = transient and task["attempts"] < 3
                delay = max([60, 300, 900][min(task["attempts"], 2)], retry_after or 0)
                c.execute(
                    "UPDATE tasks SET status=?,next_at=?,error=? WHERE id=?",
                    (
                        "queued" if retry else "failed",
                        time.time() + delay,
                        error,
                        task["id"],
                    ),
                )
            else:
                counts = {"new": 0, "revised": 0, "unchanged": 0, "rejected": 0}
                h = store.digest(raw)
                # Response only; no authenticated request URL is persisted.
                c.execute(
                    "INSERT OR IGNORE INTO raw_refs VALUES(?,?,?,?,?)",
                    (h, req["source"], store.dumps(req), store.now(), raw),
                )
                for row in rows:
                    if req["source"] == "exim":
                        key = store.digest(
                            [
                                row["date"],
                                row["currency"],
                                row["kind"],
                                row["original_unit"],
                            ]
                        )
                        old = c.execute(
                            "SELECT payload FROM fx WHERE key=?", (key,)
                        ).fetchone()
                        kind = (
                            "new"
                            if not old
                            else "unchanged"
                            if old["payload"] == store.dumps(row)
                            else "revised"
                        )
                        if kind == "revised":
                            c.execute(
                                "INSERT INTO revisions(key,previous,current,job_id,changed_at) VALUES(?,?,?,?,?)",
                                (
                                    key,
                                    old["payload"],
                                    store.dumps(row),
                                    job["id"],
                                    store.now(),
                                ),
                            )
                        c.execute(
                            "INSERT OR REPLACE INTO fx VALUES(?,?,?,?,?)",
                            (
                                key,
                                row["date"],
                                row["currency"],
                                store.dumps(row),
                                store.now(),
                            ),
                        )
                    else:
                        kind = store.upsert(c, row, job["id"])
                    counts[kind] += 1
                c.execute(
                    "INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?,?)",
                    (
                        store.digest(req),
                        req["source"],
                        store.dumps(req),
                        "queried" if rows else "no_data",
                        store.now(),
                        job["id"],
                    ),
                )
                c.execute(
                    "UPDATE tasks SET status='succeeded',counts=?,error=NULL WHERE id=?",
                    (store.dumps(counts), task["id"]),
                )
    with store.db() as c:
        tasks = c.execute(
            "SELECT status,counts FROM tasks WHERE job_id=?", (job["id"],)
        ).fetchall()
        pending = any(t["status"] in ("queued", "running") for t in tasks)
        failures = sum(t["status"] == "failed" for t in tasks)
        changed = any(
            t["counts"]
            and (json.loads(t["counts"])["new"] or json.loads(t["counts"])["revised"])
            for t in tasks
        )
        status = (
            "running"
            if pending
            else "partial_failed"
            if failures and failures < len(tasks)
            else "failed"
            if failures
            else "succeeded_changed"
            if changed
            else "succeeded_unchanged"
        )
        c.execute(
            "UPDATE jobs SET status=?,lease_until=?,updated_at=? WHERE id=? AND owner=? AND status!='cancelled'",
            (status, time.time() + 2 if pending else 0, store.now(), job["id"], owner),
        )
    return True


def schedule_tick():
    current = datetime.now(ZoneInfo("Asia/Seoul"))
    with store.db() as c:
        s = c.execute("SELECT payload FROM settings WHERE key='schedule'").fetchone()
        cfg = (
            json.loads(s[0])
            if s
            else {"enabled": True, "time": "12:00", "last_due": None}
        )
        if not cfg["enabled"]:
            return
        hour, minute = map(int, cfg["time"].split(":"))
        due = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due > current:
            due -= timedelta(days=1)
        if cfg.get("last_due", "") and cfg["last_due"] >= due.isoformat():
            return
        searches = [
            json.loads(r[0])
            for r in c.execute("SELECT payload FROM documents WHERE kind='search'")
        ]
    end = shift(current.strftime("%Y-%m"), -1)
    for search in searches:
        f = search["filters"]
        start = shift(end, -35 if due.day == 1 else -2)
        enqueue(
            {
                **f,
                "start": start,
                "end": end,
                "sources": [f.get("source", "customs")],
                "scheduled_due": due.isoformat(),
            }
        )
    enqueue(
        {
            "sources": ["exim"],
            "start": shift(end, -2),
            "end": end,
            "scheduled_due": due.isoformat(),
        }
    )
    cfg["last_due"] = due.isoformat()
    with store.db() as c:
        c.execute(
            "INSERT OR REPLACE INTO settings VALUES(?,?)",
            ("schedule", store.dumps(cfg)),
        )


def worker(stop):
    owner = uuid.uuid4().hex
    last = 0
    while not stop.is_set():
        try:
            if time.time() - last > 30:
                schedule_tick()
                last = time.time()
            run_one(owner)
        except Exception:
            import logging

            logging.getLogger("qmeat").error(
                "Worker iteration failed; next iteration will recover expired leases"
            )
        stop.wait(1)
