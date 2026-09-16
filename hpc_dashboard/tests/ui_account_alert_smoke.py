import json
from pathlib import Path

from playwright.sync_api import sync_playwright


REPORT = Path(__file__).resolve().parents[2] / "runtime" / "ui-account-alert-smoke.json"
REPORT.parent.mkdir(parents=True, exist_ok=True)
report = {"console_errors": [], "page_errors": [], "checks": {}}
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    page.on("console", lambda message: report["console_errors"].append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
    page.goto("http://127.0.0.1:8876/", wait_until="networkidle", timeout=120_000)
    page.locator("#manageAccountsBtn").click()
    page.wait_for_selector("#accountDialog[open]")
    report["checks"]["account_dialog"] = page.locator("#accountDialog[open]").count()
    report["checks"]["account_list_items"] = page.locator(".managed-account").count()
    report["checks"]["add_button"] = page.locator("#addAccountBtn").count()
    page.locator("#accountDialog .close-btn").click()
    report["checks"]["dialog_closed"] = page.locator("#accountDialog[open]").count() == 0
    report["checks"]["body_scroll_width"] = page.evaluate("document.body.scrollWidth")
    report["checks"]["viewport_width"] = page.evaluate("window.innerWidth")
    page.screenshot(path=str(REPORT.with_suffix(".png")), full_page=False)
    browser.close()
REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
if report["console_errors"] or report["page_errors"] or not report["checks"]["account_dialog"] or not report["checks"]["dialog_closed"]:
    raise SystemExit(1)
