"""只读 PAWS 连续 spin-up 监控核心。

这个模块不向模型进程发送任何信号，也不打开或写入模型输出文件。它只读取：

* ``continuous_spinup_manifest.json`` / ``continuous_spinup_status.json`` /
  ``continuous_spinup_result.json``
* ``paws_pid.txt``
* 预算与诊断文本的尾部

因此可以在 PAWS 正常运行时安全地反复调用 ``snapshot``。
"""

from __future__ import annotations

import ctypes
import csv
import base64
import hashlib
import json
import math
import os
import re
import subprocess
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Iterable, Optional


DEFAULT_CASE_DIR = Path(os.environ.get("PAWS_LOCAL_CASE", Path.home() / "paws-cases" / "demo"))
DEFAULT_SCAN_ROOT = Path(os.environ.get("PAWS_LOCAL_SCAN_ROOT", Path.home() / "paws-cases"))
DEFAULT_STALE_SECONDS = 30 * 60
# WSL /proc hides exe and cwd symlinks across Linux users. Root is used only
# for read-only process discovery and status sampling.
WSL_QUERY_USER = "root"
DEFAULT_WSL_DISTROS = ("Ubuntu",)
WSL_DISCOVERY_SCRIPT = r'''for p in /proc/[0-9]*; do
  pid=${p##*/}
  [ -r "$p/cmdline" ] || continue
  args=$(tr '\0' ' ' < "$p/cmdline")
  exe=$(readlink -f "$p/exe" 2>/dev/null) || continue
  case "${exe##*/}:$args" in PAWS_CLM*.in*) ;; *) continue ;; esac
  cwd=$(readlink -f "$p/cwd" 2>/dev/null) || continue
  comm=$(cat "$p/comm" 2>/dev/null || true)
  etimes=$(ps -p "$pid" -o etimes= 2>/dev/null | tr -d ' ')
  cput=$(ps -p "$pid" -o time= 2>/dev/null | tr -d ' ')
  rss=$(ps -p "$pid" -o rss= 2>/dev/null | tr -d ' ')
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$pid" "$comm" "$etimes" "$cput" "$rss" "$cwd" "$args"
done'''
# PAWS 的预算通常按小时写出；窗口太短会在正常运行时一直显示“无最近速度”。
WINDOW_SECONDS = 2 * 60 * 60

DATE_ROW_RE = re.compile(r"^\s*(?P<date>\d{8})\s+(?P<clock>\d{1,8})(?:\s|$)")
YEAR_DOY_RE = re.compile(r"^\s*(?P<year>\d{4})\s+(?P<doy>\d{1,3})(?:\s|$)")
MODEL_START_RE = re.compile(r"Model Start Time:\s*(\d{4})\s+(\d{1,2})\s+(\d{1,2})", re.IGNORECASE)
MODEL_END_RE = re.compile(r"Model End Time:\s*(\d{4})\s+(\d{1,2})\s+(\d{1,2})", re.IGNORECASE)
PID_FILE_NAMES = ("PID.txt", "paws_pid.txt", "warm_restart_pid.txt", "resource_smoke_pid.txt")


def _local_tz() -> timezone:
    return datetime.now().astimezone().tzinfo or timezone.utc


