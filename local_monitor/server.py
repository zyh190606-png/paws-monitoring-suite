"""本地多进程 PAWS 监控网页服务器。"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from monitor import (
    DEFAULT_SCAN_ROOT,
    DEFAULT_STALE_SECONDS,
    SpinupMonitor,
    case_profile,
    discover_running_cases,
    discover_wsl_running_cases,
    list_running_wsl_distros,
    monitor_id,
    process_info,
    render_terminal,
)


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DEFAULT_PORT = 8877


class MonitorApp:
    """发现本机活动 case，并为每个 case 保留独立速度历史。"""

    def __init__(
        self,
        case_dir: Optional[Path],
        scan_roots: list[Path],
        stale_seconds: int,
        refresh_seconds: int,
        wsl_distros: Optional[list[str]],
    ) -> None:
        self.default_case_dir = case_dir.expanduser().resolve() if case_dir else None
        self.scan_roots = [path.expanduser().resolve() for path in scan_roots]
        self.stale_seconds = max(30, int(stale_seconds))
        self.refresh_seconds = max(2, int(refresh_seconds))
        self.wsl_distros = None if wsl_distros is None else [str(name).strip() for name in wsl_distros if str(name).strip()]
        self.lock = threading.RLock()
        self.monitors: dict[str, SpinupMonitor] = {}
        if self.default_case_dir and self.default_case_dir.is_dir():
            self._monitor_for(self.default_case_dir)

    def _monitor_for(
        self,
        case_dir: Path | str,
        *,
        process_backend: str = "windows",
        wsl_distro: Optional[str] = None,
        pid: Optional[int] = None,
    ) -> SpinupMonitor:
        directory = Path(case_dir).expanduser().resolve()
        key = monitor_id(directory)
        monitor = self.monitors.get(key)
        if monitor is None:
            monitor = SpinupMonitor(directory, stale_seconds=self.stale_seconds, process_backend=process_backend,
                                    wsl_distro=wsl_distro, pid_override=pid)
            self.monitors[key] = monitor
        elif process_backend == "wsl2":
            monitor.process_backend = "wsl2"
            monitor.wsl_distro = wsl_distro
            monitor.pid_override = int(pid) if pid else monitor.pid_override
        return monitor

    def discover(self) -> list[dict[str, Any]]:
        """仅在用户打开选择器时调用。"""
        with self.lock:
            extras = [self.default_case_dir] if self.default_case_dir else []
            return discover_running_cases(self.scan_roots, extras) + discover_wsl_running_cases(self.wsl_distros)

    def select_directory(
        self,
        case_dir: Path | str,
        *,
        process_backend: str = "windows",
        wsl_distro: Optional[str] = None,
        pid: Optional[int] = None,
    ) -> SpinupMonitor:
        """验证用户选中的目录确实对应当前 PAWS.exe，再创建监控器。"""

        directory = Path(case_dir).expanduser().resolve()
        allowed = any(directory == root or root in directory.parents for root in self.scan_roots)
        allowed_wsl_distros = self.wsl_distros if self.wsl_distros is not None else list_running_wsl_distros()
        if process_backend == "wsl2" and wsl_distro in allowed_wsl_distros:
            text = str(directory).lower()
            allowed = text.startswith(f"\\\\wsl.localhost\\{wsl_distro.lower()}\\home\\") or bool(
                re.match(r"^[a-z]:\\", text)
            )
        if self.default_case_dir and directory == self.default_case_dir:
            allowed = True
        if not allowed:
            raise PermissionError("所选目录不在允许的 PAWS case 扫描根目录中")
        profile = case_profile(directory, process_backend=process_backend, wsl_distro=wsl_distro, pid_override=pid)
        proc = process_info(profile.get("pid"), backend=process_backend, wsl_distro=wsl_distro)
        expected = "paws_clm" if process_backend == "wsl2" else "paws.exe"
        actual = str(proc.get("name") or "").lower()
        if not proc.get("alive") or (expected not in actual if process_backend == "wsl2" else actual != expected):
            raise FileNotFoundError(f"所选 case 当前没有匹配且存活的 {expected}")
        return self._monitor_for(directory, process_backend=process_backend, wsl_distro=wsl_distro, pid=pid)

    def selected_monitor(self, requested: Optional[str], requested_dir: Optional[str], *, process_backend: str = "windows",
                         wsl_distro: Optional[str] = None, pid: Optional[int] = None) -> Optional[SpinupMonitor]:
        if requested and requested in self.monitors:
            return self.monitors[requested]
        if requested_dir:
            return self.select_directory(requested_dir, process_backend=process_backend, wsl_distro=wsl_distro, pid=pid)
        if self.default_case_dir:
            return self.select_directory(self.default_case_dir)
        return None

    def snapshot(self, requested: Optional[str] = None, requested_dir: Optional[str] = None, *,
                 process_backend: str = "windows", wsl_distro: Optional[str] = None, pid: Optional[int] = None) -> dict[str, Any]:
        with self.lock:
            monitor = self.selected_monitor(requested, requested_dir, process_backend=process_backend, wsl_distro=wsl_distro, pid=pid)
            if monitor is None:
                raise FileNotFoundError("尚未选择监控目标，请点击“选择运行中的 PAWS”")
            snapshot = monitor.snapshot()
            snapshot["selected_monitor_id"] = snapshot["monitor_id"]
            return snapshot

    def bootstrap(self) -> dict[str, Any]:
        return {
            "service": "paws-local-process-monitor",
            "read_only": True,
            "selection_mode": "manual",
            "refresh_seconds": self.refresh_seconds,
            "stale_seconds": self.stale_seconds,
            "scan_roots": [str(path) for path in self.scan_roots],
            "wsl_distros": list(self.wsl_distros) if self.wsl_distros is not None else list_running_wsl_distros(),
            "wsl_discovery": "explicit" if self.wsl_distros is not None else "auto-running",
            "features": ["windows", "wsl2"],
        }

    def case_choices(self) -> dict[str, Any]:
        rows = self.discover()
        with self.lock:
            for row in rows:
                if row.get("selectable") and row.get("case_dir"):
                    self._monitor_for(Path(row["case_dir"]), process_backend=row.get("process_backend", "windows"),
                                      wsl_distro=row.get("wsl_distro"), pid=row.get("pid"))
        return {
            "selection_mode": "manual",
            "read_only": True,
            "cases": rows,
        }


def make_handler(app: MonitorApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PAWS-Local-Monitor/2.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[{self.log_date_time_string()}] {fmt % args}")

        def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _static(self, path: str) -> None:
            if path in {"", "/"}:
                relative = "index.html"
            elif path.startswith("/static/"):
                relative = path[len("/static/") :]
            else:
                relative = path.lstrip("/")
            candidate = (STATIC / relative).resolve()
            if STATIC.resolve() not in candidate.parents and candidate != STATIC.resolve():
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if not candidate.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = candidate.read_bytes()
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json", "image/svg+xml"}:
                content_type += "; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            requested = query.get("case_id", [None])[0]
            requested_dir = query.get("case_dir", [None])[0]
            process_backend = query.get("process_backend", ["windows"])[0]
            wsl_distro = query.get("wsl_distro", [None])[0]
            try:
                pid = int(query.get("pid", ["0"])[0]) or None
            except ValueError:
                pid = None
            try:
                if parsed.path in {"/api/status", "/api/snapshot"}:
                    self._json(app.snapshot(requested, requested_dir, process_backend=process_backend,
                                            wsl_distro=wsl_distro, pid=pid))
                    return
                if parsed.path == "/api/bootstrap":
                    self._json(app.bootstrap())
                    return
                if parsed.path == "/api/cases":
                    self._json(app.case_choices())
                    return
                if parsed.path == "/api/health":
                    self._json({"ok": True, "service": "paws-local-process-monitor", "selection_mode": "manual",
                                "features": ["windows", "wsl2"]})
                    return
                self._static(parsed.path)
            except FileNotFoundError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except PermissionError as exc:
                self._json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
            except Exception as exc:
                self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    return Handler


def run_server(app: MonitorApp, host: str, port: int, open_browser: bool) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{host}:{port}/"
    print(f"PAWS 本地运行监控: {url}")
    print("由用户手动选择运行中的 PAWS；仅监听本机。关闭面板不会停止 PAWS。按 Ctrl+C 停止监控服务。")
    if open_browser and host in {"127.0.0.1", "localhost", "::1"}:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n监控服务已停止。")
    finally:
        server.server_close()


def run_watch(app: MonitorApp, requested: Optional[str], requested_dir: Optional[str]) -> int:
    try:
        while True:
            snapshot = app.snapshot(requested, requested_dir)
            print("\x1b[2J\x1b[H", end="")
            print(render_terminal(snapshot), flush=True)
            time.sleep(app.refresh_seconds)
    except KeyboardInterrupt:
        print("\n监控已停止；PAWS 进程未被操作。")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="手动选择并只读监控本地运行中的 PAWS")
    parser.add_argument("--case-dir", type=Path, help="终端/一次性模式指定 case；网页模式通常省略")
    parser.add_argument("--scan-root", type=Path, action="append", help="点击选择按钮时扫描的根目录，可重复")
    parser.add_argument("--case-id", help="终端/一次性模式选择 monitor_id")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="网页端口，默认 8877")
    parser.add_argument("--interval", type=int, default=5, help="页面/终端刷新间隔（秒）")
    parser.add_argument("--stale-minutes", type=float, default=30, help="多久没有进度更新才告警")
    parser.add_argument("--wsl-distro", action="append", help="启用监控的 WSL 发行版，可重复；默认 Ubuntu")
    parser.add_argument("--no-browser", action="store_true", help="启动网页服务但不自动打开浏览器")
    parser.add_argument("--watch", action="store_true", help="使用终端监控，不启动网页")
    parser.add_argument("--once", action="store_true", help="只输出一次 JSON 状态后退出")
    args = parser.parse_args()

    app = MonitorApp(
        args.case_dir,
        scan_roots=args.scan_root or [DEFAULT_SCAN_ROOT],
        stale_seconds=max(30, int(args.stale_minutes * 60)),
        refresh_seconds=max(2, args.interval),
        wsl_distros=args.wsl_distro,
    )
    if args.once:
        print(json.dumps(app.snapshot(args.case_id), ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    if args.watch:
        return run_watch(app, args.case_id, str(args.case_dir) if args.case_dir else None)
    run_server(app, args.host, args.port, not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
