from __future__ import annotations

import concurrent.futures
import json
import math
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


ACCOUNT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
RUN_DIR_RE = re.compile(r"^(?P<job>\d+)_(?P<task>\d+)_(?P<case>.+)$")
JOB_TASK_RE = re.compile(r"^(?P<job>\d+)(?:_(?P<task>\d+))?$")


@dataclass(frozen=True)
class Account:
    username: str
    key_path: str
    source: str


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def discover_accounts(config: Dict[str, Any]) -> List[Account]:
    found: Dict[str, Account] = {}
    for item in config.get("accounts", []):
        username = str(item.get("username", "")).strip()
        key_path = Path(os.path.expandvars(str(item.get("key_path", "")))).expanduser()
        if ACCOUNT_RE.fullmatch(username) and key_path.is_file():
            found[username] = Account(username, str(key_path), "config")

    root = Path(config["accounts_root"]).expanduser()
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or not ACCOUNT_RE.fullmatch(folder.name):
                continue
            keys = sorted(path for path in folder.glob(f"{folder.name}_*") if path.is_file())
            if keys:
                found[folder.name] = Account(folder.name, str(keys[-1]), "HPC_accounts")

    legacy_root = Path(config["legacy_keys_root"]).expanduser()
    if legacy_root.is_dir():
        for key in sorted(legacy_root.glob("paws_hpc_*_key")):
            username = key.name[len("paws_hpc_") : -len("_key")]
            if ACCOUNT_RE.fullmatch(username) and username not in found:
                found[username] = Account(username, str(key), ".ssh")
    return [found[name] for name in sorted(found)]


def ssh_base(account: Account, config: Dict[str, Any]) -> List[str]:
    return [
        "ssh",
        "-p",
        str(config["port"]),
        "-i",
        account.key_path,
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={int(config['ssh_timeout_seconds'])}",
        f"{account.username}@{config['host']}",
    ]