def aware(value: datetime) -> datetime:
    """把无时区时间按本机时区解释，避免减法混用 naive/aware。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=_local_tz())
    return value.astimezone(_local_tz())


def parse_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return aware(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return aware(datetime.strptime(text, fmt))
        except ValueError:
            continue
    return None


def json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def key_value_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip():
            values[key.strip().lower()] = value.strip()
    return values


def monitor_id(case_dir: Path | str) -> str:
    normalized = str(Path(case_dir).expanduser().resolve()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def wsl_unc_path(distro: str, linux_path: str) -> Path:
    """把 WSL Linux 绝对路径映射为 Windows 可只读访问的 UNC 路径。"""

    clean_distro = str(distro).strip()
    clean_path = str(linux_path).strip()
    if not clean_distro or not clean_path.startswith("/"):
        raise ValueError("WSL 路径需要发行版名称和 Linux 绝对路径")
    # Windows-mounted paths are already directly accessible via their drive
    # letter. Mapping them through \\wsl.localhost can yield WinError 5.
    parts = clean_path.split("/", 3)
    if len(parts) >= 3 and parts[1].lower() == "mnt" and len(parts[2]) == 1 and parts[2].isalpha():
        tail = parts[3] if len(parts) == 4 else ""
        windows_tail = tail.replace("/", "\\")
        return Path(f"{parts[2].upper()}:\\{windows_tail}")
    windows_tail = clean_path.lstrip("/").replace("/", "\\")
    return Path(f"\\\\wsl.localhost\\{clean_distro}\\{windows_tail}")


def parse_wsl_process_rows(text: str, distro: str) -> list[dict[str, Any]]:
    """解析 WSL 发现命令的制表符输出，忽略并发退出造成的残行。"""

    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.rstrip("\r\n").split("\t", 6)
        if len(parts) < 7:
            continue
        pid_text, comm, elapsed_text, cpu_time, rss_text, cwd, args = parts
        try:
            pid = int(pid_text)
            elapsed = float(elapsed_text)
            rss = int(rss_text) * 1024
        except ValueError:
            continue
        if pid <= 0 or not cwd.startswith("/") or "PAWS_CLM" not in args:
            continue
        rows.append({
            "pid": pid,
            "name": comm.strip() or "PAWS_CLM",
            "elapsed_seconds": max(0.0, elapsed),
            "cpu_time": cpu_time.strip(),
            "working_set_bytes": max(0, rss),
            "cwd_linux": cwd,
            "args": args.strip(),
            "distro": distro,
        })
    return rows


def _run_wsl(
    distro: str,
    script: str,
    timeout: int = 15,
    user: Optional[str] = WSL_QUERY_USER,
) -> subprocess.CompletedProcess[bytes]:
    flags = 0x08000000 if os.name == "nt" else 0
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    command = ["wsl.exe", "-d", distro]
    if user:
        command.extend(["-u", user])
    command.extend(["--", "bash", "-lc", f"printf %s {encoded} | base64 -d | bash"])
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
        creationflags=flags,
    )


def _decode_wsl_output(data: bytes) -> str:
    if not data:
        return ""
    if b"\x00" in data[:128]:
        return data.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    return data.decode("utf-8", errors="replace")


def parse_wsl_distros(text: str) -> list[str]:
    """解析 ``wsl --list --running --quiet``，并按显示顺序去重。"""
    names: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        name = line.replace("\x00", "").strip().lstrip("\ufeff")
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def list_running_wsl_distros(timeout: int = 15) -> list[str]:
    """只列出已经运行的发行版，避免为监控而启动停止中的 WSL。"""
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            ["wsl.exe", "--list", "--running", "--quiet"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return parse_wsl_distros(_decode_wsl_output(completed.stdout))


def _head_text(path: Path, max_bytes: int = 262_144) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _best_json(case_dir: Path, kind: str) -> tuple[Optional[Path], dict[str, Any]]:
    """从不同版本控制器的文件名中挑出最像 manifest/status/result 的 JSON。"""

    if kind == "manifest":
        exact = ("continuous_spinup_manifest.json",)
        patterns = ("*manifest.json", "*manifest*.json")
        keys = {"model_start", "model_end", "runtime_case_id", "formal_case_id"}
    elif kind == "status":
        exact = ("continuous_spinup_status.json",)
        patterns = ("*status.json",)
        keys = {"paws_pid", "started_at", "status", "runtime_case_id"}
    else:
        exact = ("continuous_spinup_result.json",)
        patterns = ("*result.json",)
        keys = {"return_code", "completed_at", "status", "success_marker"}

    candidates: list[Path] = []
    for name in exact:
        path = case_dir / name
        if path.is_file():
            candidates.append(path)
    for pattern in patterns:
        candidates.extend(path for path in case_dir.glob(pattern) if path.is_file())

    unique: dict[str, Path] = {str(path.resolve()).lower(): path for path in candidates}
    scored: list[tuple[int, float, Path, dict[str, Any]]] = []
    for path in unique.values():
        data = json_object(path)
        score = sum(1 for key in keys if data.get(key) is not None)
        if score:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            scored.append((score, mtime, path, data))
    if not scored:
        return None, {}
    _, _, path, data = max(scored, key=lambda row: (row[0], row[1]))
    return path, data


def _runtime_case_id(case_dir: Path, manifest: dict[str, Any], status: dict[str, Any]) -> str:
    for source in (manifest, status):
        value = str(source.get("runtime_case_id") or "").strip()
        if value:
            return value
    # Copied inputs often retain template drivers; prefer the runtime directory
    # and canonical driver over alphabetical template names.
    runtime_name = case_dir.parent.name if case_dir.name == "work" else case_dir.name
    drivers = [case_dir / f"{runtime_name}.in", case_dir / "muskegon.in"]
    drivers.extend(sorted(case_dir.glob("*.in")))
    for path in drivers:
        try:
            value = path.read_text(encoding="ascii", errors="ignore").splitlines()[0].strip()
        except (OSError, IndexError):
            continue
        if value:
            return value
    stdout = sorted(case_dir.glob("*.stdout.txt"), key=lambda path: path.stat().st_mtime, reverse=True)
    if stdout:
        return stdout[0].name[: -len(".stdout.txt")]
    return case_dir.name


def _run_file(case_dir: Path, name: str) -> Path:
    path = case_dir / name
    if not path.is_file() and case_dir.name == "work":
        parent_path = case_dir.parent / name
        if parent_path.is_file():
            return parent_path
    return path


def _stdout_path(case_dir: Path, runtime_case_id: str) -> Path:
    preferred = _run_file(case_dir, f"{runtime_case_id}.stdout.txt")
    if preferred.is_file():
        return preferred
    legacy = _run_file(case_dir, "paws_stdout.log")
    if legacy.is_file():
        return legacy
    candidates = sorted(case_dir.glob("*.stdout.txt"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else preferred


def _bounds_from_stdout(path: Path) -> tuple[Optional[datetime], Optional[datetime]]:
    text = _head_text(path)
    start_match = MODEL_START_RE.search(text)
    end_match = MODEL_END_RE.search(text)
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    if start_match:
        try:
            start = aware(datetime(*map(int, start_match.groups())))
        except ValueError:
            start = None
    if end_match:
        try:
            end = aware(datetime(*map(int, end_match.groups()), 23, 0, 0))
        except ValueError:
            end = None
    return start, end


def case_profile(
    case_dir: Path | str,
    *,
    process_backend: str = "windows",
    wsl_distro: Optional[str] = None,
    pid_override: Optional[int] = None,
) -> dict[str, Any]:
    directory = Path(case_dir).expanduser().resolve()
    manifest_path, manifest = _best_json(directory, "manifest")
    status_path, status = _best_json(directory, "status")
    result_path, result = _best_json(directory, "result")
    runtime_case_id = _runtime_case_id(directory, manifest, status)
    stdout_path = _stdout_path(directory, runtime_case_id)
    run_meta = key_value_file(_run_file(directory, "run_metadata.log"))
    if not run_meta.get("start"):
        try:
            run_meta["start"] = _run_file(directory, "started.txt").read_text(encoding="utf-8").strip()
        except OSError:
            pass
    if not status and run_meta.get("start"):
        status = {"started_at": run_meta["start"], "status": "running", "runtime_case_id": runtime_case_id}
    if not result:
        rc_path = _run_file(directory, "PAWS_RC.txt")
        try:
            return_code = int(rc_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return_code = None
        if return_code is not None:
            try:
                completed_at = datetime.fromtimestamp(rc_path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
            except OSError:
                completed_at = None
            result_path = rc_path
            result = {
                "return_code": return_code,
                "status": "completed" if return_code == 0 else "failed",
                "completed_at": run_meta.get("end") or completed_at,
                "success_marker": "Simulation Successfully Completed" in _tail_text(stdout_path),
            }

    pid: Optional[int] = None
    pid_path: Optional[Path] = None
    for name in PID_FILE_NAMES:
        candidate = _run_file(directory, name)
        value = read_pid(candidate)
        if value is not None:
            pid, pid_path = value, candidate
            break
    if pid is None:
        try:
            value = int(status.get("paws_pid"))
            if value > 0:
                pid = value
        except (TypeError, ValueError):
            pass
    # A fresh run PID file supersedes the PID cached by an old browser selection.
    if pid_override is not None and int(pid_override) > 0 and not (process_backend == "wsl2" and pid_path):
        pid = int(pid_override)

    model_start = parse_datetime(manifest.get("model_start"))
    model_end = parse_datetime(manifest.get("model_end"))
    if model_start is None or model_end is None:
        stdout_start, stdout_end = _bounds_from_stdout(stdout_path)
        model_start = model_start or stdout_start
        model_end = model_end or stdout_end

    return {
        "case_dir": directory,
        "monitor_id": monitor_id(directory),
        "manifest_path": manifest_path,
        "manifest": manifest,
        "status_path": status_path,
        "status": status,
        "result_path": result_path,
        "result": result,
        "runtime_case_id": runtime_case_id,
        "formal_case_id": manifest.get("formal_case_id") or status.get("formal_case_id"),
        "stdout_path": stdout_path,
        "pid": pid,
        "pid_path": pid_path,
        "model_start": model_start,
        "model_end": model_end,
        "process_backend": process_backend,
        "wsl_distro": wsl_distro,
    }


def _tail_text(path: Path, max_bytes: int = 131_072) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def tail_lines(path: Path, max_lines: int = 8, max_bytes: int = 131_072) -> list[str]:
    text = _tail_text(path, max_bytes)
    if not text:
        return []
    return [line.rstrip() for line in text.splitlines() if line.strip()][-max_lines:]


def _clock_to_time(clock: str) -> Optional[tuple[int, int, int]]:
    try:
        number = int(clock)
    except (TypeError, ValueError):
        return None
    # PAWS writes 0, 10000, 20000, ... for 00:00, 01:00, 02:00, ...;
    # retain minute/second handling for files produced by other drivers.
    hour, remainder = divmod(number, 10000)
    minute, second = divmod(remainder, 100)
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None
    return hour, minute, second


def parse_progress_line(line: str, source: str = "") -> Optional[dict[str, Any]]:
    """解析预算/诊断的 ``YYYYMMDD clock``，或 stdout 的 ``YYYY DOY``。"""

    match = DATE_ROW_RE.match(line)
    if match:
        try:
            day = datetime.strptime(match.group("date"), "%Y%m%d")
        except ValueError:
            return None
        clock = _clock_to_time(match.group("clock"))
        if clock is None:
            return None
        value = day.replace(hour=clock[0], minute=clock[1], second=clock[2])
        return {
            "datetime": value,
            "date": value.strftime("%Y-%m-%d"),
            "clock": value.strftime("%H:%M:%S"),
            "source": source,
        }

    match = YEAR_DOY_RE.match(line)
    if match:
        try:
            year = int(match.group("year"))
            doy = int(match.group("doy"))
            leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
            max_doy = 366 if leap else 365
            if not 1 <= doy <= max_doy:
                return None
            value = datetime(year, 1, 1) + timedelta(days=doy - 1)
        except (ValueError, OverflowError):
            return None
        return {
            "datetime": value,
            "date": value.strftime("%Y-%m-%d"),
            "clock": "00:00:00",
            "source": source,
        }
    return None


def latest_progress(path: Path) -> Optional[dict[str, Any]]:
    """从文件尾部找最后一条完整日期行，不把并发写入的半行当成进度。"""

    text = _tail_text(path)
    if not text:
        return None
    lines = text.splitlines()
    # PAWS may be writing the final row while we read.  A line without a
    # terminating newline is treated as incomplete and skipped once; the
    # previous complete row is safer than displaying a future timestamp.
    if lines and text and not text.endswith(("\n", "\r")):
        lines = lines[:-1]
    for line in reversed(lines):
        parsed = parse_progress_line(line, path.name)
        if parsed is not None:
            parsed["line_preview"] = line.strip()[:180]
            try:
                parsed["file_mtime"] = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
            except OSError:
                parsed["file_mtime"] = None
            return parsed
    return None


def read_pid(path: Path) -> Optional[int]:
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def _native_process_info(pid: int) -> dict[str, Any]:
    """Windows 上用 kernel32/psapi 读取指定 PID；失败时仍尽力判断 alive。"""

    result: dict[str, Any] = {
        "pid": pid,
        "alive": False,
        "name": None,
        "path": None,
        "started_at": None,
        "cpu_seconds": None,
        "working_set_bytes": None,
        "private_bytes": None,
        "error": None,
    }
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            result["alive"] = True
        except OSError as exc:
            result["error"] = str(exc)
        return result

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
        False,
        pid,
    )
    if not handle:
        result["error"] = f"OpenProcess error {ctypes.get_last_error()}"
        return result

    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    class MemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32),
            ("page_fault_count", ctypes.c_uint32),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
            ("private_usage", ctypes.c_size_t),
        ]

    try:
        exit_code = ctypes.c_uint32()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            result["alive"] = int(exit_code.value) == STILL_ACTIVE

        size = ctypes.c_uint32(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            result["path"] = buffer.value
            result["name"] = Path(buffer.value).name

        try:
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            counters = MemoryCountersEx()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), ctypes.sizeof(counters)):
                result["working_set_bytes"] = int(counters.working_set_size)
                result["private_bytes"] = int(counters.private_usage)
        except (AttributeError, OSError):
            pass

        try:
            create = FileTime()
            exit_time = FileTime()
            kernel_time = FileTime()
            user_time = FileTime()
            if kernel32.GetProcessTimes(
                handle,
                ctypes.byref(create),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                create_ticks = (create.high << 32) | create.low
                kernel_ticks = (kernel_time.high << 32) | kernel_time.low
                user_ticks = (user_time.high << 32) | user_time.low
                unix_seconds = (create_ticks - 116_444_736_000_000_000) / 10_000_000.0
                result["started_at"] = datetime.fromtimestamp(unix_seconds, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
                result["cpu_seconds"] = (kernel_ticks + user_ticks) / 10_000_000.0
        except (AttributeError, OSError):
            pass
    finally:
        kernel32.CloseHandle(handle)
    return result


def _wsl_process_info(pid: int, distro: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "pid": pid, "alive": False, "name": None, "path": None,
        "started_at": None, "cpu_seconds": None, "working_set_bytes": None,
        "private_bytes": None, "error": None, "platform": "wsl2", "distro": distro,
    }
    script = (
        f"if [ -r /proc/{pid}/cmdline ]; then "
        f"comm=$(cat /proc/{pid}/comm); rss=$(ps -p {pid} -o rss= | tr -d ' '); "
        f"etimes=$(ps -p {pid} -o etimes= | tr -d ' '); "
        f"cput=$(ps -p {pid} -o time= | tr -d ' '); "
        f"args=$(tr '\\0' ' ' < /proc/{pid}/cmdline); "
        f"printf '%s\\t%s\\t%s\\t%s\\t%s\\n' \"$comm\" \"$rss\" \"$etimes\" \"$cput\" \"$args\"; fi"
    )
    try:
        completed = _run_wsl(distro, script)
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
        return result
    line = _decode_wsl_output(completed.stdout).strip()
    if completed.returncode != 0 or not line:
        result["error"] = _decode_wsl_output(completed.stderr).strip() or "WSL PID 不存在"
        return result
    parts = line.split("\t", 4)
    if len(parts) != 5:
        result["error"] = "无法解析 WSL 进程信息"
        return result
    comm, rss, elapsed, cpu_time, args = parts
    try:
        elapsed_seconds = float(elapsed)
        result["working_set_bytes"] = int(rss) * 1024
    except ValueError:
        elapsed_seconds = 0.0
    result.update({
        "alive": "PAWS_CLM" in args,
        "name": comm.strip(),
        "path": args.split()[0] if args.split() else None,
        "started_at": (datetime.now().astimezone() - timedelta(seconds=elapsed_seconds)).isoformat(timespec="seconds"),
        "cpu_time": cpu_time,
    })
    return result


def process_info(pid: Optional[int], *, backend: str = "windows", wsl_distro: Optional[str] = None) -> dict[str, Any]:
    if pid is None:
        return {
            "pid": None,
            "alive": False,
            "name": None,
            "path": None,
            "started_at": None,
            "cpu_seconds": None,
            "working_set_bytes": None,
            "private_bytes": None,
            "error": "未找到 PID 文件",
        }
    if backend == "wsl2":
        if not wsl_distro:
            return {"pid": pid, "alive": False, "name": None, "path": None, "started_at": None,
                    "cpu_seconds": None, "working_set_bytes": None, "private_bytes": None,
                    "error": "缺少 WSL 发行版名称", "platform": "wsl2"}
        return _wsl_process_info(pid, wsl_distro)
    result = _native_process_info(pid)
    result["platform"] = "windows"
    return result


def discover_wsl_running_cases(distros: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    """只读发现所有目标发行版中命令行带 `.in` 的 PAWS_CLM 进程及其 cwd。

    ``distros=None`` 表示自动读取当前正在运行的 WSL 发行版；传入列表
    时只扫描该列表，便于诊断或限制扫描范围。
    """

    rows: list[dict[str, Any]] = []
    names = list_running_wsl_distros() if distros is None else list(distros)
    seen: set[str] = set()
    for distro in names:
        name = str(distro).strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        try:
            completed = _run_wsl(name, WSL_DISCOVERY_SCRIPT)
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0:
            continue
        for item in parse_wsl_process_rows(_decode_wsl_output(completed.stdout), name):
            directory = wsl_unc_path(name, item["cwd_linux"])
            if not directory.is_dir():
                continue
            profile = case_profile(directory, process_backend="wsl2", wsl_distro=name, pid_override=item["pid"])
            latest = None
            for path in (directory / f"{profile['runtime_case_id']}.txt", directory / "station_struct_diag.txt", profile["stdout_path"]):
                parsed = latest_progress(path)
                if parsed and (latest is None or parsed["datetime"] > latest["datetime"]):
                    latest = parsed
            rows.append({
                "monitor_id": profile["monitor_id"], "case_dir": str(directory),
                "runtime_case_id": profile["runtime_case_id"], "formal_case_id": profile["formal_case_id"],
                "pid": item["pid"], "process_alive": True, "process_path": item["args"].split()[0],
                "current": _iso(aware(latest["datetime"])) if latest else None,
                "selectable": True, "mapped": True,
                "label": f"WSL/{name} · {profile['runtime_case_id']} · PID {item['pid']}",
                "process_backend": "wsl2", "wsl_distro": name, "linux_case_dir": item["cwd_linux"],
            })
    return rows


def paws_process_ids() -> list[int]:
    """列出 PAWS.exe PID；只用于发现，不用于判断某个 case 的归属。"""

    if os.name != "nt":
        return []
    completed = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq PAWS.exe", "/FO", "CSV", "/NH"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        creationflags=0x08000000,
    )
    text = completed.stdout.decode(errors="replace")
    found: list[int] = []
    for row in csv.reader(text.splitlines()):
        if len(row) < 2 or row[0].strip().lower() != "paws.exe":
            continue
        try:
            found.append(int(row[1]))
        except ValueError:
            continue
    return sorted(set(found))


def discover_running_cases(
    roots: Iterable[Path | str] = (DEFAULT_SCAN_ROOT,),
    extra_case_dirs: Iterable[Path | str] = (),
) -> list[dict[str, Any]]:
    """从 PID 文件把正在运行的 PAWS.exe 映射回 case 目录。

    PID 必须同时满足“文件中记录”和“当前进程名确为 PAWS.exe”；这样可避免
    Windows 重用旧 PID 后把 wininit 等系统进程误显示为 PAWS。
    """

    candidate_dirs: set[Path] = set()
    for value in extra_case_dirs:
        path = Path(value).expanduser()
        if path.is_dir():
            candidate_dirs.add(path.resolve())
    for value in roots:
        root = Path(value).expanduser()
        if not root.is_dir():
            continue
        for name in PID_FILE_NAMES:
            try:
                candidate_dirs.update(path.parent.resolve() for path in root.rglob(name))
            except OSError:
                continue

    rows: list[dict[str, Any]] = []
    mapped_pids: set[int] = set()
    for directory in sorted(candidate_dirs, key=lambda path: str(path).lower()):
        profile = case_profile(directory)
        pid = profile.get("pid")
        proc = process_info(pid)
        if not pid or not proc.get("alive") or str(proc.get("name") or "").lower() != "paws.exe":
            continue
        pid_path = profile.get("pid_path")
        process_started = parse_datetime(proc.get("started_at"))
        if pid_path and process_started:
            try:
                pid_written = datetime.fromtimestamp(pid_path.stat().st_mtime).astimezone()
                if abs((pid_written - process_started).total_seconds()) > 6 * 3600:
                    continue
            except OSError:
                continue
        mapped_pids.add(int(pid))
        latest = None
        for path in (
            directory / f"{profile['runtime_case_id']}.txt",
            directory / "station_struct_diag.txt",
            profile["stdout_path"],
        ):
            parsed = latest_progress(path)
            if parsed and (latest is None or parsed["datetime"] > latest["datetime"]):
                latest = parsed
        rows.append(
            {
                "monitor_id": profile["monitor_id"],
                "case_dir": str(directory),
                "runtime_case_id": profile["runtime_case_id"],
                "formal_case_id": profile["formal_case_id"],
                "pid": pid,
                "process_alive": True,
                "process_path": proc.get("path"),
                "current": _iso(aware(latest["datetime"])) if latest else None,
                "selectable": True,
                "mapped": True,
                "label": f"{profile['runtime_case_id']} · PID {pid}",
            }
        )

    # 让用户看到“还有一个 PAWS 在跑但没有 PID 文件”的事实；为安全起见，
    # 无法确认运行目录时不允许选择它，也不会猜目录。
    for pid in paws_process_ids():
        if pid in mapped_pids:
            continue
        proc = process_info(pid)
        rows.append(
            {
                "monitor_id": f"unmapped-{pid}",
                "case_dir": None,
                "runtime_case_id": f"PID {pid}",
                "formal_case_id": None,
                "pid": pid,
                "process_alive": bool(proc.get("alive")),
                "process_path": proc.get("path"),
                "current": None,
                "selectable": False,
                "mapped": False,
                "label": f"PID {pid} · 未识别运行目录",
                "reason": "找不到与该进程匹配的 paws_pid.txt",
            }
        )
    return sorted(rows, key=lambda row: (not row["selectable"], str(row["runtime_case_id"]).lower()))


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(_local_tz()).isoformat(timespec="seconds") if value else None


def _number(value: Optional[float], digits: int = 3) -> Optional[float]:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def _format_duration(seconds: Optional[float]) -> Optional[str]:
    if seconds is None or not math.isfinite(float(seconds)) or seconds < 0:
        return None
    seconds = int(round(seconds))
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}天 {hours:02d}:{minutes:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _file_info(path: Path, now: datetime) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"name": path.name, "path": str(path), "exists": False, "bytes": 0, "mtime": None, "age_seconds": None}
    mtime = datetime.fromtimestamp(stat.st_mtime).astimezone()
    return {
        "name": path.name,
        "path": str(path),
        "exists": True,
        "bytes": int(stat.st_size),
        "mtime": mtime.isoformat(timespec="seconds"),
        "age_seconds": max(0, int((now - mtime).total_seconds())),
    }


def _model_bounds(profile: dict[str, Any], first: Optional[dict[str, Any]]) -> tuple[Optional[datetime], Optional[datetime]]:
    start = profile.get("model_start")
    end = profile.get("model_end")
    if start and end:
        return start, end
    if first:
        start = aware(first["datetime"])
    return start, end


class SpinupMonitor:
    """单个本地 PAWS case 的只读状态采集器。"""

    def __init__(
        self,
        case_dir: Path | str = DEFAULT_CASE_DIR,
        stale_seconds: int = DEFAULT_STALE_SECONDS,
        *,
        process_backend: str = "windows",
        wsl_distro: Optional[str] = None,
        pid_override: Optional[int] = None,
    ) -> None:
        self.case_dir = Path(case_dir).expanduser().resolve()
        self.stale_seconds = max(30, int(stale_seconds))
        self.process_backend = process_backend
        self.wsl_distro = wsl_distro
        self.pid_override = int(pid_override) if pid_override else None
        self.history: Deque[tuple[datetime, datetime]] = deque(maxlen=240)
        self.last_snapshot: Optional[dict[str, Any]] = None

    def _progress_paths(self, profile: dict[str, Any]) -> list[Path]:
        case_id = str(profile["runtime_case_id"])
        names = [
            f"{case_id}.txt",
            "station_struct_diag.txt",
        ]
        paths: list[Path] = [self.case_dir / name for name in names]
        paths.append(profile["stdout_path"])
        legacy = self.case_dir / "paws_stdout.log"
        if legacy not in paths:
            paths.append(legacy)
        unique: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            key = str(path).lower()
            if key not in seen:
                seen.add(key)
                unique.append(path)
        return unique

    def snapshot(self, now: Optional[datetime] = None) -> dict[str, Any]:
        current_wall = aware(now or datetime.now().astimezone())
        profile = case_profile(
            self.case_dir,
            process_backend=self.process_backend,
            wsl_distro=self.wsl_distro,
            pid_override=self.pid_override,
        )
        manifest = profile["manifest"]
        status = profile["status"]
        result = profile["result"]
        runtime_case_id = str(profile["runtime_case_id"])

        progress_candidates = [latest_progress(path) for path in self._progress_paths(profile)]
        progress_candidates = [item for item in progress_candidates if item is not None]
        progress: Optional[dict[str, Any]] = None
        if progress_candidates:
            progress = max(progress_candidates, key=lambda item: item["datetime"])
            progress["datetime"] = aware(progress["datetime"])
            sample = (current_wall, progress["datetime"])
            if not self.history or self.history[-1][1] != sample[1]:
                self.history.append(sample)

        model_start, model_end = _model_bounds(profile, progress)
        pid = profile["pid"]
        proc = process_info(pid, backend=self.process_backend, wsl_distro=self.wsl_distro)
        started = parse_datetime(status.get("started_at")) or parse_datetime(proc.get("started_at"))
        completed = parse_datetime(result.get("completed_at"))
        wall_end = completed or current_wall
        if started is None:
            try:
                pid_path = profile.get("pid_path")
                if not pid_path:
                    raise OSError("PID path unavailable")
                started = datetime.fromtimestamp(pid_path.stat().st_mtime).astimezone()
            except OSError:
                started = current_wall
        wall_elapsed = max(0.0, (wall_end - started).total_seconds())

        sim_elapsed = None
        progress_pct = None
        remaining_sim_seconds = None
        if progress and model_start:
            sim_elapsed = max(0.0, (progress["datetime"] - model_start).total_seconds())
        if progress and model_start and model_end and model_end > model_start:
            total_sim_seconds = (model_end - model_start).total_seconds()
            fraction = max(0.0, min(1.0, (progress["datetime"] - model_start).total_seconds() / total_sim_seconds))
            progress_pct = fraction * 100.0
            remaining_sim_seconds = max(0.0, (model_end - progress["datetime"]).total_seconds())

        avg_ratio = (sim_elapsed / wall_elapsed) if sim_elapsed is not None and wall_elapsed > 1 else None
        # ratio is simulated-hours / wall-clock-hour; convert hours to days.
        avg_days_per_wall_hour = avg_ratio / 24.0 if avg_ratio is not None else None

        recent_ratio = None
        recent_pair: Optional[tuple[datetime, datetime]] = None
        if len(self.history) >= 2:
            newest = self.history[-1]
            for older in reversed(self.history):
                wall_delta = (newest[0] - older[0]).total_seconds()
                sim_delta = (newest[1] - older[1]).total_seconds()
                if wall_delta >= 30 and sim_delta > 0 and wall_delta <= WINDOW_SECONDS:
                    recent_pair = older
                    recent_ratio = sim_delta / wall_delta
                    break
        recent_days_per_wall_hour = recent_ratio / 24.0 if recent_ratio is not None else None
        chosen_ratio = recent_ratio or avg_ratio
        eta_seconds = (remaining_sim_seconds / chosen_ratio) if chosen_ratio and remaining_sim_seconds is not None else None

        files_to_report = [
            f"{runtime_case_id}.txt",
            "station_struct_diag.txt",
            profile["stdout_path"].name,
            "paws_stderr.log",
            "month.mat",
            "month_CLM.mat",
            f"{runtime_case_id}_Rec.txt",
            "cRec.txt",
        ]
        files: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in files_to_report:
            if name in seen:
                continue
            seen.add(name)
            files.append(_file_info(_run_file(self.case_dir, name), current_wall))

        stderr_info = next((item for item in files if item["name"] == "paws_stderr.log"), None)
        progress_file_mtime: Optional[datetime] = None
        if progress and progress.get("file_mtime"):
            progress_file_mtime = parse_datetime(progress["file_mtime"])
        progress_age = (current_wall - progress_file_mtime).total_seconds() if progress_file_mtime else None

        state_text = str(result.get("status") or "").lower()
        if result:
            run_state = state_text or ("completed" if result.get("return_code") == 0 else "failed")
        elif proc.get("alive"):
            run_state = "running"
        elif status.get("status"):
            run_state = str(status.get("status")).lower()
        else:
            run_state = "not_started" if not manifest else "unknown"

        warnings: list[dict[str, str]] = []
        if model_start is None or model_end is None:
            warnings.append({"level": "warning", "code": "TIME_CONTRACT_MISSING", "message": "未能从 manifest 或 stdout 确认完整模型起止时间，暂不计算百分比和 ETA"})
        if pid is None and run_state in {"running", "unknown"}:
            warnings.append({"level": "warning", "code": "PID_MISSING", "message": "尚未找到 paws_pid.txt"})
        if result.get("return_code") not in (None, 0):
            warnings.append({"level": "critical", "code": "RETURN_CODE", "message": f"PAWS 返回码为 {result.get('return_code')}"})
        if stderr_info and stderr_info["bytes"]:
            warnings.append({"level": "critical", "code": "STDERR_NONEMPTY", "message": f"paws_stderr.log 非空（{stderr_info['bytes']:,} B）"})
        if run_state in {"running", "unknown"} and progress is None:
            warnings.append({"level": "warning", "code": "NO_PROGRESS", "message": "运行中但还没有可解析的模拟日期"})
        if proc.get("alive") and progress_age is not None and progress_age > self.stale_seconds:
            warnings.append({"level": "critical", "code": "PROGRESS_STALE", "message": f"进度文件已 {int(progress_age // 60)} 分钟没有更新"})
        if pid is not None and not proc.get("alive") and not result and run_state == "running":
            warnings.append({"level": "critical", "code": "PROCESS_EXITED", "message": f"PID {pid} 已退出但没有最终结果文件"})

        log_payload = {
            "stdout": tail_lines(profile["stdout_path"], 8),
            "stderr": tail_lines(_run_file(self.case_dir, "paws_stderr.log"), 8),
        }
        if not log_payload["stdout"]:
            log_payload["stdout"] = tail_lines(_run_file(self.case_dir, "paws_stdout.log"), 8)

        history_payload: list[dict[str, Any]] = []
        for wall, sim in self.history:
            point: dict[str, Any] = {"wall_time": _iso(wall), "sim_time": _iso(sim)}
            if model_start and model_end and model_end > model_start:
                point["progress_pct"] = _number(max(0.0, min(100.0, (sim - model_start).total_seconds() / (model_end - model_start).total_seconds() * 100)), 2)
            history_payload.append(point)

        snapshot = {
            "schema_version": 1,
            "read_only": True,
            "monitor_id": profile["monitor_id"],
            "sampled_at": _iso(current_wall),
            "case_dir": str(self.case_dir),
            "process_backend": self.process_backend,
            "wsl_distro": self.wsl_distro,
            "formal_case_id": profile["formal_case_id"],
            "runtime_case_id": runtime_case_id,
            "state": run_state,
            "status_file": status,
            "result_file": result,
            "process": proc,
            "model": {
                "start": _iso(model_start),
                "end": _iso(model_end),
                "calendar_years": manifest.get("calendar_years"),
                "target_days": manifest.get("target_days"),
                "expected_hourly_rows": manifest.get("expected_hourly_rows"),
            },
            "metadata_sources": {
                "manifest": str(profile["manifest_path"]) if profile["manifest_path"] else None,
                "status": str(profile["status_path"]) if profile["status_path"] else None,
                "result": str(profile["result_path"]) if profile["result_path"] else None,
                "stdout": str(profile["stdout_path"]),
                "pid": str(profile["pid_path"]) if profile["pid_path"] else None,
            },
            "progress": {
                "current": _iso(progress["datetime"]) if progress else None,
                "current_date": progress.get("date") if progress else None,
                "current_clock": progress.get("clock") if progress else None,
                "source": progress.get("source") if progress else None,
                "line_preview": progress.get("line_preview") if progress else None,
                "progress_pct": _number(progress_pct, 2),
                "sim_elapsed_days": _number(sim_elapsed / 86_400 if sim_elapsed is not None else None, 3),
                "remaining_days": _number(remaining_sim_seconds / 86_400 if remaining_sim_seconds is not None else None, 3),
                "last_update_age_seconds": _number(progress_age, 1),
            },
            "speed": {
                "average_sim_days_per_wall_hour": _number(avg_days_per_wall_hour, 3),
                "recent_sim_days_per_wall_hour": _number(recent_days_per_wall_hour, 3),
                "average_sim_hours_per_wall_hour": _number(avg_ratio, 3),
                "recent_sim_hours_per_wall_hour": _number(recent_ratio, 3),
                "wall_elapsed_seconds": _number(wall_elapsed, 1),
                "wall_elapsed_text": _format_duration(wall_elapsed),
                "eta_seconds": _number(eta_seconds, 1),
                "eta_text": _format_duration(eta_seconds),
                "expected_finish_at": _iso(current_wall + timedelta(seconds=eta_seconds)) if eta_seconds is not None else None,
            },
            "files": files,
            "logs": log_payload,
            "warnings": warnings,
            "health": {
                "process_alive": bool(proc.get("alive")),
                "stderr_bytes": int(stderr_info["bytes"]) if stderr_info else 0,
                "progress_stale": bool(progress_age is not None and progress_age > self.stale_seconds),
                "progress_file_age_seconds": _number(progress_age, 1),
            },
            "history": history_payload,
        }
        self.last_snapshot = snapshot
        return snapshot


def render_terminal(snapshot: dict[str, Any]) -> str:
    """给 PowerShell/终端模式使用的紧凑中文状态页。"""

    progress = snapshot.get("progress") or {}
    speed = snapshot.get("speed") or {}
    process = snapshot.get("process") or {}
    warnings = snapshot.get("warnings") or []
    pct = progress.get("progress_pct")
    bar_width = 36
    filled = int(round(bar_width * float(pct or 0) / 100.0))
    bar = "█" * filled + "·" * (bar_width - filled)
    lines = [
        "PAWS 连续 60 年 spin-up 只读监控",
        f"Case: {snapshot.get('case_dir')}",
        f"状态: {snapshot.get('state')}    采样: {snapshot.get('sampled_at')}",
        f"模拟日期: {progress.get('current') or '等待输出'}",
        f"进度: [{bar}] {pct if pct is not None else '—'}%",
        f"平均速度: {speed.get('average_sim_days_per_wall_hour') or '—'} 模拟日/墙钟小时",
        f"最近速度: {speed.get('recent_sim_days_per_wall_hour') or '—'} 模拟日/墙钟小时",
        f"已运行: {speed.get('wall_elapsed_text') or '—'}    预计剩余: {speed.get('eta_text') or '—'}",
        f"PAWS PID: {process.get('pid') or '—'}    存活: {'是' if process.get('alive') else '否'}",
    ]
    if process.get("working_set_bytes"):
        lines[-1] += f"    内存: {process['working_set_bytes'] / (1024**3):.2f} GB"
    if warnings:
        lines.append("告警:")
        lines.extend(f"  [{item.get('level')}] {item.get('message')}" for item in warnings)
    lines.append("关闭监控窗口不会停止 PAWS。")
    return "\n".join(lines)
