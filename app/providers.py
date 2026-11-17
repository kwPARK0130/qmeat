import os, httpx, re
from defusedxml import ElementTree
from .domain import dec, month

SOURCES = {
    "customs": {
        "name": "관세청 수출입실적",
        "url": "https://www.data.go.kr/data/15100475/openapi.do",
        "file_url": "https://tradedata.go.kr/",
        "population": "통관 · CIF USD",
        "key": "CUSTOMS_API_KEY",
        "filters": ["period", "code", "country"],
        "verification": "공식 문서 접근이 환경에서 차단됨. 실연결 검증 전",
    },
    "mfds": {
        "name": "식약처 일반통계",
        "url": "https://impfood.mfds.go.kr/CFDDD01F01",
        "file_url": "https://impfood.mfds.go.kr/CFDDD01F01",
        "population": "검사 통계 · USD",
        "filters": ["period", "code", "country"],
        "verification": "공식 파일 가져오기",
    },
    "exim": {
        "name": "한국수출입은행 고시환율",
        "url": "https://www.koreaexim.go.kr/ir/HPHKIR020M01?apino=2&viewtype=C",
        "file_url": "https://www.data.go.kr/data/3068846/openapi.do",
        "population": "매매기준율 · 결제환율과 구분",
        "key": "EXIM_API_KEY",
        "filters": ["date"],
        "verification": "공식 문서 접근이 환경에서 차단됨. 실연결 검증 전",
    },
}


class SourceError(Exception):
    def __init__(self, message, transient=False, retry_after=None):
        super().__init__(message)
        self.transient = transient
        self.retry_after = retry_after


def normalize(row, source, weight_unit, amount_unit, version, country_role):
    if source not in ("customs", "mfds"):
        raise ValueError("지원하지 않는 통계 제공처")
    if weight_unit not in ("kg", "ton") or amount_unit not in ("USD", "1000USD"):
        raise ValueError("중량·금액 단위를 지정하세요")
    code = str(row.get("code", "")).strip()
    if not re.fullmatch(r"[0-9]{2,12}", code):
        raise ValueError("품목코드는 선행 0을 유지한 2~12자리 문자열이어야 합니다")
    country = str(row.get("country", "")).strip().upper()
    if not country or len(country) > 50:
        raise ValueError("국가가 필요합니다")
    if country_role not in ("origin", "export", "manufacture", "statistical"):
        raise ValueError("국가 역할이 필요합니다")
    if not version.strip():
        raise ValueError("분류 버전이 필요합니다")
    species = str(row.get("species") or "기타").strip()
    storage = str(row.get("storage") or "미분류").strip()
    if species not in ("돼지", "소", "닭", "양", "기타"):
        raise ValueError("축종을 확인하세요")
    if storage not in ("냉장", "냉동", "미분류"):
        raise ValueError("보관 분류를 확인하세요")
    return {
        "source": source,
        "series": "imports",
        "classification_version": version,
        "code": code,
        "country_role": country_role,
        "country": country,
        "period": month(str(row.get("period", "")).strip()),
        "species": species,
        "storage": storage,
        "product": str(row.get("product", code))[:200],
        "weight_kg": str(
            dec(row.get("weight"), "중량", 0) * (1000 if weight_unit == "ton" else 1)
        ),
        "amount_usd": str(
            dec(row.get("amount"), "금액", 0)
            * (1000 if amount_unit == "1000USD" else 1)
        ),
        "original_weight_unit": weight_unit,
        "original_amount_unit": amount_unit,
        "currency": "USD",
        "provisional": str(row.get("provisional", "")).lower() in ("true", "1", "잠정"),
        "population": SOURCES[source]["population"],
    }


def get(url, params):
    try:
        r = httpx.get(url, params=params, timeout=30, follow_redirects=False)
    except httpx.HTTPError:
        raise SourceError("제공처 네트워크 연결 실패", True)
    if r.status_code in (429, 500, 502, 503, 504):
        retry = r.headers.get("Retry-After", "")
        raise SourceError(
            f"제공처 일시 오류 HTTP {r.status_code}",
            True,
            int(retry) if retry.isdigit() else None,
        )
    if r.status_code != 200:
        raise SourceError(f"제공처 HTTP {r.status_code}: 인증·접근 설정을 확인하세요")
    if len(r.content) > 20_000_000:
        raise SourceError("제공처 응답이 허용 크기를 초과했습니다")
    return r


def collect(req):
    source = req["source"]
    key = os.getenv(SOURCES[source].get("key", ""))
    if source == "mfds":
        raise SourceError("자동 API 미지원: 공식 파일을 가져오세요")
    if not key:
        raise SourceError(
            "인증 필요: 서버 환경 설정에 " + SOURCES[source]["key"] + "를 등록하세요"
        )
    if os.getenv("QMEAT_ENABLE_UNVERIFIED_API") != "1":
        raise SourceError(
            "공식 API 경로·단위 검증 후 QMEAT_ENABLE_UNVERIFIED_API=1로 활성화하세요"
        )
    if source == "customs":
        r = get(
            "https://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList",
            {
                "serviceKey": key,
                "strtYymm": req["start"].replace("-", ""),
                "endYymm": req["end"].replace("-", ""),
                "hsSgn": req["code"],
                "cntyCd": req["country"],
            },
        )
        try:
            root = ElementTree.fromstring(r.content)
        except Exception:
            raise SourceError("관세청 XML 형식 오류")
        result = root.findtext(".//resultCode")
        if result not in ("00", "000", "0"):
            raise SourceError("관세청 업무 오류: 응답 코드 " + str(result))
        rows = []
        for item in root.findall(".//item"):
            raw = {e.tag: e.text or "" for e in item}
            period = re.sub(r"[^0-9]", "", raw.get("year", ""))
            if len(period) != 6:
                continue  # annual totals are not monthly observations
            row = normalize(
                {
                    "period": period[:4] + "-" + period[4:],
                    "code": raw.get("hsCd", req["code"]),
                    "country": raw.get("statCd", req["country"]),
                    "product": raw.get("statKor"),
                    "weight": raw.get("impWgt"),
                    "amount": raw.get("impDlr"),
                },
                "customs",
                "kg",
                "USD",
                req["version"],
                "statistical",
            )
            rows.append(row)
        total = root.findtext(".//totalCount")
        if total and int(total) > len(root.findall(".//item")):
            raise SourceError("응답 페이지 누락: 완전한 수집을 확인할 수 없습니다")
        return rows, r.text
    r = get(
        "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON",
        {"authkey": key, "searchdate": req["date"].replace("-", ""), "data": "AP01"},
    )
    try:
        data = r.json()
    except ValueError:
        raise SourceError("환율 JSON 형식 오류")
    if not isinstance(data, list):
        raise SourceError("환율 응답 형식 오류")
    rows = []
    for item in data:
        if item.get("result") != 1:
            raise SourceError("환율 인증 또는 업무 오류")
        cur = item["cur_unit"]
        base = 100 if "(100)" in cur else 1
        rows.append(
            {
                "date": req["date"],
                "currency": cur.replace("(100)", ""),
                "kind": "매매기준율",
                "original_unit": base,
                "original_rate": item["deal_bas_r"],
                "rate": str(dec(item["deal_bas_r"]) / base),
                "source": "exim",
            }
        )
    return rows, r.text
