from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from monitor import Account, MonitorService, ssh_base


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
PAWS_RECFILE_LENGTH = 32


@dataclass
class DownloadTask:
    id: str
    status: str
    account: str
    label: str
    created_at: str
    updated_at: str
    phase: str = "等待开始"
    progress_pct: Optional[float] = None
    archive_bytes: Optional[int] = None
    local_bytes: Optional[int] = None
    local_path: Optional[str] = None
    remote_archive: Optional[str] = None
    sha256: Optional[str] = None
    error: Optional[str] = None
    cases: List[str] = field(default_factory=list)


def _safe_leaf(value: str, fallback: str = "paws_results") -> str:
    cleaned = SAFE_NAME_RE.sub("_", value).strip("._")
    return cleaned[:100] or fallback


def _valid_relative_path(value: str) -> bool:
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts


def _file_role(path: str, case_id: str) -> str:
    name = PurePosixPath(path).name
    main_expected = f"{case_id}.txt"
    recorder_expected = f"{case_id}_Rec.txt"
    main_truncated = main_expected[:PAWS_RECFILE_LENGTH]
    recorder_truncated = recorder_expected[:PAWS_RECFILE_LENGTH]
    if main_truncated == recorder_truncated and name == main_truncated:
        return "collided_output"
    if name == main_expected or name == main_truncated:
        return "main_output"
    if name.startswith(f"{case_id}_Rec") or (recorder_truncated != main_truncated and name == recorder_truncated):
        return "recorder_output"
    if name == "changepar.dat":
        return "changepar"
    if name == "cRec.txt":
        return "channel_recorder"
    if name in {"paws_stdout.log", "paws_stderr.log", "run_metadata.log"}:
        return "provenance"
    if name.endswith(".in"):
        return "driver"
    if name == f"{case_id}_m.mat":
        return "model_mat"
    if name == f"{case_id}_m_CLM.mat":
        return "clm_mat"
    return "other"


def _infer_output_prefix(paths: List[str], case_id: str) -> str:
    """Find the prefix PAWS actually used when the driver ID differs from the case ID."""
    names = {PurePosixPath(path).name for path in paths}
    if f"{case_id}.txt" in names or f"{case_id}_Rec.txt" in names:
        return case_id
    candidates = []
    for name in names:
        if not name.endswith(".txt") or name.endswith("_Rec.txt"):
            continue
        prefix = name[:-4]
        if f"{prefix}_Rec.txt" in names:
            candidates.append(prefix)
    return sorted(candidates, key=lambda value: (len(value), value))[0] if candidates else case_id


def _standard_selected(path: str, case_id: str) -> bool:
    return _file_role(path, case_id) not in {"other", "collided_output"}


def _standard_audit(files: List[Dict[str, Any]], case_id: str, output_prefix: Optional[str] = None) -> Dict[str, Any]:
    output_prefix = output_prefix or _infer_output_prefix([entry.get("path", "") for entry in files], case_id)
    roles = {entry["role"] for entry in files}
    main_truncated = f"{output_prefix}.txt"[:PAWS_RECFILE_LENGTH]
    recorder_truncated = f"{output_prefix}_Rec.txt"[:PAWS_RECFILE_LENGTH]
    collision = main_truncated == recorder_truncated and len(f"{output_prefix}_Rec.txt") > PAWS_RECFILE_LENGTH
    missing = []
    if "main_output" not in roles:
        missing.append("main_output")
    if "recorder_output" not in roles:
        missing.append("recorder_output")
    messages = []
    if collision:
        messages.append(f"case名超过PAWS recfile的{PAWS_RECFILE_LENGTH}字符上限，主结果与Recorder目标路径发生截断碰撞；共享文件可能混写覆盖，不能作为任一标准结果")
    if "main_output" not in roles:
        messages.append("缺少主结果文件（<case>.txt或其PAWS截断变体）")
    if "recorder_output" not in roles:
        messages.append("缺少Recorder结果文件（<case>_Rec.txt或其截断变体）")
    return {
        "standard_complete": not missing,
        "missing_roles": missing,
        "filename_collision": collision,
        "messages": messages,
        "main_output_paths": [entry["path"] for entry in files if entry["role"] == "main_output"],
        "recorder_output_paths": [entry["path"] for entry in files if entry["role"] == "recorder_output"],
        "collided_output_paths": [entry["path"] for entry in files if entry["role"] == "collided_output"],
    }


