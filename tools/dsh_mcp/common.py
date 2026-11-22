# -*- coding: utf-8 -*-
r"""tools/dsh_mcp 共用层：路径、工作区白名单、路径安全、审计、job 元数据。

设计参照 tools/fleet_mcp.py 的安全哲学：
  * 业务动作不直接抛异常给远端，统一返回 {"ok": bool, ...}；
  * 每个写动作默认 dry-run，apply=true 才真执行；
  * 每次 MCP 调用逐条追加审计到 ops/dsh_mcp_audit.jsonl（永不因审计失败中断动作）。
"""
from __future__ import annotations

import ctypes
import datetime as dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量                                                                          #
# --------------------------------------------------------------------------- #

PROJECT = Path(__file__).resolve().parent.parent.parent      # <repo root>\
OPS_DIR = PROJECT / "ops"
AUDIT_PATH = OPS_DIR / "dsh_mcp_audit.jsonl"
RUNTIME_DIR = PROJECT / "runtime"
JOBS_DIR = RUNTIME_DIR / "dsh_jobs"
HERE = Path(__file__).resolve().parent
PATCH_FILE = HERE / "headless-account.patch.yml"
RUNNER_SCRIPT = HERE / "job_runner.py"

# 目录白名单：硬编码，不接受远端传入的任意绝对路径。
WORKSPACES: dict[str, Path] = {
    "main": Path(r"D:\maa-cli-fleet"),
    "slot2": Path(r"D:\maa-cli-fleet-b"),
}

# 读取文件时的敏感文件黑名单（按路径任意一段匹配）。
SENSITIVE_NAMES = {"accounts.json", ".env"}
SENSITIVE_GLOBS = ("*token*", "*secret*")

# job 状态机
ACTIVE_STATUSES = frozenset({"starting", "running", "cancelling"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "timeout"})
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES | {"unknown"}

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)

_audit_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 时间 / 审计                                                                   #
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def now_ts() -> float:
    return time.time()


def audit(tool: str, *, workspace: str | None = None, apply: bool | None = None,
          job_id: str | None = None, status: str = "ok", **extra) -> None:
    """逐条追加审计。字段固定包含 ts/tool/workspace/apply/job_id/status。"""
    rec = {
        "ts": now_iso(),
        "tool": tool,
        "workspace": workspace,
        "apply": apply,
        "job_id": job_id,
        "status": status,
    }
    for key, val in extra.items():
        if val is not None:
            rec[key] = val
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    try:
        with _audit_lock:
            OPS_DIR.mkdir(parents=True, exist_ok=True)
            with AUDIT_PATH.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception:  # noqa: BLE001 —— 审计失败绝不阻断业务
        pass


# --------------------------------------------------------------------------- #
# 工作区与路径安全                                                               #
# --------------------------------------------------------------------------- #

def resolve_workspace(name: str) -> Path:
    if not isinstance(name, str) or name not in WORKSPACES:
        raise ValueError("未知 workspace %r（允许：%s）" % (name, ", ".join(WORKSPACES)))
    return WORKSPACES[name]


def is_sensitive(rel: Path) -> bool:
    """敏感文件判定：accounts.json / .env / *token* / *secret*，命中任意路径段即拒读。"""
    for part in rel.parts:
        low = part.lower()
        if low in SENSITIVE_NAMES:
            return True
        if any(fnmatch.fnmatch(low, pat) for pat in SENSITIVE_GLOBS):
            return True
    return False


def safe_read_path(workspace: str, rel_path: str) -> Path:
    """把远端给的相对路径解析成白名单工作区内的真实路径。

    拒绝：绝对路径、盘符、UNC、`..` 穿越、解析后越界、敏感文件。
    """
    root = resolve_workspace(workspace)
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise ValueError("rel_path 必填（工作区内的相对路径）")
    raw = rel_path.strip().replace("\\", "/")
    p = Path(raw)
    if p.is_absolute() or p.drive:
        raise ValueError("rel_path 必须是相对路径，禁止绝对路径/盘符")
    if raw.startswith("//"):
        raise ValueError("rel_path 禁止 UNC 路径")
    if any(part == ".." for part in p.parts):
        raise ValueError("rel_path 禁止包含 '..' 路径穿越")

    root_real = root.resolve()
    target = (root_real / p).resolve()
    if target != root_real and root_real not in target.parents:
        raise ValueError("rel_path 解析后越出白名单工作区")
    rel = target.relative_to(root_real)
    if is_sensitive(rel):
        raise PermissionError("拒绝读取敏感文件：%s" % rel)
    return target