def _remote_snapshot_script(recent_days: int, manual_jobs: Iterable[str]) -> str:
    jobs = ",".join(j for j in manual_jobs if re.fullmatch(r"\d+(?:_\d+)?", j))
    scan_days = max(3, recent_days + 3)
    return r'''set +e
export LC_ALL=C
printf '__NOW__\t%s\t%s\n' "$(date +%s)" "$(date '+%F %T %Z')"
squeue -u "$USER" -h -o '%i|%A|%a|%P|%j|%T|%M|%L|%D|%R|%S' 2>/dev/null | sed 's/^/__SQUEUE__\t/'
start_date=$(date -d '__RECENT__ days ago' +%F 2>/dev/null)
[ -n "$start_date" ] || start_date=$(date +%F)
sacct -u "$USER" -S "$start_date" -X --array -n -P --format=JobIDRaw,JobName,Partition,State,ExitCode,Elapsed,Start,End,NodeList,Timelimit 2>/dev/null | sed '/^[[:space:]]*$/d; s/^/__SACCT__\t/'
if [ -n '__JOBS__' ]; then
  sacct -j '__JOBS__' -X --array -n -P --format=JobIDRaw,JobName,Partition,State,ExitCode,Elapsed,Start,End,NodeList,Timelimit 2>/dev/null | sed '/^[[:space:]]*$/d; s/^/__SACCT__\t/'
fi
root="$HOME/__REMOTE_ROOT__"
if [ -d "$root" ]; then
  find "$root" -maxdepth 5 -type f -name run_metadata.log -mtime -__SCAN_DAYS__ -print0 2>/dev/null |
  while IFS= read -r -d '' meta; do
    run_dir=$(dirname "$meta")
    work="$run_dir/work"
    base=$(basename "$run_dir")
    batch_root=$(dirname "$(dirname "$run_dir")")
    batch=$(basename "$batch_root")
    job=$(printf '%s' "$base" | sed -nE 's/^([0-9]+)_([0-9]+)_.*/\1/p')
    task=$(printf '%s' "$base" | sed -nE 's/^([0-9]+)_([0-9]+)_.*/\2/p')
    case_id=$(sed -nE 's/^(CASE_ID|CASE|RUN_CASE_ID)=//p' "$meta" | head -n 1)
    [ -n "$case_id" ] || case_id=$(printf '%s' "$base" | sed -E 's/^[0-9]+_[0-9]+_//')
    rc=$(sed -n 's/^PAWS_RC=//p' "$meta" | tail -n 1)
    start_epoch=$(sed -n 's/^START_EPOCH=//p' "$meta" | head -n 1)
    end_epoch=$(sed -n 's/^END_EPOCH=//p' "$meta" | tail -n 1)
    stderr_bytes=0
    [ -f "$work/paws_stderr.log" ] && stderr_bytes=$(wc -c < "$work/paws_stderr.log")
    stdout_bytes=0
    stdout_mtime=0
    first=''
    latest=''
    if [ -f "$work/paws_stdout.log" ]; then
      stdout_bytes=$(wc -c < "$work/paws_stdout.log")
      stdout_mtime=$(stat -c %Y "$work/paws_stdout.log" 2>/dev/null)
      first=$(awk '$1 ~ /^[0-9][0-9][0-9][0-9]$/ && $2 ~ /^[0-9][0-9]?[0-9]?$/ {print $1 " " $2; exit}' "$work/paws_stdout.log")
      latest=$(tail -n 1000 "$work/paws_stdout.log" | awk '$1 ~ /^[0-9][0-9][0-9][0-9]$/ && $2 ~ /^[0-9][0-9]?[0-9]?$/ {line=$1 " " $2} END {print line}')
    fi
    first_year=$(printf '%s' "$first" | awk '{print $1}')
    first_day=$(printf '%s' "$first" | awk '{print $2}')
    latest_year=$(printf '%s' "$latest" | awk '{print $1}')
    latest_day=$(printf '%s' "$latest" | awk '{print $2}')
    core_count=$(find "$work" -maxdepth 1 -type f \( -name '*.in' -o -name '*.txt' -o -name '*.mat' -o -name '*_Rec*' \) 2>/dev/null | wc -l)
    safe_case=$(printf '%s' "$case_id" | tr '\t\r\n' '   ')
    printf '__RUN__\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$job" "$task" "$safe_case" "$batch" "$batch_root" "$run_dir" "$rc" "$start_epoch" "$end_epoch" "$stderr_bytes" "$stdout_bytes" "$stdout_mtime" "$first_year" "$first_day" "$latest_year" "$latest_day" "$core_count"
  done
fi
'''.replace("__RECENT__", str(recent_days)).replace("__SCAN_DAYS__", str(scan_days)).replace("__JOBS__", jobs)


def _safe_int(value: str) -> Optional[int]:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def parse_slurm_duration(value: Optional[str]) -> Optional[int]:
    if not value or value in {"N/A", "UNLIMITED", "INVALID", "Unknown"}:
        return None
    value = value.strip()
    days = 0
    if "-" in value:
        day_text, value = value.split("-", 1)
        try:
            days = int(day_text)
        except ValueError:
            return None
    parts = value.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = map(int, parts)
        elif len(parts) == 2:
            hours, minutes = map(int, parts)
            seconds = 0
        else:
            return None
    except ValueError:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def doy_date(year: Optional[int], day: Optional[int]) -> Optional[datetime]:
    if year is None or day is None or day < 1 or day > 366:
        return None
    try:
        return datetime(year, 1, 1) + timedelta(days=day - 1)
    except ValueError:
        return None


def _iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def load_local_timings(config: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, str]]:
    root = Path(config["local_packages_root"])
    timings: Dict[Tuple[str, str], Dict[str, str]] = {}
    if not root.is_dir():
        return timings
    for batch_dir in root.iterdir():
        if not batch_dir.is_dir():
            continue
        for cfg_path in sorted(batch_dir.glob("runner_config*.json")):
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for case in data.get("cases", []):
                case_id = str(case.get("id", ""))
                timing = case.get("time_overrides") or {}
                if case_id and timing.get("model_start") and timing.get("model_end"):
                    timings[(batch_dir.name, case_id)] = {
                        "model_start": str(timing["model_start"]),
                        "model_end": str(timing["model_end"]),
                        "source": str(cfg_path),
                    }
            break
    return timings


