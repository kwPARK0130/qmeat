import json
from decimal import Decimal
from . import store
from .domain import months, shift, change, POLICY


def query(f):
    start = f["start"]
    end = f["end"]
    periods = months(start, end)
    source = f.get("source", "customs")
    clauses = ["source=?", "period>=?", "period<=?"]
    args = [source, shift(start, -12), end]
    for key in ("code", "country", "species", "storage"):
        if f.get(key):
            clauses.append(f"{key}=?")
            args.append(f[key])
    with store.db() as c:
        raw = c.execute(
            "SELECT payload,fetched_at FROM observations WHERE "
            + " AND ".join(clauses),
            args,
        ).fetchall()
        cover = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM coverage WHERE source=? ORDER BY checked_at DESC",
                (source,),
            )
        ]
    allrows = [{**json.loads(r["payload"]), "fetched_at": r["fetched_at"]} for r in raw]
    if f.get("version"):
        allrows = [r for r in allrows if r["classification_version"] == f["version"]]
    if f.get("country_role"):
        allrows = [r for r in allrows if r["country_role"] == f["country_role"]]
    versions = {r["classification_version"] for r in allrows}
    roles = {r["country_role"] for r in allrows}
    if len(versions) > 1 or len(roles) > 1:
        raise ValueError(
            "분류 버전·국가 역할이 다른 자료입니다. 같은 조건을 선택하세요"
        )
    # Prevent parent/child and total/country double counting in each period.
    selected = []
    code_sets = {}
    totals = set()
    for r in allrows:
        code_sets.setdefault((r["period"], r["country"]), set()).add(r["code"])
        if r["country"] == "ALL":
            totals.add((r["period"], r["code"]))
    for row in allrows:
        codes = code_sets[(row["period"], row["country"])]
        if any(row["code"][:n] in codes for n in range(2, len(row["code"]))):
            continue
        if row["country"] != "ALL" and (row["period"], row["code"]) in totals:
            continue
        selected.append(row)

    def aggregate(rows):
        if not rows:
            return {"weight_kg": None, "amount_usd": None, "unit_price": None}
        w = sum((Decimal(r["weight_kg"]) for r in rows), Decimal(0))
        a = sum((Decimal(r["amount_usd"]) for r in rows), Decimal(0))
        return {
            "weight_kg": str(w),
            "amount_usd": str(a),
            "unit_price": str(a / w) if w else None,
        }

    bucket = {
        p: aggregate([r for r in selected if r["period"] == p])
        for p in months(shift(start, -12), end)
    }
    metric = f.get("metric", "weight_kg")
    if metric not in ("weight_kg", "amount_usd", "unit_price"):
        raise ValueError("지원하지 않는 지표")

    def compare(p, other):
        def dimensions(period):
            return {
                (r["code"], r["country"], r["storage"], r["species"])
                for r in selected
                if r["period"] == period
            }

        if dimensions(p) != dimensions(other):
            return {"value": None, "reason": "조회 범위가 달라 비교 불가"}
        return change(bucket[p][metric], bucket.get(other, {}).get(metric))

    timeline = [
        {
            "period": p,
            **bucket[p],
            "mom": compare(p, shift(p, -1)),
            "yoy": compare(p, shift(p, -12)),
        }
        for p in periods
    ]
    rows = [r for r in selected if start <= r["period"] <= end]
    total = aggregate(rows)
    countries = [
        {"country": co, **aggregate([r for r in rows if r["country"] == co])}
        for co in sorted({r["country"] for r in rows})
    ]
    for co in countries:
        co["share_pct"] = (
            str(Decimal(co["weight_kg"]) / Decimal(total["weight_kg"]) * 100)
            if total["weight_kg"] and Decimal(total["weight_kg"])
            else None
        )
    current_year = end[:4]
    ytd_periods = [p for p in months(current_year + "-01", end)]
    prev_periods = [shift(p, -12) for p in ytd_periods]

    def complete_total(ps):
        if any(p not in bucket or bucket[p]["weight_kg"] is None for p in ps):
            return None
        return aggregate([r for r in selected if r["period"] in ps])[metric]

    return {
        "rows": rows,
        "total": total,
        "timeline": timeline,
        "countries": countries,
        "ytd_change": change(complete_total(ytd_periods), complete_total(prev_periods)),
        "meta": {
            "source": source,
            "period": [start, end],
            "unit": "kg / USD / USD/kg",
            "currency": "USD",
            "classification_version": list(versions),
            "latest_data_period": max((r["period"] for r in rows), default=None),
            "fetched_at": max((r["fetched_at"] for r in rows), default=None),
            "last_success_at": cover[0]["checked_at"] if cover else None,
            "coverage_status": "partial" if rows else "not_queried",
            "share_basis": "조회 국가 중 비중 (전체시장 아님)",
            "provisional": any(r["provisional"] for r in rows),
            "calculation_policy": POLICY,
            "missing_months": [p for p in periods if bucket[p]["weight_kg"] is None],
            "coverage": cover,
            "deduplicated_hierarchy": len(allrows) - len(selected),
        },
    }
