import json
from decimal import Decimal as D
import pytest
from fastapi.testclient import TestClient
from app import store, domain, providers, jobs
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QMEAT_DB", str(tmp_path / "test.sqlite"))
    monkeypatch.setenv("QMEAT_WORKER", "0")
    monkeypatch.delenv("QMEAT_USERS", raising=False)
    with TestClient(app) as c:
        yield c


def calculation(**kwargs):
    return {
        "price": "2",
        "unit": "kg",
        "purchase_kg": "1000",
        "saleable_kg": "1000",
        "exchange_rate": "1400",
        "sold_kg": "400",
        "selling_price": "4000",
        "margin_pct": "20",
        "costs": [
            {
                "name": n,
                "status": "additional" if n == "국내 운송" else "na",
                "amount": "200000",
                "rate": "1",
            }
            for n in ["운임", "보험", "검사·통관", "국내 운송", "보관", "세금"]
        ],
        **kwargs,
    }


def row(weight="100", amount="200", period="2026-10", country="US", code="020329"):
    return providers.normalize(
        {
            "weight": weight,
            "amount": amount,
            "period": period,
            "country": country,
            "code": code,
        },
        "customs",
        "kg",
        "USD",
        "HS2022",
        "statistical",
    )


def test_cost_acceptance():
    r = domain.calculate(calculation())
    assert D(r["landed_cost"]) == 3000000
    assert D(r["cost_per_kg"]) == 3000
    assert D(r["target_price"]) == 3750
    assert D(r["markup_price"]) == 3600
    assert D(r["allocated_cost"]) == 1200000
    assert D(r["remaining_cost"]) == 1800000
    assert D(domain.calculate(calculation(commission_pct="5"))["target_price"]) == 4000
    assert (
        D(domain.calculate(calculation(exchange_rate="1410"))["product_krw"])
        - D(r["product_krw"])
        == 20000
    )
    assert abs(domain.unit_price("2", "lb") - D("4.40924524")) < D(".00000001")
    assert domain.unit_price(100, "100g") == 1000


@pytest.mark.parametrize(
    "override",
    [
        {"saleable_kg": 0},
        {"saleable_kg": 1001},
        {"margin_pct": 95, "commission_pct": 5},
        {"exchange_rate": "NaN"},
        {"sold_kg": 1001},
        {"price": -1},
    ],
)
def test_invalid_calculations(override):
    with pytest.raises(ValueError):
        domain.calculate(calculation(**override))


def test_included_and_recoverable():
    p = calculation()
    base = domain.calculate(p)
    p["costs"].append(
        {"name": "CIF 운임", "status": "included", "amount": 999999, "rate": 1400}
    )
    assert domain.calculate(p)["landed_cost"] == base["landed_cost"]
    p["costs"].append(
        {
            "name": "환급 세금",
            "status": "additional",
            "amount": 300000,
            "rate": 1,
            "recoverable": True,
        }
    )
    r = domain.calculate(p)
    assert r["landed_cost"] == base["landed_cost"]
    assert D(r["cash_required"]) == 3300000
    assert domain.calculate(calculation(costs=[]))["status"] == "estimate_incomplete"


def test_split_and_change():
    assert domain.chunks("2023-11", "2026-10") == [
        ("2023-11", "2024-10"),
        ("2024-11", "2025-10"),
        ("2025-11", "2026-10"),
    ]
    assert domain.change(1, 0)["value"] is None
    assert domain.change(None, 1)["value"] is None
    assert domain.change(120, 100)["value"] == "20.0"


def test_upsert_weighted_revision(client):
    with store.db() as c:
        assert store.upsert(c, row(), "j") == "new"
        assert store.upsert(c, row(), "j") == "unchanged"
        store.upsert(c, row("900", "3600", country="AU"), "j")
    response = client.get("/api/stats?start=2026-09&end=2026-10").json()
    assert D(response["total"]["unit_price"]) == D("3.8")
    assert response["timeline"][0]["weight_kg"] is None
    with store.db() as c:
        assert store.upsert(c, row("100", "300"), "j") == "revised"
        assert c.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
    assert D(
        client.get("/api/stats?start=2026-10&end=2026-10").json()["total"]["unit_price"]
    ) == D("3.9")


def test_hierarchy(client):
    with store.db() as c:
        store.upsert(c, row("100", "200", code="02"), "j")
        store.upsert(c, row("100", "200"), "j")
    assert (
        client.get("/api/stats?start=2026-10&end=2026-10").json()["total"]["weight_kg"]
        == "100"
    )


