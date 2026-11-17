from decimal import Decimal, InvalidOperation, localcontext
from datetime import date
import re

POLICY = "decimal-v1; intermediate:28 digits; display:2dp; KRW settlement not rounded"


def dec(value, name="값", minimum=None):
    try:
        d = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name}: 유효한 숫자가 필요합니다")
    if (
        not d.is_finite()
        or abs(d) > Decimal("1e20")
        or (minimum is not None and d < minimum)
    ):
        raise ValueError(f"{name}: 허용 범위를 벗어났습니다")
    return d


def month(value):
    if not re.fullmatch(r"\d{4}-\d{2}", str(value)):
        raise ValueError("기간은 YYYY-MM 형식입니다")
    date.fromisoformat(value + "-01")
    return value


def shift(value, delta):
    y, m = map(int, month(value).split("-"))
    n = y * 12 + m - 1 + delta
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


def months(start, end):
    month(start)
    month(end)
    if start > end or end > shift(start, 1199):
        raise ValueError("조회 기간은 1~1200개월입니다")
    out = []
    while start <= end:
        out.append(start)
        start = shift(start, 1)
    return out


def chunks(start, end):
    seq = months(start, end)
    return [(seq[i], seq[min(i + 11, len(seq) - 1)]) for i in range(0, len(seq), 12)]


def change(current, previous):
    if current is None or previous is None:
        return {"value": None, "reason": "비교자료 없음"}
    if dec(previous) == 0:
        return {"value": None, "reason": "비교값 0"}
    return {
        "value": str((dec(current) - dec(previous)) / dec(previous) * 100),
        "reason": None,
    }


def unit_price(price, unit, box_kg=None):
    factors = {
        "kg": Decimal(1),
        "lb": Decimal("0.45359237"),
        "100g": Decimal(".1"),
        "ton": Decimal(1000),
    }
    factor = (
        dec(box_kg, "박스 순중량", Decimal(".000001"))
        if unit == "box"
        else factors.get(unit)
    )
    if factor is None:
        raise ValueError("지원하지 않는 가격 단위")
    return dec(price, "단가", 0) / factor


def calculate(p):
    with localcontext() as ctx:
        ctx.prec = 28
        buy = dec(p.get("purchase_kg"), "구매중량", Decimal(".000001"))
        usable = dec(p.get("saleable_kg"), "판매가능 중량", Decimal(".000001"))
        sold = dec(p.get("sold_kg", 0), "판매중량", 0)
        if usable > buy or sold > usable:
            raise ValueError(
                "판매가능 중량 ≤ 구매중량, 판매중량 ≤ 판매가능 중량이어야 합니다"
            )
        fx = dec(p.get("exchange_rate"), "적용환율", Decimal(".000001"))
        product_foreign = (
            unit_price(p.get("price"), p.get("unit", "kg"), p.get("box_kg")) * buy
        )
        product = product_foreign * fx
        costs = Decimal(0)
        cash_extra = Decimal(0)
        missing = []
        lines = []
        for c in p.get("costs", []):
            status = c.get("status", "missing")
            name = str(c.get("name", "비용"))[:100]
            if status not in ("included", "additional", "na", "missing"):
                raise ValueError("잘못된 비용 상태")
            amount = Decimal(0)
            if status == "missing":
                missing.append(name)
            if status == "additional":
                amount = dec(c.get("amount"), "비용", 0) * dec(
                    c.get("rate", 1), "비용 환율", Decimal(".000001")
                )
                cash_extra += amount
                if not c.get("recoverable", False):
                    costs += amount
            lines.append(
                {
                    "name": name,
                    "status": status,
                    "krw": str(amount),
                    "recoverable": bool(c.get("recoverable", False)),
                }
            )
        required = {"운임", "보험", "검사·통관", "국내 운송", "보관", "세금"}
        missing += sorted(required - {c["name"] for c in lines})
        deduction = dec(p.get("cost_deduction", 0), "원가 차감", 0)
        total = product + costs - deduction
        if total < 0:
            raise ValueError("원가 차감은 총원가를 초과할 수 없습니다")
        per = total / usable
        fee = dec(p.get("commission_pct", 0), "수수료율", 0) / 100
        margin = dec(p.get("margin_pct", 20), "목표이익률", 0) / 100
        if fee + margin >= 1:
            raise ValueError("수수료율 + 목표이익률은 100% 미만이어야 합니다")
        fixed = dec(p.get("selling_cost_per_kg", 0), "kg당 판매비", 0)
        sale = dec(p.get("selling_price", 0), "판매단가", 0) * sold
        discount = dec(p.get("discount", 0), "할인", 0)
        if discount > sale:
            raise ValueError("할인은 판매금액을 초과할 수 없습니다")
        revenue = sale - discount
        allocated = per * sold
        profit = revenue - allocated - fixed * sold - revenue * fee
        result = {
            "product_foreign": product_foreign,
            "product_krw": product,
            "landed_cost": total,
            "cost_per_kg": per,
            "allocated_cost": allocated,
            "remaining_cost": total - allocated,
            "target_price": (per + fixed) / (1 - fee - margin),
            "markup_price": per * (1 + dec(p.get("markup_pct", 20), "가산율", 0) / 100),
            "net_revenue": revenue,
            "profit": profit,
            "profit_pct": profit / revenue * 100 if revenue else None,
            "cash_required": product + cash_extra - deduction,
        }
        if p.get("settlement_status") == "settled" and missing:
            raise ValueError("미입력 비용이 있으면 실제 정산으로 저장할 수 없습니다")
        return {
            **{k: str(v) if v is not None else None for k, v in result.items()},
            "missing_costs": missing,
            "status": "estimate_incomplete"
            if missing
            else p.get("settlement_status", "estimate"),
            "lines": lines,
            "calculation_policy": POLICY,
            "formula_version": "1.0",
        }
