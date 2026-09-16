"""桌面快捷方式入口：按需启动服务，再打开浏览器。"""

from __future__ import annotations

import subprocess
import sys
import time
import json
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import messagebox


ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "server.py"
HOST = "127.0.0.1"
PORT = 8877
URL = f"http://{HOST}:{PORT}/"
HEALTH = f"http://{HOST}:{PORT}/api/health"


def ready() -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=1.2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and "wsl2" in payload.get("features", [])
    except Exception:
        return False


def main() -> None:
    if not ready():
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(
            [sys.executable, str(SERVER), "--host", HOST, "--port", str(PORT), "--no-browser"],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not ready():
            time.sleep(0.35)
    if not ready():
        messagebox.showerror("PAWS 本地运行监控", "监控服务启动失败。请检查端口 8877 或运行日志。")
        return
    webbrowser.open(URL)


if __name__ == "__main__":
    main()
