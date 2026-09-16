"""Playwright只读UI冒烟测试；不点击最终下载确认。"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


OUTPUT = Path(__file__).resolve().parents[2] / "runtime" / "ui-smoke"
OUTPUT.mkdir(parents=True, exist_ok=True)

report = {"console_errors": [], "page_errors": [], "checks": {}}
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
    page.on("console", lambda message: report["console_errors"].append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
    page.goto("http://127.0.0.1:8876/", wait_until="networkidle", timeout=120_000)
    page.wait_for_selector("#caseRows tr", timeout=120_000)
    page.wait_for_function("document.querySelectorAll('.case-check').length >= 15", timeout=120_000)
    page.screenshot(path=str(OUTPUT / "dashboard_desktop_full.png"), full_page=True)

    bootstrap = page.evaluate("fetch('/api/bootstrap').then(response => response.json())")
    report["checks"]["title"] = page.title()
    report["checks"]["configured_account_count"] = bootstrap["account_count"]
    report["checks"]["account_chips"] = page.locator(".account-chip").count()
    report["checks"]["capacity_labels"] = page.locator(".account-capacity").count()
    report["checks"]["case_rows"] = page.locator("#caseRows tr").count()
    report["checks"]["downloadable_cases"] = page.locator(".case-check:not(:disabled)").count()
    report["checks"]["warning_rows"] = page.locator(".health.warning, .health.critical").count()
    report["checks"]["expected_end_cells"] = page.locator(".expected-end[data-has-eta='true']").count()
    report["checks"]["body_scroll_width"] = page.evaluate("document.body.scrollWidth")
    report["checks"]["viewport_width"] = page.evaluate("window.innerWidth")

    page.locator("#refreshCasesBtn").click()
    page.wait_for_function("document.querySelector('#freshLabel').textContent.includes('Case')", timeout=120_000)
    partial_snapshot = page.evaluate("fetch('/api/snapshot').then(response => response.json())")
    report["checks"]["case_refresh_scope"] = partial_snapshot["refresh_scope"]
    report["checks"]["case_refresh_accounts"] = partial_snapshot["refreshed_accounts"]

    page.locator(".account-chip").first.click()
    page.locator("#searchInput").fill("canshuwa16")
    report["checks"]["filtered_rows"] = page.locator("#caseRows tr").count()
    page.locator("#searchInput").fill("")
    page.locator("#clearAccount").click()

    enabled = page.locator(".case-check:not(:disabled)")
    if enabled.count():
        enabled.first.check()
        page.locator("#downloadBtn").click()
        page.wait_for_selector("#downloadDialog[open]", timeout=30_000)
        page.wait_for_function("document.querySelector('#filePicker').textContent.includes('个文件')", timeout=30_000)
        report["checks"]["download_modal_standard_preview"] = page.locator("#filePicker").inner_text()
        report["checks"]["standard_confirm_disabled"] = page.locator("#confirmDownload").is_disabled()
        report["checks"]["standard_confirm_opacity"] = page.locator("#confirmDownload").evaluate("el => getComputedStyle(el).opacity")
        page.screenshot(path=str(OUTPUT / "dashboard_download_incomplete_warning.png"), full_page=False)
        page.locator(".scope-tabs button[data-scope='custom']").click()
        report["checks"]["custom_file_rows"] = page.locator(".file-row").count()
        page.screenshot(path=str(OUTPUT / "dashboard_download_picker.png"), full_page=False)
        page.locator("#downloadDialog .close-btn").click()

    browser.close()

(OUTPUT / "ui_smoke_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
if report["console_errors"] or report["page_errors"]:
    raise SystemExit(1)
if report["checks"].get("account_chips") != report["checks"].get("configured_account_count") or report["checks"].get("capacity_labels") != report["checks"].get("configured_account_count") or report["checks"].get("case_rows", 0) < 15:
    raise SystemExit(2)
if "标准结果不完整" in report["checks"].get("download_modal_standard_preview", "") and not report["checks"].get("standard_confirm_disabled"):
    raise SystemExit(3)
if "标准结果不完整" in report["checks"].get("download_modal_standard_preview", "") and float(report["checks"].get("standard_confirm_opacity", "1")) > 0.4:
    raise SystemExit(4)
