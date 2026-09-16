from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE = "http://127.0.0.1:8899"
OUTPUTS = Path(__file__).resolve().parents[3] / "outputs"
PICKER_OUT = OUTPUTS / "paws_local_monitor_picker_ui_smoke_20260828.png"
OUT = OUTPUTS / "paws_local_monitor_selected_ui_smoke_20260828.png"


def main() -> None:
    console_errors: list[str] = []
    with sync_playwright() as playwright:
        chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        browser = playwright.chromium.launch(headless=True, executable_path=str(chrome) if chrome.exists() else None)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("requestfailed", lambda request: console_errors.append(f"requestfailed {request.url} {request.failure}"))
        page.on("pageerror", lambda error: console_errors.append(f"pageerror {error}"))
        api_probe = page.request.get(BASE + "/api/status")
        assert api_probe.status == 404, api_probe.status
        page.goto(BASE + "/", wait_until="networkidle")
        page.wait_for_selector("#choose-target")
        assert page.locator("#target-name").inner_text() == "尚未选择 PAWS 程序"
        page.locator("#choose-target").click()
        page.wait_for_selector(".case-choice")
        choice_count = page.locator(".case-choice:not([disabled])").count()
        assert choice_count >= 1, choice_count
        page.screenshot(path=str(PICKER_OUT), full_page=True)
        page.locator(".case-choice:not([disabled])").first.click()
        page.wait_for_function("document.querySelector('#progress-percent').textContent !== '—'")
        page.wait_for_timeout(500)
        value = page.locator("#progress-percent").inner_text()
        date_value = page.locator("#sim-date").inner_text()
        state_value = page.locator("#state-label").inner_text()
        speed_value = page.locator("#avg-speed").inner_text()
        assert value not in {"—", ""}, value
        assert date_value != "等待输出", date_value
        assert state_value in {"运行中", "已完成", "失败", "状态未知"}, state_value
        assert speed_value not in {"—", ""}, speed_value
        page.screenshot(path=str(OUT), full_page=True)
        browser.close()
    print(json.dumps({"pass": not console_errors, "manual_picker_choices": choice_count, "progress_pct": value, "sim_date": date_value, "state": state_value, "avg_speed": speed_value, "console_errors": console_errors, "picker_screenshot": str(PICKER_OUT), "selected_screenshot": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
