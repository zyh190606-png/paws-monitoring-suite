from __future__ import annotations

import hashlib
import csv
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

from monitor import ACCOUNT_RE, Account, discover_accounts


PRIVATE_KEY_MARKERS = (
    "OPENSSH PRIVATE KEY",
    "RSA PRIVATE KEY",
    "EC PRIVATE KEY",
    "DSA PRIVATE KEY",
)


def _load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class AccountManager:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.state_root = Path(config["state_root"])
        self.registry_path = self.state_root / "account_registry.json"
        self._lock = threading.RLock()

    def _registry(self) -> Dict[str, Any]:
        value = _load_json(self.registry_path, {})
        value.setdefault("disabled_accounts", [])
        value.setdefault("managed_accounts", {})
        return value

    def discover(self) -> List[Account]:
        with self._lock:
            disabled = set(self._registry()["disabled_accounts"])
            return [account for account in discover_accounts(self.config) if account.username not in disabled]

    def describe(self) -> List[Dict[str, Any]]:
        with self._lock:
            registry = self._registry()
            managed = registry["managed_accounts"]
            return [
                {
                    "username": account.username,
                    "source": account.source,
                    "managed": account.username in managed,
                    "remove_mode": "delete_managed_copy" if account.username in managed else "hide_only",
                }
                for account in self.discover()
            ]

    def add(self, username: str, key_content: str, original_name: str = "") -> Dict[str, Any]:
        username = username.strip()
        if not ACCOUNT_RE.fullmatch(username):
            raise ValueError("账号名格式无效")
        if not isinstance(key_content, str) or not 64 <= len(key_content.encode("utf-8")) <= 1_000_000:
            raise ValueError("私钥文件大小无效")
        normalized = key_content.replace("\r\n", "\n").strip() + "\n"
        if "\x00" in normalized or not any(f"BEGIN {marker}" in normalized and f"END {marker}" in normalized for marker in PRIVATE_KEY_MARKERS):
            raise ValueError("文件不是可识别的 OpenSSH/PEM 私钥")

        with self._lock:
            registry = self._registry()
            active = {account.username for account in discover_accounts(self.config)}
            if username in active and username not in set(registry["disabled_accounts"]):
                raise ValueError("该账号已在面板中")

            account_root = Path(self.config["accounts_root"]) / username
            account_root.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            key_path = account_root / f"{username}_manual_{timestamp}.key"
            temporary = key_path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(normalized)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, key_path)

            registry["disabled_accounts"] = [name for name in registry["disabled_accounts"] if name != username]
            registry["managed_accounts"][username] = {
                "key_path": str(key_path),
                "original_name": Path(original_name).name,
                "added_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            _write_json(self.registry_path, registry)
            return {"username": username, "source": "HPC_accounts", "managed": True}

    def remove(self, username: str) -> Dict[str, Any]:
        username = username.strip()
        if not ACCOUNT_RE.fullmatch(username):
            raise ValueError("账号名格式无效")
        with self._lock:
            registry = self._registry()
            active = {account.username for account in self.discover()}
            if username not in active:
                raise ValueError("账号不在当前面板中")

            managed = registry["managed_accounts"].pop(username, None)
            deleted_copy = False
            if managed:
                key_path = Path(managed.get("key_path", ""))
                accounts_root = Path(self.config["accounts_root"]).resolve()
                try:
                    if key_path.is_file() and accounts_root in key_path.resolve().parents:
                        key_path.unlink()
                        deleted_copy = True
                        if key_path.parent != accounts_root and not any(key_path.parent.iterdir()):
                            key_path.parent.rmdir()
                except OSError:
                    deleted_copy = False

            disabled = set(registry["disabled_accounts"])
            disabled.add(username)
            registry["disabled_accounts"] = sorted(disabled)
            _write_json(self.registry_path, registry)
            return {"username": username, "deleted_managed_copy": deleted_copy, "source_credentials_preserved": not managed}


class AlertReadStore:
    def __init__(self, config: Dict[str, Any]):
        self.path = Path(config["state_root"]) / "read_alerts.json"
        self._lock = threading.RLock()

    @staticmethod
    def alert_id(alert: Dict[str, Any]) -> str:
        fields = ("account", "job_id", "task_id", "case_id", "code")
        identity = "\x1f".join(str(alert.get(field, "")) for field in fields)
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]

    def decorate(self, alerts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        with self._lock:
            state = _load_json(self.path, {"read_ids": []})
            read_ids = set(state.get("read_ids", []))
            decorated = []
            active_ids = set()
            for source in alerts:
                alert = dict(source)
                alert["alert_id"] = self.alert_id(alert)
                active_ids.add(alert["alert_id"])
                alert["read"] = alert["alert_id"] in read_ids
                decorated.append(alert)
            remaining = sorted(read_ids & active_ids)
            if remaining != sorted(read_ids):
                _write_json(self.path, {"read_ids": remaining})
            return decorated

    def mark_read(self, alert_ids: Iterable[str]) -> None:
        clean_ids = {str(value) for value in alert_ids if isinstance(value, str) and len(value) == 20}
        if not clean_ids:
            raise ValueError("没有可标记的告警")
        with self._lock:
            state = _load_json(self.path, {"read_ids": []})
            state["read_ids"] = sorted(set(state.get("read_ids", [])) | clean_ids)
            _write_json(self.path, state)


class AccountCapacityStore:
    def __init__(self, config: Dict[str, Any]):
        self.path = Path(config["account_ledger_path"])
        self.limit = int(config.get("account_case_limit", 40))
        self.pool = set(config.get("account_pool_accounts", []))

    def describe(self, usernames: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        names = set(usernames)
        base = {
            name: {
                "capacity_status": "read_only" if name not in self.pool else "unknown",
                "capacity_limit": self.limit if name in self.pool else None,
                "assigned_case_count": None,
                "capacity_remaining": None,
                "ledger_updated_at": None,
            }
            for name in names
        }
        if not self.path.is_file():
            return base

        assigned = {name: set() for name in self.pool}
        try:
            with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                required = {"account", "formal_case_id"}
                if not required.issubset(reader.fieldnames or []):
                    return base
                for row in reader:
                    account = str(row.get("account", "")).strip()
                    formal_case_id = str(row.get("formal_case_id", "")).strip()
                    if account in assigned and formal_case_id:
                        assigned[account].add(formal_case_id)
        except OSError:
            return base

        updated_at = datetime.fromtimestamp(self.path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        for name in names & self.pool:
            count = len(assigned[name])
            base[name] = {
                "capacity_status": "known",
                "capacity_limit": self.limit,
                "assigned_case_count": count,
                "capacity_remaining": max(0, self.limit - count),
                "ledger_updated_at": updated_at,
            }
        return base
