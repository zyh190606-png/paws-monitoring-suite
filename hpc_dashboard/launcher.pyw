from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent


def configured_url() -> str:
    """Read the dashboard port from config so the launcher stays in sync."""
    try:
        config = json.loads((APP_DIR / "config.json").read_text(encoding="utf-8"))
        host = str(config.get("listen_host", "127.0.0.1"))
        port = int(config.get("listen_port", 8876))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        host, port = "127.0.0.1", 8876
    return f"http://{host}:{port}/"


URL = configured_url()
BOOTSTRAP = URL + "api/bootstrap"


def service_is_ready() -> bool:
    try:
        with urllib.request.urlopen(BOOTSTRAP, timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def show_error(message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "PAWS HPC监控面板", 0x10)
    except Exception:
        pass


def start_service() -> bool:
    if service_is_ready():
        return True
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    try:
        subprocess.Popen(
            [sys.executable, str(APP_DIR / "app.py"), "--no-browser"],
            cwd=str(APP_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
    except Exception as exc:
        show_error(f"无法启动监控服务：\n{exc}")
        return False
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if service_is_ready():
            return True
        time.sleep(0.35)
    show_error("监控服务启动超时。\n请检查面板目录和 paws 环境是否可用。")
    return False


if __name__ == "__main__":
    if start_service():
        webbrowser.open(URL)
