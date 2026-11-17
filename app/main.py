import os, json, uuid, threading, secrets, re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from . import store, domain, providers, jobs, query, files


@asynccontextmanager
async def lifespan(app):
    store.init()
    stop = threading.Event()
    thread = threading.Thread(target=jobs.worker, args=(stop,), daemon=True)
    if os.getenv("QMEAT_WORKER", "1") == "1":
        thread.start()
    yield
    stop.set()
    if thread.is_alive():
        thread.join(timeout=3)


app = FastAPI(title="Qmeat 시장 정보", lifespan=lifespan)


@app.exception_handler(ValueError)
async def invalid(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=422)


@app.exception_handler(KeyError)
async def missing(request, exc):
    return JSONResponse({"detail": "필수 입력 필드가 누락되었습니다"}, status_code=422)


@app.middleware("http")
async def safety(request, call_next):
    if int(request.headers.get("content-length", "0")) > 12 * 1024 * 1024:
        return JSONResponse({"detail": "요청 크기는 12MB 이하여야 합니다"}, 413)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "다른 사이트에서 요청할 수 없습니다"}, 403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
    )
    return response


def org(request: Request):
    # Optional credentials are server-owned: clients cannot select an organization.
    users = json.loads(os.getenv("QMEAT_USERS", "{}"))
    if not users:
        return "local"
    import base64

    try:
        scheme, encoded = request.headers.get("authorization", "").split(" ", 1)
        if scheme.lower() != "basic":
            raise ValueError()
        name, password = base64.b64decode(encoded).decode().split(":", 1)
        user = users[name]
        if not secrets.compare_digest(password, user["password"]):
            raise ValueError()
        return user["org"]
    except Exception:
        raise HTTPException(
            401,
            "인증이 필요합니다",
            headers={"WWW-Authenticate": 'Basic realm="Qmeat"'},
        )


@app.get("/")
def home():
    return FileResponse(Path(__file__).parent / "static/index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "database": "sqlite", "timezone": "Asia/Seoul"}


@app.get("/api/sources")
def sources(organization=Depends(org)):
    with store.db() as c:
        coverage = [
            dict(r)
            for r in c.execute("SELECT * FROM coverage ORDER BY checked_at DESC")
        ]
    return [
        {
            **v,
            "id": k,
            "status": "file_only"
            if k == "mfds"
            else "authentication_required"
            if not os.getenv(v.get("key", ""))
            else "verification_required"
            if os.getenv("QMEAT_ENABLE_UNVERIFIED_API") != "1"
            else "configured_unverified",
            "last_success_at": next(
                (r["checked_at"] for r in coverage if r["source"] == k), None
            ),
        }
        for k, v in providers.SOURCES.items()
    ]


def filters(request):
    end = domain.shift(datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m"), -1)
    return {
        "start": domain.shift(end, -11),
        "end": end,
        "source": "customs",
        **dict(request.query_params),
    }


@app.get("/api/stats")
def stats(request: Request, organization=Depends(org)):
    f = filters(request)
    result = query.query(f)
    page = max(1, int(f.get("page", 1)))
    size = min(200, max(1, int(f.get("size", 50))))
    result["row_count"] = len(result["rows"])
    result["rows"] = result["rows"][(page - 1) * size : page * size]
    result["page"] = page
    return result


@app.get("/api/export")
def download(request: Request, organization=Depends(org)):
    f = filters(request)
    result = query.query(f)
    fmt = "xlsx" if f.get("format") == "xlsx" else "csv"
    return Response(
        files.export(result["rows"], {"filters": f, **result["meta"]}, fmt),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="qmeat-statistics.{fmt}"'
        },
    )


@app.post("/api/calculate")
def calculate(payload: dict, organization=Depends(org)):
    return domain.calculate(payload)


@app.get("/api/documents/{kind}")
def documents(kind: str, organization=Depends(org)):
    if kind not in ("search", "calculation"):
        raise HTTPException(404)
    with store.db() as c:
        return [
            {"id": r["id"], "created_at": r["created_at"], **json.loads(r["payload"])}
            for r in c.execute(
                "SELECT * FROM documents WHERE org=? AND kind=? ORDER BY created_at DESC",
                (organization, kind),
            )
        ]


@app.post("/api/documents/{kind}")
def save_document(kind: str, payload: dict, organization=Depends(org)):
    if kind not in ("search", "calculation"):
        raise HTTPException(404)
    if len(store.dumps(payload)) > 100000:
        raise ValueError("저장 내용이 너무 큽니다")
    if kind == "calculation":
        payload = {
            "name": str(payload.get("name", "원가 계산"))[:100],
            "input": payload["input"],
            "result": domain.calculate(payload["input"]),
            "parent_id": payload.get("parent_id"),
        }
    else:
        f = payload["filters"]
        domain.month(f["start"])
        domain.month(f["end"])
        if payload.get("period_mode") not in ("fixed", "recent"):
            raise ValueError("기간 정책을 선택하세요")
        payload["months"] = min(120, max(1, int(payload.get("months", 12))))
    if payload.get("parent_id"):
        with store.db() as c:
            if not c.execute(
                "SELECT 1 FROM documents WHERE id=? AND org=?",
                (payload["parent_id"], organization),
            ).fetchone():
                raise HTTPException(404)
    id = uuid.uuid4().hex
    with store.db() as c:
        c.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?)",
            (id, organization, kind, store.dumps(payload), store.now()),
        )
    return {"id": id}