def _parse_scheduler_line(line: str, marker: str) -> Optional[Dict[str, Any]]:
    payload = line.split("\t", 1)[1] if "\t" in line else ""
    fields = payload.split("|")
    if marker == "__SQUEUE__" and len(fields) >= 11:
        job_raw, array_job, array_task, partition, name, state, elapsed, time_left, nodes, reason, start = fields[:11]
        return {
            "job_raw": job_raw,
            "array_job": array_job,
            "array_task": array_task,
            "partition": partition,
            "job_name": name,
            "state": state,
            "exit_code": None,
            "elapsed": elapsed,
            "time_left": time_left,
            "nodes": nodes,
            "node_or_reason": reason,
            "start": start,
            "end": None,
            "timelimit": None,
            "source": "squeue",
        }
    if marker == "__SACCT__" and len(fields) >= 10:
        job_raw, name, partition, state, exit_code, elapsed, start, end, node, timelimit = fields[:10]
        return {
            "job_raw": job_raw,
            "array_job": None,
            "array_task": None,
            "partition": partition,
            "job_name": name,
            "state": state.split()[0],
            "exit_code": exit_code,
            "elapsed": elapsed,
            "time_left": None,
            "nodes": None,
            "node_or_reason": node,
            "start": start,
            "end": end,
            "timelimit": timelimit,
            "source": "sacct",
        }
    return None


def _job_key(job_raw: str) -> Optional[str]:
    base = job_raw.split(".", 1)[0]
    match = JOB_TASK_RE.fullmatch(base)
    if not match:
        return None
    if match.group("task"):
        return f"{match.group('job')}_{match.group('task')}"
    return match.group("job")


def parse_snapshot_output(output: str) -> Dict[str, Any]:
    now_epoch = int(time.time())
    now_text = ""
    scheduler: Dict[str, Dict[str, Any]] = {}
    runs: List[Dict[str, Any]] = []
    for raw_line in output.splitlines():
        if raw_line.startswith("__NOW__\t"):
            parts = raw_line.split("\t", 2)
            now_epoch = _safe_int(parts[1]) or now_epoch
            now_text = parts[2] if len(parts) > 2 else ""
        elif raw_line.startswith("__SQUEUE__\t") or raw_line.startswith("__SACCT__\t"):
            marker = raw_line.split("\t", 1)[0]
            row = _parse_scheduler_line(raw_line, marker)
            if not row:
                continue
            key = _job_key(row["job_raw"])
            if not key:
                continue
            previous = scheduler.get(key)
            if previous is None or row["source"] == "squeue":
                scheduler[key] = row
        elif raw_line.startswith("__RUN__\t"):
            fields = raw_line.split("\t")[1:]
            if len(fields) < 17:
                continue
            job, task, case_id, batch, batch_root, run_dir, rc, start_epoch, end_epoch, stderr_bytes, stdout_bytes, stdout_mtime, first_year, first_day, latest_year, latest_day, core_count = fields[:17]
            if not (job.isdigit() and task.isdigit()):
                continue
            runs.append(
                {
                    "job_id": job,
                    "task_id": task,
                    "run_key": f"{job}_{task}",
                    "case_id": case_id,
                    "batch": batch,
                    "batch_root": batch_root,
                    "run_dir": run_dir,
                    "paws_rc": _safe_int(rc),
                    "start_epoch": _safe_int(start_epoch),
                    "end_epoch": _safe_int(end_epoch),
                    "stderr_bytes": _safe_int(stderr_bytes) or 0,
                    "stdout_bytes": _safe_int(stdout_bytes) or 0,
                    "stdout_mtime": _safe_int(stdout_mtime) or 0,
                    "first_year": _safe_int(first_year),
                    "first_day": _safe_int(first_day),
                    "latest_year": _safe_int(latest_year),
                    "latest_day": _safe_int(latest_day),
                    "core_file_count": _safe_int(core_count) or 0,
                }
            )
    return {"now_epoch": now_epoch, "now_text": now_text, "scheduler": scheduler, "runs": runs}


