from playwright.sync_api import sync_playwright, expect
from pathlib import Path
import tempfile, subprocess, sys, os, time
from urllib.request import urlopen

# A separate ephemeral database ensures fixture values never enter the real workspace.
tmp = tempfile.TemporaryDirectory()
server = subprocess.Popen(
    [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "3011",
    ],
    env={
        **os.environ,
        "QMEAT_DB": tmp.name + "/test.sqlite3",
        "QMEAT_WORKER": "0",
        "QMEAT_USERS": "{}",
    },
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
import atexit

atexit.register(lambda: (server.terminate(), server.wait(timeout=5), tmp.cleanup()))
for attempt in range(50):
    try:
        urlopen("http://127.0.0.1:3011/api/health", timeout=1)
        break
    except OSError:
        time.sleep(0.1)
Path("artifacts").mkdir(exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
    )
    page = browser.new_page(
        viewport={"width": 1440, "height": 1100}, device_scale_factor=1
    )
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("http://127.0.0.1:3011")
    page.wait_for_selector(".kpi")
    page.screenshot(path="artifacts/test-empty-dashboard.png", full_page=True)
    page.get_by_role("button", name="파일 가져오기", exact=False).first.click()
    page.locator("#import-file").set_input_files(
        {
            "name": "official-test.csv",
            "mimeType": "text/csv",
            "buffer": b"period,code,country,weight,amount,product\n2026-10,020329,US,100,200,test\n2026-10,020329,AU,900,3600,test\n",
        }
    )
    page.locator("#import-form [name=version]").fill("HS2022")
    page.locator("#import-form [name=weight_unit]").select_option("kg")
    page.locator("#import-form [name=amount_unit]").select_option("USD")
    page.get_by_role("button", name="파일 미리보기·검증").click()
    page.get_by_role("button", name="확정 저장", exact=True).wait_for()
    assert "정상 2행" in page.locator("#import-preview").inner_text()
    page.get_by_role("button", name="확정 저장", exact=True).click()
    expect(page.locator("#alert")).to_contain_text("가져오기 완료")
    page.locator("nav [data-page=overview]").click()
    expect(page.locator("#kpis")).to_contain_text("3.8")
    page.locator("nav [data-page=calculator]").click()
    for k, v in {
        "price": "2",
        "purchase_kg": "1000",
        "saleable_kg": "1000",
        "exchange_rate": "1400",
        "sold_kg": "400",
        "selling_price": "4000",
    }.items():
        page.locator(f"#calc-form [name={k}]").fill(v)
    page.locator(".cost-status").evaluate_all("els=>els.forEach(e=>e.value='na')")
    page.locator(".cost-line").nth(3).locator(".cost-status").select_option(
        "additional"
    )
    page.locator(".cost-line").nth(3).locator(".cost-amount").fill("200000")
    page.get_by_role("button", name="원가·판매가격 계산", exact=True).click()
    page.locator("#save-calc").wait_for()
    assert "3,000" in page.locator("#calc-result").inner_text()
    assert "3,750" in page.locator("#calc-result").inner_text()
    page.get_by_role("button", name="계산 스냅샷 저장", exact=True).click()
    expect(page.locator("#alert")).to_contain_text("수식 버전을 저장")
    page.screenshot(path="artifacts/test-calculator.png", full_page=True)
    page.locator("nav [data-page=saved]").click()
    page.get_by_role("button", name="입력 복사·새 버전 계산").wait_for()
    page.locator("nav [data-page=market]").click()
    page.locator("#save-search").click()
    page.locator("#save-search-form [name=name]").fill("관심 돈육")
    page.locator("#save-search-form button.primary").click()
    page.locator("nav [data-page=saved]").click()
    page.get_by_role("button", name="저장 조건 조회 →").click()
    page.wait_for_selector("#page-market:not([hidden])")
    for width in [768, 390]:
        page.set_viewport_size({"width": width, "height": 1000})
        page.locator("nav [data-page=overview]").click()
        page.wait_for_timeout(150)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"Overflow {width}"
        page.screenshot(path=f"artifacts/test-dashboard-{width}.png", full_page=True)
    assert not errors, errors
    browser.close()
print(
    "Browser checks passed: import, weighted statistics, calculator, snapshot, saved search, tablet/mobile layout; no JS errors."
)