# --------------------------------------------------------------------------- #
# 子进程环境 / git                                                              #
# --------------------------------------------------------------------------- #

# 从当前 DSH 会话继承下来、会干扰独立子代理的变量。
_DROP_ENV = {
    "DSH_SESSION_ID",
    "DSH_WEB_URL",
    "DSH_PROFILE",
    "DSH_PROFILE_DIR",
    "DSH_SHELL",
    "DSH_USER_QUESTIONS",
    "DSH_PERMISSION_MODE",
    "DSH_MCP_TOKEN",          # 网关自己的 Token 不传递给子代理
}


def child_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k.upper() not in _DROP_ENV}
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return env


def find_dsh() -> str | None:
    """定位 dsh CLI：DSH_CLI 覆盖 → PATH → 本机安装默认路径。"""
    override = (os.environ.get("DSH_CLI") or "").strip()
    if override:
        p = Path(override)
        if p.is_file():
            return str(p)
    found = shutil.which("dsh")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    cand = Path(local) / "Programs" / "DeepSeek Harness" / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd"
    if cand.is_file():
        return str(cand)
    return None


def run_git(root: Path, args: list[str], timeout: int = 20) -> dict:
    git = shutil.which("git") or "git"
    try:
        p = subprocess.run(
            [git, "-C", str(root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=child_env(), stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        return {"rc": p.returncode, "stdout": (p.stdout or ""), "stderr": (p.stderr or "").strip()}
    except Exception as ex:  # noqa: BLE001
        return {"rc": -1, "stdout": "", "stderr": "%s: %s" % (type(ex).__name__, ex)}


def git_info(root: Path, max_status_lines: int = 200) -> dict:
    """只读 git 快照：分支 + 精简状态。非 git 目录时返回 ok=false。"""
    if not root.is_dir():
        return {"ok": False, "error": "目录不存在或不是目录"}
    top = run_git(root, ["rev-parse", "--show-toplevel"])
    if top["rc"] != 0:
        return {"ok": False, "error": "不是 git 仓库", "detail": top["stderr"][:300]}
    branch = run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    status = run_git(root, ["status", "--short"])
    lines = status["stdout"].splitlines()
    return {
        "ok": True,
        "branch": branch["stdout"].strip() if branch["rc"] == 0 else None,
        "dirty": bool(lines),
        "status_short": lines[:max_status_lines],
        "status_truncated": len(lines) > max_status_lines,
        "status_total": len(lines),
    }


# --------------------------------------------------------------------------- #
# 进程存活 / 结束                                                               #
# --------------------------------------------------------------------------- #

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    pid = int(pid)
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


def kill_tree(pid: int | None, wait_sec: float = 8.0) -> bool:
    """按 PID 结束整棵进程树（taskkill /T /F），等待其真正消失。"""
    if not pid:
        return False
    pid = int(pid)
    try:
        # 用二进制捕获：taskkill 的输出随系统语言可能是 GBK，text=True 会在读线程里炸。
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:  # noqa: BLE001
        pass
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


# --------------------------------------------------------------------------- #
# JSON / 文件                                                                   #
# --------------------------------------------------------------------------- #

def read_json(path: Path, default=None):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:  # noqa: BLE001
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def tail_lines(path: Path, n: int) -> list[str]:
    """高效读取文件尾部 n 行（最多回读 256 KiB）。"""
    if not path.is_file():
        return []
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block = min(size, 256 * 1024)
        if block:
            f.seek(size - block, os.SEEK_SET)
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-max(1, n):]


def parse_ndjson_output(path: Path, max_bytes: int = 4 * 1024 * 1024) -> dict:
    """从 dsh --json 的 NDJSON 输出提取 final / session / usage 摘要（只回读尾部）。"""
    info: dict = {"final_text": None, "session_id": None, "reason": None,
                  "usage": None, "last_text": None, "events": 0}
    if not path.is_file():
        return info
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, max_bytes)
            if block:
                f.seek(size - block, os.SEEK_SET)
            data = f.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return info
    for line in data.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        info["events"] += 1
        etype = ev.get("type")
        if etype == "session":
            info["session_id"] = ev.get("sessionId") or info["session_id"]
        elif etype == "final":
            info["session_id"] = ev.get("sessionId") or info["session_id"]
            info["final_text"] = ev.get("text")
            info["reason"] = ev.get("reason")
        elif etype == "text" and ev.get("text"):
            info["last_text"] = ev.get("text")
        elif etype == "status" and isinstance(ev.get("usage"), dict):
            info["usage"] = ev.get("usage")
    return info


# --------------------------------------------------------------------------- #
# Job 元数据                                                                    #
# --------------------------------------------------------------------------- #

def new_job_id(workspace: str) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return "%s-%s-%s" % (stamp, workspace, uuid.uuid4().hex[:6])


def job_dir(job_id: str) -> Path:
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise ValueError("非法 job_id：%r" % (job_id,))
    root = JOBS_DIR.resolve()
    d = (root / job_id).resolve()
    if root not in d.parents:
        raise ValueError("非法 job_id（越界）：%r" % (job_id,))
    return d


def meta_path(d: Path) -> Path:
    return d / "meta.json"


def read_meta(d: Path) -> dict | None:
    return read_json(meta_path(d))


def write_meta(d: Path, meta: dict) -> None:
    write_json(meta_path(d), meta)


def iter_job_dirs(limit: int = 1000) -> list[Path]:
    if not JOBS_DIR.is_dir():
        return []
    dirs = [d for d in JOBS_DIR.iterdir() if d.is_dir() and JOB_ID_RE.match(d.name)]
    dirs.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return dirs[:limit]


def elapsed_sec(meta: dict) -> float:
    duration = meta.get("duration_sec")
    if isinstance(duration, (int, float)):
        return round(float(duration), 1)
    base = meta.get("started_ts") or meta.get("created_ts") or 0
    try:
        return round(max(0.0, now_ts() - float(base)), 1)
    except (TypeError, ValueError):
        return 0.0


def refresh_job(d: Path) -> dict | None:
    """按 exit.json / runner 存活状态推进 job 状态，并把变化落回 meta.json。"""
    meta = read_meta(d)
    if not meta:
        return None
    status = meta.get("status", "unknown")
    if status in TERMINAL_STATUSES:
        return meta

    exit_rec = read_json(d / "exit.json", None)
    runner_info = read_json(d / "runner.json", None) or {}
    runner_pid = meta.get("runner_pid") or runner_info.get("runner_pid")
    changed = False

    if exit_rec:
        new_status = exit_rec.get("status") or "failed"
        if status == "cancelling":
            new_status = "cancelled"
        meta["status"] = new_status if new_status in TERMINAL_STATUSES else "failed"
        for key in ("exit_code", "ended_at", "duration_sec", "session_id",
                    "final_text", "reason", "timed_out", "error", "usage", "events"):
            if key in exit_rec:
                meta[key] = exit_rec[key]
        changed = True
    elif status == "cancelling":
        if runner_pid and not pid_alive(runner_pid):
            meta["status"] = "cancelled"
            meta["ended_at"] = meta.get("ended_at") or now_iso()
            meta["duration_sec"] = elapsed_sec(meta)
            changed = True
    elif runner_pid and not pid_alive(runner_pid):
        meta["status"] = "failed"
        meta["error"] = "job runner 已退出但未写 exit.json（进程可能被外部结束）"
        meta["ended_at"] = now_iso()
        meta["duration_sec"] = elapsed_sec(meta)
        changed = True

    if changed:
        write_meta(d, meta)
    return meta


def active_job_for_workspace(workspace: str) -> dict | None:
    for d in iter_job_dirs():
        meta = refresh_job(d)
        if meta and meta.get("workspace") == workspace and meta.get("status") in ACTIVE_STATUSES:
            return meta
    return None


def active_jobs() -> list[dict]:
    out = []
    for d in iter_job_dirs():
        meta = refresh_job(d)
        if meta and meta.get("status") in ACTIVE_STATUSES:
            out.append(meta)
    return out