def _archive_aliases(entry: Dict[str, Any], case_id: str) -> List[str]:
    role = entry["role"]
    parent = str(PurePosixPath(entry["path"]).parent)
    prefix = "" if parent == "." else f"{parent}/"
    if role == "main_output":
        return [f"{prefix}prj.txt", f"{prefix}{case_id}.txt"]
    if role == "recorder_output":
        return [f"{prefix}prj_Rec.txt", f"{prefix}{case_id}_Rec.txt"]
    if role == "changepar":
        return [f"{prefix}changepar{case_id}.dat"]
    return []


class DownloadManager:
    def __init__(self, monitor: MonitorService):
        self.monitor = monitor
        self.config = monitor.config
        self.tasks: Dict[str, DownloadTask] = {}
        self.lock = threading.Lock()

    def _account(self, username: str) -> Account:
        for account in self.monitor.accounts:
            if account.username == username:
                return account
        raise ValueError("账号不存在")

    def list_files(self, account_name: str, run_key: str) -> List[Dict[str, Any]]:
        run = self.monitor.get_run(account_name, run_key)
        if not run:
            raise ValueError("请先刷新，所选case不在当前快照中")
        if not run.get("download_ready"):
            raise PermissionError("该case尚未通过完成健康门，不能列出下载文件")
        run_dir = str(run["run_dir"])
        account = self._account(account_name)
        script = f"cd {shlex.quote(run_dir)} && find . -type f -printf '%P\\t%s\\n' | sort"
        completed = subprocess.run(
            ssh_base(account, self.config) + [script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(self.config["ssh_timeout_seconds"]) + 20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or "远端文件清单读取失败").strip()[-500:])
        raw_files = []
        for line in completed.stdout.splitlines():
            try:
                path, size = line.rsplit("\t", 1)
                if _valid_relative_path(path):
                    raw_files.append((path, int(size)))
            except (ValueError, TypeError):
                continue
        output_prefix = _infer_output_prefix([path for path, _ in raw_files], run["case_id"])
        files = []
        for path, size in raw_files:
            role = _file_role(path, output_prefix)
            files.append({"path": path, "size_bytes": size, "role": role, "standard": role not in {"other", "collided_output"}})
        return files

    def start(self, payload: Dict[str, Any]) -> DownloadTask:
        account_name = str(payload.get("account", ""))
        selections = payload.get("selections") or []
        scope = str(payload.get("scope", "standard"))
        if scope not in {"standard", "full", "custom"}:
            raise ValueError("下载范围无效")
        if not selections:
            raise ValueError("至少选择一个case")
        if len(selections) > 100:
            raise ValueError("单次最多选择100个case")
        resolved = []
        for item in selections:
            run_key = str(item.get("run_key", ""))
            run = self.monitor.get_run(account_name, run_key)
            if not run or not run.get("download_ready"):
                raise PermissionError(f"{run_key}尚未通过完成健康门")
            available = self.list_files(account_name, run_key)
            available_map = {entry["path"]: entry for entry in available}
            if scope == "standard":
                audit = _standard_audit(available, run["case_id"])
                if not audit["standard_complete"]:
                    raise ValueError(f"{run['case_id']}标准结果不完整：{'；'.join(audit['messages'])}")
                selected = [entry for entry in available if entry["standard"]]
            elif scope == "full":
                selected = list(available)
            else:
                requested = [str(x) for x in item.get("files", [])]
                selected = [available_map[path] for path in requested if path in available_map and _valid_relative_path(path)]
            if not selected:
                raise ValueError(f"{run['case_id']}没有可打包文件")
            resolved.append({"run": run, "files": sorted(selected, key=lambda entry: entry["path"]), "scope": scope, "audit": _standard_audit(available, run["case_id"])})

        label = _safe_leaf(str(payload.get("label") or f"paws_{resolved[0]['run']['job_id']}_selected"))
        task_id = uuid.uuid4().hex[:12]
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        task = DownloadTask(task_id, "queued", account_name, label, now, now, cases=[item["run"]["case_id"] for item in resolved])
        with self.lock:
            self.tasks[task_id] = task
        threading.Thread(target=self._execute, args=(task_id, resolved), daemon=True).start()
        return task

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            task = self.tasks.get(task_id)
            return asdict(task) if task else None

    def all(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [asdict(task) for task in sorted(self.tasks.values(), key=lambda x: x.created_at, reverse=True)]

    def _update(self, task_id: str, **values: Any) -> None:
        with self.lock:
            task = self.tasks[task_id]
            for key, value in values.items():
                setattr(task, key, value)
            task.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    def _execute(self, task_id: str, resolved: List[Dict[str, Any]]) -> None:
        task = self.tasks[task_id]
        account = self._account(task.account)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        remote_leaf = _safe_leaf(f"{task.label}_{stamp}_{task.id}")
        remote_base = f"$HOME/.paws_dashboard_downloads/{remote_leaf}"
        remote_archive = f"$HOME/.paws_dashboard_downloads/{remote_leaf}.tar.gz"
        local_parent = Path(self.config["download_root"]).resolve()
        local_dir = (local_parent / f"{task.label}_{stamp}").resolve()
        if local_dir.parent != local_parent:
            self._update(task_id, status="failed", error="本地下载路径安全检查失败")
            return
        local_dir.mkdir(parents=True, exist_ok=False)
        archive_path = local_dir / f"{remote_leaf}.tar.gz"
        checksum_path = local_dir / f"{remote_leaf}.tar.gz.sha256"
        manifest_rows: List[Dict[str, Any]] = []
        try:
            self._update(task_id, status="running", phase="远端建立只读文件清单并打包", progress_pct=8.0, local_path=str(local_dir))
            manifest_rows = []
            for item in resolved:
                run = item["run"]
                for entry in item["files"]:
                    rel = entry["path"]
                    manifest_rows.append({"account": task.account, "job_id": run["job_id"], "task_id": run["task_id"], "case_id": run["case_id"], "remote_run": run["run_dir"], "remote_file": rel, "archive_file": rel, "role": entry["role"], "alias_of": ""})
                    for alias in _archive_aliases(entry, run["case_id"]):
                        if alias != rel:
                            manifest_rows.append({"account": task.account, "job_id": run["job_id"], "task_id": run["task_id"], "case_id": run["case_id"], "remote_run": run["run_dir"], "remote_file": rel, "archive_file": alias, "role": entry["role"], "alias_of": rel})
            # Use literal absolute remote home paths to avoid shell-variable quoting ambiguity.
            home = f"/public/home/{task.account}"
            stage_abs = f"{home}/.paws_dashboard_downloads/{remote_leaf}"
            archive_abs = f"{home}/.paws_dashboard_downloads/{remote_leaf}.tar.gz"
            lines = ["set -euo pipefail", "umask 077", f"mkdir -p {shlex.quote(home + '/.paws_dashboard_downloads')}", f"rm -rf {shlex.quote(stage_abs)}", f"mkdir -p {shlex.quote(stage_abs)}"]
            for item in resolved:
                run = item["run"]
                case_leaf = _safe_leaf(run["case_id"])
                for entry in item["files"]:
                    rel = entry["path"]
                    source = f"{run['run_dir']}/{rel}"
                    destination = f"{stage_abs}/{case_leaf}/{rel}"
                    lines.append(f"mkdir -p {shlex.quote(str(PurePosixPath(destination).parent))}")
                    lines.append(f"ln -s {shlex.quote(source)} {shlex.quote(destination)}")
                    for alias in _archive_aliases(entry, run["case_id"]):
                        if alias == rel:
                            continue
                        alias_destination = f"{stage_abs}/{case_leaf}/{alias}"
                        lines.append(f"mkdir -p {shlex.quote(str(PurePosixPath(alias_destination).parent))}")
                        lines.append(f"ln -s {shlex.quote(source)} {shlex.quote(alias_destination)}")
            lines += [
                f"tar -czhf {shlex.quote(archive_abs)} -C {shlex.quote(stage_abs)} .",
                f"sha256sum {shlex.quote(archive_abs)} > {shlex.quote(archive_abs + '.sha256')}",
                f"size=$(stat -c %s {shlex.quote(archive_abs)})",
                f"sha=$(cut -d' ' -f1 {shlex.quote(archive_abs + '.sha256')})",
                f"rm -rf {shlex.quote(stage_abs)}",
                f"printf '__PACKAGE__\\t%s\\t%s\\t%s\\n' {shlex.quote(archive_abs)} \"$size\" \"$sha\"",
            ]
            completed = subprocess.run(
                ssh_base(account, self.config) + ["bash -s"],
                input=("\n".join(lines) + "\n").encode("utf-8"),
                capture_output=True,
                timeout=3600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
                raise RuntimeError(detail[-1000:])
            remote_output = completed.stdout.decode("utf-8", errors="replace")
            package_line = next((line for line in remote_output.splitlines() if line.startswith("__PACKAGE__\t")), None)
            if not package_line:
                raise RuntimeError("远端打包未返回校验信息")
            _, archive_abs, size_text, expected_sha = package_line.split("\t", 3)
            archive_bytes = int(size_text)
            self._update(task_id, phase="下载归档文件", progress_pct=35.0, archive_bytes=archive_bytes, remote_archive=archive_abs)
            scp_base = ["scp", "-P", str(self.config["port"]), "-i", account.key_path, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new"]
            process = subprocess.Popen(
                scp_base + [f"{account.username}@{self.config['host']}:{archive_abs}", str(archive_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            while process.poll() is None:
                current = archive_path.stat().st_size if archive_path.exists() else 0
                pct = 35.0 + min(55.0, 55.0 * current / archive_bytes) if archive_bytes else 35.0
                self._update(task_id, progress_pct=round(pct, 1), local_bytes=current)
                time.sleep(1)
            stdout, stderr = process.communicate()
            if process.returncode != 0:
                raise RuntimeError((stderr or stdout or b"scp failed").decode("utf-8", errors="replace")[-1000:])
            self._update(task_id, phase="验证SHA-256并写入manifest", progress_pct=92.0, local_bytes=archive_path.stat().st_size)
            digest = hashlib.sha256()
            with archive_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
            actual_sha = digest.hexdigest()
            if actual_sha.lower() != expected_sha.lower():
                raise RuntimeError(f"SHA-256不一致：远端{expected_sha}，本地{actual_sha}")
            checksum_path.write_text(f"{actual_sha}  {archive_path.name}\n", encoding="ascii")
            manifest_json = {
                "created_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
                "account": task.account,
                "download_scope": resolved[0]["scope"],
                "case_count": len(resolved),
                "archive": str(archive_path),
                "archive_size_bytes": archive_path.stat().st_size,
                "sha256": actual_sha,
                "remote_archive": archive_abs,
                "standard_audits": [{"case_id": item["run"]["case_id"], **item["audit"]} for item in resolved],
                "files": manifest_rows,
            }
            (local_dir / "download_summary.json").write_text(json.dumps(manifest_json, ensure_ascii=False, indent=2), encoding="utf-8")
            with (local_dir / "download_manifest_selected.csv").open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=["account", "job_id", "task_id", "case_id", "remote_run", "remote_file", "archive_file", "role", "alias_of"])
                writer.writeheader()
                writer.writerows(manifest_rows)
            self._update(task_id, status="completed", phase="下载完成并通过SHA-256校验", progress_pct=100.0, sha256=actual_sha)
        except Exception as exc:  # background task boundary
            self._update(task_id, status="failed", phase="下载失败", error=str(exc))