def _calculate_progress(run: Dict[str, Any], timing: Optional[Dict[str, str]]) -> Dict[str, Any]:
    latest = doy_date(run.get("latest_year"), run.get("latest_day"))
    if timing:
        model_start = _iso_datetime(timing.get("model_start"))
        model_end = _iso_datetime(timing.get("model_end"))
    else:
        model_start = doy_date(run.get("first_year"), run.get("first_day"))
        model_end = None
    result: Dict[str, Any] = {
        "latest_date": latest.strftime("%Y-%m-%d") if latest else None,
        "latest_label": f"{run.get('latest_year')}年第{run.get('latest_day')}天" if latest else "尚无日期输出",
        "model_start": model_start.strftime("%Y-%m-%d") if model_start else None,
        "model_end": model_end.strftime("%Y-%m-%d") if model_end else None,
        "timing_source": timing.get("source") if timing else None,
        "progress_pct": None,
        "eta_seconds": None,
    }
    if latest and model_start and model_end and model_end > model_start:
        fraction = max(0.0, min(1.0, (latest - model_start).total_seconds() / (model_end - model_start).total_seconds()))
        result["progress_pct"] = round(fraction * 100, 1)
        elapsed = parse_slurm_duration(run.get("elapsed"))
        if elapsed and fraction > 0.01 and fraction < 1.0:
            result["eta_seconds"] = int(elapsed * (1.0 - fraction) / fraction)
    return result


