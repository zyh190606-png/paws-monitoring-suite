from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from download import DownloadManager, _standard_audit
from monitor import MonitorService, load_config
from state import AccountCapacityStore, AccountManager, AlertReadStore


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
CONFIG_PATH = ROOT / "config.json"


class DashboardApp:
    def __init__(self, config_path: Path = CONFIG_PATH):
        self.config = load_config(config_path)
        self.accounts = AccountManager(self.config)
        self.capacity = AccountCapacityStore(self.config)
        self.alerts = AlertReadStore(self.config)
        self.monitor = MonitorService(self.config, account_provider=self.accounts.discover)
        self.downloads = DownloadManager(self.monitor)
        self.token = secrets.token_urlsafe(24)

    def snapshot(self) -> Dict[str, Any]:
        snapshot = copy.deepcopy(self.monitor.last_snapshot)
        telemetry = snapshot.get("accounts", [])
        capacities = self.capacity.describe(account["username"] for account in telemetry)
        snapshot["accounts"] = [{**account, **capacities.get(account["username"], {})} for account in telemetry]
        snapshot["managed_accounts"] = self.account_descriptions()
        decorated = self.alerts.decorate(snapshot.get("alerts", []))
        snapshot["alerts"] = [alert for alert in decorated if not alert["read"]]
        summary = snapshot.setdefault("summary", {})
        summary["critical_count"] = sum(1 for alert in snapshot["alerts"] if alert["level"] == "critical")
        summary["warning_count"] = sum(1 for alert in snapshot["alerts"] if alert["level"] == "warning")
        return snapshot

    def account_descriptions(self) -> list[Dict[str, Any]]:
        accounts = self.accounts.describe()
        capacities = self.capacity.describe(account["username"] for account in accounts)
        return [{**account, **capacities.get(account["username"], {})} for account in accounts]


def make_handler(app: DashboardApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PAWS-HPC-Dashboard/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[{self.log_date_time_string()}] {fmt % args}")

        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, message: str, status: int = 400) -> None:
            self._json({"error": message}, status)

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 2_000_000:
                raise ValueError("请求过大")
            raw = self.rfile.read(length) if length else b"{}"
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("请求必须是JSON对象")
            return value

        def _authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-PAWS-Token", ""), app.token)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/bootstrap":
                self._json({
                    "token": app.token,
                    "account_count": len(app.monitor.accounts),
                    "accounts": app.account_descriptions(),
                    "recent_days": app.config["recent_days"],
                    "download_root": app.config["download_root"],
                    "completion_policy": "完成前只读；完成健康门全部通过后开放打包下载",
                })
                return
            if parsed.path == "/api/snapshot":
                self._json(app.snapshot())
                return
            if parsed.path == "/api/accounts":
                self._json({"accounts": app.account_descriptions()})
                return
            if parsed.path == "/api/files":
                query = parse_qs(parsed.query)
                try:
                    files = app.downloads.list_files(query.get("account", [""])[0], query.get("run_key", [""])[0])
                    run = app.monitor.get_run(query.get("account", [""])[0], query.get("run_key", [""])[0])
                    self._json({"files": files, "audit": _standard_audit(files, run["case_id"]) if run else None})
                except PermissionError as exc:
                    self._error(str(exc), HTTPStatus.FORBIDDEN)
                except Exception as exc:
                    self._error(str(exc))
                return
            if parsed.path == "/api/downloads":
                self._json({"tasks": app.downloads.all()})
                return
            if parsed.path.startswith("/api/downloads/"):
                task = app.downloads.get(parsed.path.rsplit("/", 1)[-1])
                self._json(task or {"error": "下载任务不存在"}, 200 if task else 404)
                return
            self._static(parsed.path)

        def do_POST(self) -> None:
            if not self._authorized():
                self._error("本地会话令牌无效，请刷新页面", HTTPStatus.FORBIDDEN)
                return
            try:
                payload = self._body()
                if self.path == "/api/refresh":
                    app.monitor.reload_accounts()
                    app.monitor.refresh(payload.get("recent_days"), payload.get("manual_jobs"), payload.get("accounts"))
                    self._json(app.snapshot())
                elif self.path == "/api/accounts":
                    result = app.accounts.add(payload.get("username", ""), payload.get("key_content", ""), payload.get("filename", ""))
                    app.monitor.reload_accounts()
                    self._json({"account": result, "accounts": app.account_descriptions()}, HTTPStatus.CREATED)
                elif self.path == "/api/alerts/read":
                    app.alerts.mark_read(payload.get("alert_ids", []))
                    self._json(app.snapshot())
                elif self.path == "/api/downloads":
                    if payload.get("confirm") is not True:
                        raise ValueError("下载请求缺少二次确认")
                    task = app.downloads.start(payload)
                    self._json(task.__dict__, HTTPStatus.ACCEPTED)
                else:
                    self._error("接口不存在", HTTPStatus.NOT_FOUND)
            except PermissionError as exc:
                self._error(str(exc), HTTPStatus.FORBIDDEN)
            except Exception as exc:
                self._error(str(exc))

        def do_DELETE(self) -> None:
            if not self._authorized():
                self._error("本地会话令牌无效，请刷新页面", HTTPStatus.FORBIDDEN)
                return
            try:
                payload = self._body()
                if self.path != "/api/accounts":
                    self._error("接口不存在", HTTPStatus.NOT_FOUND)
                    return
                result = app.accounts.remove(payload.get("username", ""))
                app.monitor.reload_accounts()
                self._json({"removed": result, "accounts": app.account_descriptions()})
            except Exception as exc:
                self._error(str(exc))

        def _static(self, path: str) -> None:
            relative = "index.html" if path in {"", "/"} else path.lstrip("/")
            candidate = (STATIC / relative).resolve()
            if STATIC.resolve() not in candidate.parents and candidate != STATIC.resolve():
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if not candidate.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = candidate.read_bytes()
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="PAWS HPC本地监控与安全下载面板")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    app = DashboardApp(args.config)
    host = app.config.get("listen_host", "127.0.0.1")
    port = args.port or int(app.config.get("listen_port", 8876))
    server = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{host}:{port}/"
    print(f"PAWS HPC Dashboard: {url}")
    print("仅监听本机；按 Ctrl+C 停止。")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n面板已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
