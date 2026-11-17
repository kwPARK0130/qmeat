import csv, io, zipfile, hashlib
from openpyxl import load_workbook, Workbook
from .providers import normalize

MAX_BYTES = 10 * 1024 * 1024


def read_file(name, data, sheet=None, header=1):
    if len(data) > MAX_BYTES:
        raise ValueError("파일은 10MB 이하로 업로드하세요")
    if not 1 <= header <= 100:
        raise ValueError("헤더 행은 1~100입니다")
    if name.lower().endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp949")
        rows = list(csv.reader(io.StringIO(text)))
        sheets = ["CSV"]
    elif name.lower().endswith(".xlsx"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > 50 * 1024 * 1024:
                raise ValueError("압축 해제 크기가 너무 큽니다")
        book = load_workbook(
            io.BytesIO(data), read_only=True, data_only=False, keep_links=False
        )
        sheets = book.sheetnames
        ws = book[sheet] if sheet else book[sheets[0]]
        if ws.max_row and ws.max_row > 50001:
            raise ValueError("최대 50,000행까지 가져올 수 있습니다")
        rows = []
        for values in ws.iter_rows(values_only=True):
            rows.append(["" if v is None else str(v) for v in values])
            if len(rows) > 50001:
                raise ValueError("최대 50,000행까지 가져올 수 있습니다")
        book.close()
    else:
        raise ValueError(
            "CSV 또는 XLSX만 지원합니다. XLS·매크로 파일은 지원하지 않습니다"
        )
    if len(rows) > 50001 or len(rows) < header:
        raise ValueError("헤더 위치 또는 행 수를 확인하세요")
    return sheets, rows[header - 1], rows[header:]


def preview(name, data, options):
    sheets, headers, values = read_file(
        name, data, options.get("sheet"), int(options.get("header", 1))
    )
    mapping = options.get("mapping", {})
    valid = []
    errors = []
    keys = set()
    duplicates = 0
    for n, values_row in enumerate(values, int(options.get("header", 1)) + 1):
        if not any(values_row):
            continue
        try:
            row = {
                k: values_row[headers.index(v)]
                if v in headers and headers.index(v) < len(values_row)
                else ""
                for k, v in mapping.items()
            }
            normalized = normalize(
                row,
                options["source"],
                options.get("weight_unit"),
                options.get("amount_unit"),
                options.get("version", ""),
                options.get("country_role", ""),
            )
            key = tuple(
                normalized[k]
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
            )
            if key in keys:
                duplicates += 1
                raise ValueError("파일 내 논리키 중복: 집계·분류를 확인하세요")
            keys.add(key)
            valid.append(normalized)
        except (ValueError, KeyError) as exc:
            errors.append({"row": n, "reason": str(exc)})
    return {
        "sheets": sheets,
        "headers": headers,
        "sample": values[:5],
        "rows": valid,
        "errors": errors,
        "duplicates": duplicates,
        "file_hash": hashlib.sha256(data).hexdigest(),
        "options": options,
    }


def safe(value):
    return (
        "'" + value
        if isinstance(value, str)
        and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r"))
        else value
    )


def export(rows, metadata, fmt):
    headers = (
        list(rows[0])
        if rows
        else ["period", "code", "country", "weight_kg", "amount_usd"]
    )
    if fmt == "xlsx":
        book = Workbook()
        ws = book.active
        ws.title = "자료"
        ws.append(headers)
        for row in rows:
            vals = []
            for k in headers:
                v = row.get(k)
                if k in ("weight_kg", "amount_usd", "unit_price") and v is not None:
                    v = float(v)
                elif isinstance(v, (list, dict)):
                    v = str(v)
                vals.append(safe(v))
            ws.append(vals)
        meta = book.create_sheet("조회조건·계산정책")
        for k, v in metadata.items():
            meta.append([k, safe(str(v))])
        out = io.BytesIO()
        book.save(out)
        return out.getvalue()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([safe(row.get(k, "")) for k in headers])
    writer.writerow([])
    for k, v in metadata.items():
        writer.writerow(["# " + k, safe(str(v))])
    return out.getvalue().encode("utf-8-sig")