@app.get("/api/document/{id}")
def document(id: str, organization=Depends(org)):
    with store.db() as c:
        r = c.execute(
            "SELECT * FROM documents WHERE id=? AND org=?", (id, organization)
        ).fetchone()
    if not r:
        raise HTTPException(404)
    return {**dict(r), "payload": json.loads(r["payload"])}


@app.get("/api/document/{id}/export")
def export_document(id: str, organization=Depends(org)):
    doc = document(id, organization)
    p = doc["payload"]
    return Response(
        files.export(
            [{"id": id, "created_at": doc["created_at"], **p.get("result", {})}],
            {"input": p.get("input"), "policy": domain.POLICY},
            "xlsx",
        ),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="qmeat-calculation.xlsx"'
        },
    )


@app.post("/api/import/preview")
async def preview(
    file: UploadFile = File(...), options: str = Form("{}"), organization=Depends(org)
):
    data = await file.read(files.MAX_BYTES + 1)
    try:
        options = json.loads(options)
    except ValueError:
        raise ValueError("열 연결 설정 오류")
    result = files.preview(file.filename or "", data, options)
    id = uuid.uuid4().hex
    with store.db() as c:
        old = c.execute(
            "SELECT id FROM imports WHERE org=? AND hash=? AND committed=1",
            (organization, result["file_hash"]),
        ).fetchone()
        c.execute(
            "INSERT INTO imports VALUES(?,?,?,?,0,?)",
            (id, organization, result["file_hash"], store.dumps(result), store.now()),
        )
    return {
        **result,
        "rows": result["rows"][:10],
        "valid_count": len(result["rows"]),
        "id": id,
        "already_imported": bool(old),
    }


@app.post("/api/import/{id}/commit")
def commit(id: str, payload: dict, organization=Depends(org)):
    with store.db() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute(
            "SELECT * FROM imports WHERE id=? AND org=?", (id, organization)
        ).fetchone()
        if not r:
            raise HTTPException(404)
        if r["committed"]:
            return {"already_committed": True}
        p = json.loads(r["payload"])
        if p["errors"] and not payload.get("accept_valid_only"):
            raise ValueError("오류행이 있습니다. 정상행만 저장을 명시적으로 선택하세요")
        if not p["rows"]:
            raise ValueError("저장 가능한 행이 없습니다")
        counts = {"new": 0, "revised": 0, "unchanged": 0}
        for row in p["rows"]:
            counts[store.upsert(c, row, id)] += 1
        c.execute("UPDATE imports SET committed=1 WHERE id=?", (id,))
        c.execute(
            "INSERT OR IGNORE INTO raw_refs VALUES(?,?,?,?,?)",
            (
                r["hash"],
                p["options"]["source"],
                store.dumps(p["options"]),
                store.now(),
                store.dumps(p),
            ),
        )
        req = {
            "file_hash": r["hash"],
            "options": p["options"],
            "periods": sorted({row["period"] for row in p["rows"]}),
        }
        c.execute(
            "INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?,?)",
            (
                store.digest(req),
                p["options"]["source"],
                store.dumps(req),
                "file_partial",
                store.now(),
                id,
            ),
        )
    return counts


@app.get("/api/fx")
def fx(organization=Depends(org)):
    with store.db() as c:
        rows = [
            {**json.loads(r["payload"]), "fetched_at": r["fetched_at"]}
            for r in c.execute("SELECT * FROM fx ORDER BY date DESC")
        ]
    latest = {}
    for row in rows:
        latest.setdefault(row["currency"], row)
    return list(latest.values())


