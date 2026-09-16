"""验证自选文件可以勾选/取消，且不会触发真实下载。"""
from playwright.sync_api import sync_playwright


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    page.goto("http://127.0.0.1:8876/", wait_until="networkidle", timeout=120_000)
    page.wait_for_selector("#caseRows .case-check:not(:disabled)", timeout=120_000)
    row = page.locator("#caseRows tr").filter(has_text="m649").first
    row.locator(".case-check").check()
    page.locator("#downloadBtn").click()
    page.wait_for_selector("#downloadDialog[open]", timeout=30_000)
    page.wait_for_selector(".scope-tabs button[data-scope='custom']", timeout=30_000)
    page.locator(".scope-tabs button[data-scope='custom']").click()
    page.wait_for_selector(".file-check", timeout=30_000)
    checks = page.locator(".file-check")
    initial = checks.locator(":checked").count()
    checks.first.uncheck()
    after_uncheck = checks.locator(":checked").count()
    if initial <= after_uncheck:
        raise SystemExit(f"checkbox did not change: {initial} -> {after_uncheck}")
    if page.locator("#confirmDownload").is_disabled():
        raise SystemExit("custom download confirm unexpectedly disabled")
    print({"file_rows": checks.count(), "checked_before": initial, "checked_after_uncheck": after_uncheck})
    browser.close()