def _enrich_run(run: Dict[str, Any], scheduler: Optional[Dict[str, Any]], timing: Optional[Dict[str, str]], now_epoch: int, stale_minutes: int) -> Dict[str, Any]:
    merged = dict(run)
    if scheduler:
        merged.update({k: v for k, v in scheduler.items() if k not in {"job_raw"}})
    else:
        merged.update({"state": "COMPLETED" if run.get("paws_rc") == 0 else "UNKNOWN", "exit_code": None, "elapsed": None, "time_left": None, "partition": None, "job_name": None, "node_or_reason": None, "start": None, "end": None, "timelimit": None})
    merged.update(_calculate_progress(merged, timing))
    merged["stdout_age_seconds"] = max(0, now_epoch - merged["stdout_mtime"]) if merged["stdout_mtime"] else None
    merged["elapsed_seconds"] = parse_slurm_duration(merged.get("elapsed"))
    merged["time_left_seconds"] = parse_slurm_duration(merged.get("time_left"))
    eta_seconds = merged.get("eta_seconds")
    merged["expected_end_at"] = (
        datetime.fromtimestamp(now_epoch + eta_seconds).astimezone().isoformat(timespec="minutes")
        if eta_seconds and str(merged.get("state") or "").upper() == "RUNNING"
        else None
    )
    warnings: List[Dict[str, str]] = []
    state = str(merged.get("state") or "UNKNOWN").upper()
    if merged["stderr_bytes"] > 0:
        warnings.append({"level": "critical", "code": "STDERR_NONEMPTY", "message": f"stderr非空（{merged['stderr_bytes']} B）"})
    if state in {"FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL"}:
        warnings.append({"level": "critical", "code": "SLURM_FAILURE", "message": f"Slurm状态为{state}"})
    if state == "RUNNING" and not merged.get("latest_date"):
        warnings.append({"level": "warning", "code": "NO_PROGRESS", "message": "运行中但尚无模型日期输出"})
    if state == "RUNNING" and merged.get("stdout_age_seconds") is not None and merged["stdout_age_seconds"] > stale_minutes * 60:
        warnings.append({"level": "critical", "code": "STDOUT_STALE", "message": f"stdout已{merged['stdout_age_seconds'] // 60}分钟未更新"})
    if state == "RUNNING" and merged.get("eta_seconds") and merged.get("time_left_seconds") and merged["eta_seconds"] > merged["time_left_seconds"]:
        warnings.append({"level": "critical", "code": "WALLTIME_RISK", "message": "预计剩余时间超过Slurm墙钟余量"})
    elif state == "RUNNING" and merged.get("eta_seconds") and merged.get("time_left_seconds") and merged["time_left_seconds"] - merged["eta_seconds"] < 4 * 3600:
        warnings.append({"level": "warning", "code": "WALLTIME_MARGIN_LOW", "message": "预计完成距墙钟上限不足4小时"})
    if state == "COMPLETED" and merged.get("paws_rc") not in {0}:
        warnings.append({"level": "critical", "code": "PAWS_RC_NOT_ZERO", "message": "Slurm完成但PAWS_RC不是0或缺失"})
    if state == "COMPLETED" and timing is None:
        warnings.append({"level": "info", "code": "TIMING_INFERRED", "message": "缺少本地runner_config时间合同；末年覆盖按PAWS正常结束与最终日期推断"})

    end_date = _iso_datetime(timing.get("model_end")) if timing else None
    latest_date = doy_date(merged.get("latest_year"), merged.get("latest_day"))
    coverage_ok = bool(
        (end_date and latest_date and latest_date.date() >= end_date.date())
        or (not end_date and latest_date and merged.get("paws_rc") == 0 and state == "COMPLETED")
    )
    exit_ok = merged.get("exit_code") in {"0:0", None} and (scheduler is not None or merged.get("paws_rc") == 0)
    merged["health_warnings"] = warnings
    merged["health_level"] = "critical" if any(w["level"] == "critical" for w in warnings) else "warning" if any(w["level"] == "warning" for w in warnings) else "ok"
    merged["download_ready"] = bool(state == "COMPLETED" and exit_ok and merged.get("paws_rc") == 0 and merged["stderr_bytes"] == 0 and coverage_ok and merged["core_file_count"] >= 3)
    merged["completion_gate"] = {
        "slurm_completed": state == "COMPLETED",
        "exit_zero": exit_ok,
        "paws_rc_zero": merged.get("paws_rc") == 0,
        "stderr_empty": merged["stderr_bytes"] == 0,
        "model_end_covered": coverage_ok,
        "core_outputs_present": merged["core_file_count"] >= 3,
        "coverage_evidence": "runner_config" if end_date else "normal_completion_and_final_stdout_date" if coverage_ok else "missing",
    }
    return merged