@app.post("/api/fx/manual")
def manual_fx(payload: dict, organization=Depends(org)):
    datetime.strptime(payload["date"], "%Y-%m-%d")
    if not re.fullmatch("[A-Z]{3}", payload["currency"]):
        raise ValueError("통화는 USD, JPY 등의 3자리 코드입니다")
    rate = domain.dec(payload["rate"], "고시환율", domain.Decimal(".000001"))
    unit = domain.dec(payload["base"], "고시 기준단위", domain.Decimal(".000001"))
    row = {
        **payload,
        "rate": str(rate / unit),
        "original_rate": str(rate),
        "original_unit": str(unit),
        "source": "user_entered",
        "kind": "사용자 확인 고시환율",
    }
    key = store.digest(
        [row["source"], row["date"], row["currency"], unit.to_eng_string()]
    )
    with store.db() as c:
        c.execute(
            "INSERT OR REPLACE INTO fx VALUES(?,?,?,?,?)",
            (key, row["date"], row["currency"], store.dumps(row), store.now()),
        )
    return row


@app.post("/api/jobs")
def refresh(payload: dict, organization=Depends(org)):
    domain.month(payload["start"])
    domain.month(payload["end"])
    domain.months(payload["start"], payload["end"])
    return {"id": jobs.enqueue(payload)}


@app.get("/api/jobs")
def get_jobs(organization=Depends(org)):
    with store.db() as c:
        result = []
        for r in c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50"):
            d = dict(r)
            d["request"] = json.loads(d["request"])
            d["tasks"] = [
                {
                    **dict(t),
                    "request": json.loads(t["request"]),
                    "counts": json.loads(t["counts"]) if t["counts"] else None,
                }
                for t in c.execute("SELECT * FROM tasks WHERE job_id=?", (r["id"],))
            ]
            result.append(d)
        return result


@app.post("/api/jobs/{id}/{action}")
def job_action(id: str, action: str, organization=Depends(org)):
    with store.db() as c:
        job = c.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone()
        if not job:
            raise HTTPException(404)
        if action == "cancel":
            c.execute(
                "UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND status IN ('queued','running')",
                (store.now(), id),
            )
            return {"id": id}
        failed = [
            json.loads(t[0])
            for t in c.execute(
                "SELECT request FROM tasks WHERE job_id=? AND status='failed'", (id,)
            )
        ]
    if action != "retry" or not failed:
        raise ValueError("재시도할 실패 구간이 없습니다")
    return {"id": jobs.enqueue(json.loads(job["request"]), failed)}


@app.get("/api/schedule")
def schedule(organization=Depends(org)):
    with store.db() as c:
        r = c.execute("SELECT payload FROM settings WHERE key='schedule'").fetchone()
    p = json.loads(r[0]) if r else {"enabled": True, "time": "12:00", "last_due": None}
    current = datetime.now(ZoneInfo("Asia/Seoul"))
    hour, minute = map(int, p["time"].split(":"))
    next_run = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= current:
        next_run += timedelta(days=1)
    return {
        **p,
        "timezone": "Asia/Seoul",
        "next_run": next_run.isoformat() if p["enabled"] else None,
        "worker_enabled": os.getenv("QMEAT_WORKER", "1") == "1",
    }


@app.post("/api/schedule")
def save_schedule(payload: dict, organization=Depends(org)):
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", payload.get("time", "")):
        raise ValueError("HH:MM 시간을 입력하세요")
    p = schedule(organization)
    p.update({"time": payload["time"], "enabled": bool(payload.get("enabled"))})
    with store.db() as c:
        c.execute(
            "INSERT OR REPLACE INTO settings VALUES(?,?)", ("schedule", store.dumps(p))
        )
    return p


app.mount(
    "/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static"
)


@app.post("/api/convert")
def convert(payload: dict, organization=Depends(org)):
    v = domain.dec(payload["value"], "값", 0)
    kind = payload["kind"]
    if kind == "lb":
        value = v * domain.Decimal(".45359237")
        unit = "kg"
    elif kind == "ton":
        value = v * 1000
        unit = "kg"
    elif kind == "price_lb":
        value = v / domain.Decimal(".45359237")
        unit = "외화/kg"
    elif kind == "100g":
        value = v * 10
        unit = "KRW/kg"
    elif kind == "fx":
        value = v * domain.dec(payload["rate"], "환율", domain.Decimal(".000001"))
        unit = "KRW"
    else:
        raise ValueError("지원하지 않는 환산")
    return {"value": str(value), "unit": unit}