def test_import_export(client):
    options = {
        "source": "customs",
        "header": 2,
        "weight_unit": "ton",
        "amount_unit": "1000USD",
        "country_role": "statistical",
        "version": "HS2022",
        "mapping": {
            k: k for k in ["period", "code", "country", "weight", "amount", "product"]
        },
    }
    content = "설명행\nperiod,code,country,weight,amount,product\n2026-10,020329,US,1,2,=HYPERLINK(1)\n2026-11,020329,US,no,2,오류\n"
    p = client.post(
        "/api/import/preview",
        files={"file": ("report.csv", content.encode(), "text/csv")},
        data={"options": json.dumps(options)},
    ).json()
    assert p["valid_count"] == 1
    assert p["errors"][0]["row"] == 4
    assert p["rows"][0]["weight_kg"] == "1000"
    assert p["rows"][0]["amount_usd"] == "2000"
    assert client.post("/api/import/" + p["id"] + "/commit", json={}).status_code == 422
    assert (
        client.post(
            "/api/import/" + p["id"] + "/commit", json={"accept_valid_only": True}
        ).json()["new"]
        == 1
    )
    assert client.post("/api/import/" + p["id"] + "/commit", json={}).json()[
        "already_committed"
    ]
    import io
    from openpyxl import load_workbook

    x = client.get("/api/export?start=2026-10&end=2026-10&format=xlsx")
    wb = load_workbook(io.BytesIO(x.content))
    cells = list(wb["자료"].values)
    headers = cells[0]
    values = cells[1]
    assert values[headers.index("code")] == "020329"
    assert values[headers.index("product")].startswith("'=")
    assert isinstance(values[headers.index("weight_kg")], int)
    assert len(wb.sheetnames) == 2


def test_org_and_snapshot(client, monkeypatch):
    monkeypatch.setenv(
        "QMEAT_USERS",
        json.dumps(
            {
                "alice": {"password": "test1", "org": "a"},
                "bob": {"password": "test2", "org": "b"},
            }
        ),
    )
    p = calculation()
    saved = client.post(
        "/api/documents/calculation", auth=("alice", "test1"), json={"input": p}
    ).json()["id"]
    assert (
        client.get("/api/document/" + saved, auth=("bob", "test2")).status_code == 404
    )
    assert client.get("/api/document/" + saved).status_code == 401
    p["exchange_rate"] = "1500"
    client.post("/api/calculate", auth=("alice", "test1"), json=p)
    snap = client.get("/api/document/" + saved, auth=("alice", "test1")).json()
    assert snap["payload"]["input"]["exchange_rate"] == "1400"


def test_fx_date_units(client):
    r = client.post(
        "/api/fx/manual",
        json={
            "date": "2026-11-13",
            "currency": "JPY",
            "rate": "950",
            "base": 100,
            "reference": "official file",
        },
    )
    assert r.json()["rate"] == "9.5"
    assert client.get("/api/fx").json()[0]["date"] == "2026-11-13"


def test_job_dedup_partial_retry(client, monkeypatch):
    req = {
        "sources": ["customs"],
        "code": "020329",
        "country": "US",
        "start": "2023-11",
        "end": "2026-10",
    }
    jid = jobs.enqueue(req)
    assert jobs.enqueue(req) == jid

    def collect(req):
        if req["start"] == "2024-11":
            raise providers.SourceError("fixture permanent failure")
        return [row(period=req["start"])], "test fixture"

    monkeypatch.setattr(providers, "collect", collect)
    for _ in range(3):
        with store.db() as c:
            c.execute("UPDATE jobs SET lease_until=0")
        jobs.run_one("test")
    with store.db() as c:
        assert (
            c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0]
            == "partial_failed"
        )
        assert c.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
    retried = client.post(f"/api/jobs/{jid}/retry", json={}).json()["id"]
    with store.db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM tasks WHERE job_id=?", (retried,)
            ).fetchone()[0]
            == 1
        )
    monkeypatch.setattr(
        providers, "collect", lambda r: ([row(period=r["start"])], "test fixture 2")
    )
    jobs.run_one("test")
    with store.db() as c:
        assert (
            c.execute("SELECT status FROM jobs WHERE id=?", (retried,)).fetchone()[0]
            == "succeeded_changed"
        )


def test_unchanged_and_cancel(client, monkeypatch):
    monkeypatch.setattr(providers, "collect", lambda req: ([row()], "fixture"))
    req = {
        "sources": ["customs"],
        "code": "020329",
        "country": "US",
        "start": "2026-10",
        "end": "2026-10",
    }
    a = jobs.enqueue(req)
    jobs.run_one("test")
    b = jobs.enqueue(req)
    jobs.run_one("test")
    with store.db() as c:
        assert (
            c.execute("SELECT status FROM jobs WHERE id=?", (b,)).fetchone()[0]
            == "succeeded_unchanged"
        )
    z = jobs.enqueue(req)
    client.post(f"/api/jobs/{z}/cancel", json={})
    jobs.run_one("test")
    with store.db() as c:
        assert (
            c.execute("SELECT status FROM jobs WHERE id=?", (z,)).fetchone()[0]
            == "cancelled"
        )


def test_schedule_catchup(client):
    jobs.schedule_tick()
    jobs.schedule_tick()
    with store.db() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert json.loads(
            c.execute("SELECT payload FROM settings WHERE key='schedule'").fetchone()[0]
        )["last_due"]


def test_csrf_and_smoke(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert (
        client.post(
            "/api/calculate",
            json=calculation(),
            headers={"Origin": "https://untrusted.example"},
        ).status_code
        == 403
    )
    assert client.post("/api/calculate", json=calculation()).status_code == 200