class MonitorService:
    def __init__(self, config: Dict[str, Any], account_provider: Optional[Callable[[], List[Account]]] = None):
        self.config = config
        self._account_provider = account_provider or (lambda: discover_accounts(config))
        self.accounts = self._account_provider()
        self.timings = load_local_timings(config)
        self.last_snapshot: Dict[str, Any] = {"accounts": [], "runs": [], "jobs": [], "alerts": []}
        self.run_index: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def reload_accounts(self) -> List[Account]:
        self.accounts = self._account_provider()
        return self.accounts

    def refresh(self, recent_days: Optional[int] = None, manual_jobs: Optional[List[str]] = None, account_names: Optional[List[str]] = None) -> Dict[str, Any]:
        days = max(1, min(30, int(recent_days or self.config["recent_days"])))
        jobs = [j for j in (manual_jobs or []) if re.fullmatch(r"\d+(?:_\d+)?", str(j))]
        requested_accounts = {str(name) for name in account_names} if account_names is not None else None
        target_accounts = [account for account in self.accounts if requested_accounts is None or account.username in requested_accounts]
        if not target_accounts:
            raise ValueError("没有可刷新的有效账号")
        partial_refresh = len(target_accounts) < len(self.accounts)
        script = _remote_snapshot_script(days, jobs).replace("__REMOTE_ROOT__", str(self.config["remote_cases_root"]))
        max_workers = min(len(target_accounts), int(self.config.get("max_parallel_accounts", 8)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._refresh_account, account, script): account for account in target_accounts}
            account_results = [future.result() for future in concurrent.futures.as_completed(futures)]

        result_by_account = {result["account"]["username"]: result for result in account_results}
        previous_accounts = {account["username"]: account for account in self.last_snapshot.get("accounts", [])}
        previous_runs = list(self.last_snapshot.get("runs", []))
        account_summaries: List[Dict[str, Any]] = []
        for account in self.accounts:
            result = result_by_account.get(account.username)
            if result is not None:
                summary = dict(result["account"])
                summary["using_cached_data"] = bool(not summary["reachable"] and any(run["account"] == account.username for run in previous_runs))
                account_summaries.append(summary)
            elif account.username in previous_accounts:
                summary = dict(previous_accounts[account.username])
                summary["not_refreshed"] = True
                account_summaries.append(summary)
            else:
                account_summaries.append({**asdict(account), "reachable": False, "latency_ms": None, "error_code": "NOT_REFRESHED", "error": "尚未查询", "not_refreshed": True})

        refreshed_names = {account.username for account in target_accounts}
        failed_names = {name for name, result in result_by_account.items() if not result["account"]["reachable"]}
        if partial_refresh:
            all_runs = [dict(run, data_stale=(run["account"] in failed_names)) for run in previous_runs if run["account"] not in refreshed_names or run["account"] in failed_names]
        else:
            all_runs = []
        scheduler_rows: List[Dict[str, Any]] = []
        for result in sorted(account_results, key=lambda item: item["account"]["username"]):
            scheduler_rows.extend(result.get("scheduler_rows", []))
            all_runs.extend(dict(run, data_stale=False) for run in result.get("runs", []))

        known_keys = {(r["account"], r["run_key"]) for r in all_runs}
        for row in scheduler_rows:
            key = (row["account"], row["run_key"])
            if key in known_keys or "_" not in row["run_key"]:
                continue
            placeholder = {
                "account": row["account"], "job_id": row["run_key"].split("_", 1)[0], "task_id": row["run_key"].split("_", 1)[1], "run_key": row["run_key"],
                "case_id": "等待运行目录", "batch": "未识别", "batch_root": None, "run_dir": None, "paws_rc": None, "stderr_bytes": 0, "stdout_bytes": 0,
                "stdout_mtime": 0, "latest_year": None, "latest_day": None, "core_file_count": 0,
            }
            all_runs.append(_enrich_run(placeholder, row, None, int(time.time()), int(self.config["stale_stdout_minutes"])))

        all_runs.sort(key=lambda r: (r["account"], -int(r["job_id"]), int(r["task_id"])))
        self.run_index = {(r["account"], r["run_key"]): r for r in all_runs}
        jobs_summary = self._group_jobs(all_runs)
        alerts = [dict(w, account=r["account"], job_id=r["job_id"], task_id=r["task_id"], case_id=r["case_id"]) for r in all_runs for w in r["health_warnings"] if w["level"] in {"warning", "critical"}]
        snapshot = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "recent_days": days,
            "refresh_scope": "case_accounts" if partial_refresh else "all_accounts",
            "refreshed_accounts": sorted(refreshed_names),
            "accounts": account_summaries,
            "jobs": jobs_summary,
            "runs": all_runs,
            "alerts": alerts,
            "summary": {
                "account_count": len(self.accounts),
                "reachable_accounts": sum(1 for a in account_summaries if a["reachable"]),
                "job_count": len(jobs_summary),
                "case_count": len(all_runs),
                "running_count": sum(1 for r in all_runs if r["state"] == "RUNNING"),
                "completed_count": sum(1 for r in all_runs if r["state"] == "COMPLETED"),
                "download_ready_count": sum(1 for r in all_runs if r["download_ready"]),
                "critical_count": sum(1 for a in alerts if a["level"] == "critical"),
            },
        }
        self.last_snapshot = snapshot
        return snapshot

    def _refresh_account(self, account: Account, script: str) -> Dict[str, Any]:
        started = time.monotonic()
        completed = None
        attempts = int(self.config.get("query_retry_count", 1)) + 1
        timeout = int(self.config.get("remote_query_timeout_seconds", int(self.config["ssh_timeout_seconds"]) + 30))
        for attempt in range(1, attempts + 1):
            try:
                completed = subprocess.run(
                    ssh_base(account, self.config) + [script],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                if attempt < attempts:
                    time.sleep(0.6 * attempt)
                    continue
                return {"account": {**asdict(account), "reachable": False, "latency_ms": int((time.monotonic() - started) * 1000), "attempts": attempt, "error_code": "QUERY_TIMEOUT", "error": f"远端状态查询超过{timeout}秒；账号认证未必异常"}, "runs": [], "scheduler_rows": []}
            except OSError as exc:
                return {"account": {**asdict(account), "reachable": False, "latency_ms": int((time.monotonic() - started) * 1000), "attempts": attempt, "error_code": "LOCAL_SSH_ERROR", "error": str(exc)[:300]}, "runs": [], "scheduler_rows": []}
            if completed.returncode == 0 or attempt >= attempts:
                break
            time.sleep(0.6 * attempt)

        assert completed is not None
        latency = int((time.monotonic() - started) * 1000)
        if completed.returncode != 0:
            error = (completed.stderr or completed.stdout).strip()[-500:]
            lowered = error.lower()
            error_code = "AUTH_FAILED" if "permission denied" in lowered else "SSH_CONNECTION_FAILED"
            return {"account": {**asdict(account), "reachable": False, "latency_ms": latency, "attempts": attempts, "error_code": error_code, "error": error or f"SSH exit {completed.returncode}"}, "runs": [], "scheduler_rows": []}
        parsed = parse_snapshot_output(completed.stdout)
        enriched: List[Dict[str, Any]] = []
        for run in parsed["runs"]:
            run["account"] = account.username
            scheduler = parsed["scheduler"].get(run["run_key"])
            timing = self.timings.get((run["batch"], run["case_id"]))
            enriched.append(_enrich_run(run, scheduler, timing, parsed["now_epoch"], int(self.config["stale_stdout_minutes"])))
        sched_rows = []
        for key, row in parsed["scheduler"].items():
            item = dict(row)
            item["account"] = account.username
            item["run_key"] = key
            sched_rows.append(item)
        return {"account": {**asdict(account), "reachable": True, "latency_ms": latency, "attempts": attempt, "error_code": None, "error": None, "remote_time": parsed["now_text"]}, "runs": enriched, "scheduler_rows": sched_rows}

    @staticmethod
    def _group_jobs(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for run in runs:
            groups.setdefault((run["account"], run["job_id"]), []).append(run)
        result = []
        for (account, job_id), cases in groups.items():
            states: Dict[str, int] = {}
            for case in cases:
                states[case["state"]] = states.get(case["state"], 0) + 1
            progress_values = [c["progress_pct"] for c in cases if c.get("progress_pct") is not None]
            result.append({
                "account": account,
                "job_id": job_id,
                "job_name": next((c.get("job_name") for c in cases if c.get("job_name")), None),
                "batch": next((c.get("batch") for c in cases if c.get("batch") != "未识别"), "未识别"),
                "case_count": len(cases),
                "states": states,
                "mean_progress_pct": round(sum(progress_values) / len(progress_values), 1) if progress_values else None,
                "critical_count": sum(1 for c in cases if c["health_level"] == "critical"),
                "warning_count": sum(1 for c in cases if c["health_level"] == "warning"),
                "download_ready_count": sum(1 for c in cases if c["download_ready"]),
            })
        return sorted(result, key=lambda j: (j["account"], -int(j["job_id"])))

    def get_run(self, account: str, run_key: str) -> Optional[Dict[str, Any]]:
        return self.run_index.get((account, run_key))
