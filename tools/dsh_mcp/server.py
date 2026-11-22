# -*- coding: utf-8 -*-
r"""DSH 远程 MCP 网关 —— 把本机 DSH 封装成受控子代理（Streamable HTTP + Bearer）。

协议：Python 官方 mcp SDK，Streamable HTTP，默认路径 /mcp，仅监听 127.0.0.1:8795。
鉴权：除 GET /health（固定返回 {"ok": true}）外，所有 /mcp 请求必须带 Bearer：
      * 内置 OAuth 2.1 签发的 access_token（给 Gemini 等远程 MCP 客户端）；
      * 或本机 DSH_MCP_TOKEN（兼容本地脚本/冒烟）。
      未认证 401 会带 RFC 9728 的 WWW-Authenticate: resource_metadata=...，
      客户端据此走 /.well-known/* 发现 → DCR → PKCE 授权码 → token 流程。
      DSH_MCP_TOKEN 为空时拒绝启动；它同时充当 OAuth 授权页的“本机口令”。
安全：工作区白名单硬编码（main / slot2）；读文件额外禁止绝对路径、`..` 穿越和
      敏感文件（accounts.json / .env / *token* / *secret*）。
审计：每次调用逐条追加 ops/dsh_mcp_audit.jsonl。
异步：dsh_dispatch 只负责后台拉起 job_runner 并立即返回 job_id，绝不阻塞等待。

冒烟：python tools/dsh_mcp/server.py --check
启动：设置 DSH_MCP_TOKEN 后运行 tools/dsh_mcp/start.ps1（或本文件）。
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import oauth  # noqa: E402
import ws_tools  # noqa: E402

import anyio  # noqa: E402
import mcp.types as types  # noqa: E402
import uvicorn  # noqa: E402
from mcp.server.lowlevel import Server  # noqa: E402
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

SERVER_NAME = "dsh-mcp"
SERVER_VERSION = "1.0.0"
DEFAULT_HOST = "127.0.0.1"          # 红线：绝不监听 0.0.0.0
DEFAULT_PORT = 8795
MCP_PATH = "/mcp"
HEALTH_PATH = "/health"

MAX_PROMPT_CHARS = 200_000
MAX_TAIL_LINES = 500
MAX_READ_CHARS = 200_000
MAX_DIFF_CHARS = 8_000
MAX_STATUS_LINES = 200

_dispatch_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 小工具                                                                        #
# --------------------------------------------------------------------------- #

def _err(message: str, **extra) -> dict:
    out = {"ok": False, "error": message}
    out.update(extra)
    return out


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _permission_mode() -> str:
    perm = (os.environ.get("DSH_MCP_PERMISSION_MODE") or "workspace-write").strip()
    if perm not in ("read-only", "workspace-write", "danger-full-access"):
        return "workspace-write"
    return perm


def _dsh_command_preview() -> list[str]:
    dsh = common.find_dsh() or "dsh.cmd"
    argv = [dsh, "--profile", "headless"]
    if common.PATCH_FILE.is_file():
        argv += ["--patch", str(common.PATCH_FILE)]
    argv += ["--permission-mode", _permission_mode(), "--approval-policy", "never",
             "--json", "-"]
    return argv


# --------------------------------------------------------------------------- #
# 工具实现                                                                      #
# --------------------------------------------------------------------------- #

def t_list_workspaces(args: dict) -> dict:
    workspaces = []
    for name, root in common.WORKSPACES.items():
        entry = {
            "workspace": name,
            "path": str(root),
            "exists": root.is_dir(),
            "running_job_id": None,
        }
        if root.is_dir():
            entry["git"] = common.git_info(root)
        active = common.active_job_for_workspace(name)
        if active:
            entry["running_job_id"] = active.get("job_id")
            entry["running_job_started_at"] = active.get("started_at")
        workspaces.append(entry)

    running = []
    for meta in common.active_jobs():
        running.append({
            "job_id": meta.get("job_id"),
            "workspace": meta.get("workspace"),
            "status": meta.get("status"),
            "started_at": meta.get("started_at"),
            "elapsed_sec": common.elapsed_sec(meta),
        })
    return {"ok": True, "workspaces": workspaces, "running_jobs": running,
            "jobs_dir": str(common.JOBS_DIR), "audit_log": str(common.AUDIT_PATH)}


def t_dispatch(args: dict) -> dict:
    workspace = str(args.get("workspace", "") or "")
    prompt = args.get("prompt")
    apply = _as_bool(args.get("apply", False))
    timeout_sec = _clamp_int(args.get("timeout_sec", 600), 600, 10, 86_400)

    if not isinstance(prompt, str) or not prompt.strip():
        return _err("prompt 必填且不能为空")
    if len(prompt) > MAX_PROMPT_CHARS:
        return _err("prompt 过长（%d > %d 字符）" % (len(prompt), MAX_PROMPT_CHARS))
    try:
        root = common.resolve_workspace(workspace)
    except ValueError as ex:
        return _err(str(ex))
    if not root.is_dir():
        return _err("工作区目录不存在：%s" % root, workspace=workspace)

    perm = _permission_mode()
    cmd = _dsh_command_preview()

    with _dispatch_lock:
        active = common.active_job_for_workspace(workspace)
        if active:
            return _err(
                "workspace %s 已有 running 任务，严格互斥：%s（先 dsh_status 或 dsh_cancel）"
                % (workspace, active.get("job_id")),
                workspace=workspace, job_id=active.get("job_id"),
                running_status=active.get("status"),
            )

        if not apply:
            return {
                "ok": True,
                "dry_run": True,
                "workspace": workspace,
                "cwd": str(root),
                "command": cmd,
                "prompt_delivery": "stdin",
                "prompt_preview": prompt[:200] + ("…" if len(prompt) > 200 else ""),
                "timeout_sec": timeout_sec,
                "permission_mode": perm,
                "lock": {"workspace": workspace, "busy": False},
                "would_create_under": str(common.JOBS_DIR / "<job_id>"),
                "note": "dry-run：未启动任何进程；确认后传 apply=true 才真执行",
            }

        job_id = common.new_job_id(workspace)
        d = common.JOBS_DIR / job_id
        d.mkdir(parents=True, exist_ok=False)
        meta = {
            "job_id": job_id,
            "tool": "dsh_dispatch",
            "workspace": workspace,
            "cwd": str(root),
            "prompt": prompt,
            "prompt_preview": prompt[:200],
            "apply": True,
            "timeout_sec": timeout_sec,
            "permission_mode": perm,
            "status": "starting",
            "created_at": common.now_iso(),
            "created_ts": common.now_ts(),
            "started_at": common.now_iso(),
            "started_ts": common.now_ts(),
            "runner_pid": None,
            "dsh_pid": None,
        }
        common.write_meta(d, meta)

        runner_argv = [sys.executable, str(common.RUNNER_SCRIPT), str(d)]
        log_path = d / "runner.log"
        try:
            with log_path.open("ab") as logf:
                proc = subprocess.Popen(
                    runner_argv,
                    cwd=str(common.PROJECT),
                    env=common.child_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    creationflags=common.CREATE_NO_WINDOW,
                )
        except Exception as ex:  # noqa: BLE001
            meta["status"] = "failed"
            meta["error"] = "拉起 job_runner 失败：%s: %s" % (type(ex).__name__, ex)
            meta["ended_at"] = common.now_iso()
            common.write_meta(d, meta)
            return _err(meta["error"], workspace=workspace, job_id=job_id)

        meta["runner_pid"] = proc.pid
        meta["status"] = "running"
        common.write_meta(d, meta)

    return {
        "ok": True,
        "job_id": job_id,
        "workspace": workspace,
        "status": "running",
        "cwd": str(root),
        "timeout_sec": timeout_sec,
        "permission_mode": perm,
        "output_log": str(d / "output.log"),
        "runner_log": str(log_path),
        "note": "后台已启动；用 dsh_status / dsh_result 轮询，不要同步等待",
    }


def _job_or_error(job_id: str):
    try:
        d = common.job_dir(job_id)
    except ValueError as ex:
        return None, _err(str(ex))
    if not d.is_dir():
        return None, _err("未知 job_id：%s" % job_id, job_id=job_id)
    return d, None


def t_status(args: dict) -> dict:
    job_id = str(args.get("job_id", "") or "")
    tail_n = _clamp_int(args.get("tail_lines", 50), 50, 1, MAX_TAIL_LINES)
    d, err = _job_or_error(job_id)
    if err:
        return err
    meta = common.refresh_job(d)
    if not meta:
        return _err("job 元数据缺失：%s" % job_id, job_id=job_id)

    status = meta.get("status", "unknown")
    payload = {
        "ok": True,
        "job_id": job_id,
        "workspace": meta.get("workspace"),
        "status": status,
        "elapsed_sec": common.elapsed_sec(meta),
        "exit_code": meta.get("exit_code"),
        "timeout_sec": meta.get("timeout_sec"),
        "created_at": meta.get("created_at"),
        "started_at": meta.get("started_at"),
        "ended_at": meta.get("ended_at"),
        "session_id": meta.get("session_id"),
        "output_log": str(d / "output.log"),
        "tail_lines_requested": tail_n,
        "tail": common.tail_lines(d / "output.log", tail_n),
    }
    if meta.get("error"):
        payload["error"] = meta["error"]
    return payload


def _git_result(root: Path) -> dict:
    info = common.git_info(root)
    if not info.get("ok"):
        return info
    diff = common.run_git(root, ["diff", "HEAD"], timeout=25)
    diff_stat = common.run_git(root, ["diff", "HEAD", "--stat"], timeout=25)
    diff_text = diff.get("stdout", "")
    return {
        "ok": True,
        "branch": info.get("branch"),
        "status_short": info.get("status_short"),
        "status_total": info.get("status_total"),
        "status_truncated": info.get("status_truncated"),
        "diff_stat": diff_stat.get("stdout", "")[:4_000],
        "diff": diff_text[:MAX_DIFF_CHARS],
        "diff_truncated": len(diff_text) > MAX_DIFF_CHARS,
        "diff_total_chars": len(diff_text),
    }


def t_result(args: dict) -> dict:
    job_id = str(args.get("job_id", "") or "")
    include_git = _as_bool(args.get("include_git_diff", True), True)
    d, err = _job_or_error(job_id)
    if err:
        return err
    meta = common.refresh_job(d)
    if not meta:
        return _err("job 元数据缺失：%s" % job_id, job_id=job_id)

    info = common.parse_ndjson_output(d / "output.log")
    final_text = meta.get("final_text") or info.get("final_text")
    payload = {
        "ok": True,
        "job_id": job_id,
        "workspace": meta.get("workspace"),
        "status": meta.get("status"),
        "exit_code": meta.get("exit_code"),
        "duration_sec": common.elapsed_sec(meta),
        "ended_at": meta.get("ended_at"),
        "session_id": meta.get("session_id") or info.get("session_id"),
        "final_text": final_text,
        "last_text": info.get("last_text"),
        "reason": meta.get("reason") or info.get("reason"),
        "usage": meta.get("usage") or info.get("usage"),
        "output_log": str(d / "output.log"),
    }
    if meta.get("error"):
        payload["error"] = meta["error"]
    if include_git:
        workspace = meta.get("workspace")
        root = common.WORKSPACES.get(workspace) if isinstance(workspace, str) else None
        if root is not None:
            payload["git"] = _git_result(root)
    return payload


def t_cancel(args: dict) -> dict:
    job_id = str(args.get("job_id", "") or "")
    d, err = _job_or_error(job_id)
    if err:
        return err
    meta = common.refresh_job(d) or {}
    status = meta.get("status", "unknown")
    if status in common.TERMINAL_STATUSES:
        return {"ok": True, "job_id": job_id, "workspace": meta.get("workspace"),
                "status": status, "note": "任务已结束，无需取消"}

    meta["status"] = "cancelling"
    meta["cancel_requested_at"] = common.now_iso()
    common.write_meta(d, meta)

    runner_info = common.read_json(d / "runner.json", None) or {}
    candidate_pids = [runner_info.get("runner_pid"), runner_info.get("dsh_pid"),
                      meta.get("runner_pid"), meta.get("dsh_pid")]
    killed = []
    seen = set()
    for pid in candidate_pids:
        if not pid:
            continue
        pid = int(pid)
        if pid in seen:
            continue
        seen.add(pid)
        if common.pid_alive(pid):
            common.kill_tree(pid)
            if not common.pid_alive(pid):
                killed.append(pid)

    meta = common.read_meta(d) or meta
    meta["status"] = "cancelled"
    meta["cancelled_at"] = common.now_iso()
    meta["ended_at"] = meta.get("ended_at") or common.now_iso()
    meta["duration_sec"] = common.elapsed_sec(meta)
    common.write_meta(d, meta)
    return {"ok": True, "job_id": job_id, "workspace": meta.get("workspace"),
            "status": "cancelled", "killed_pids": killed,
            "note": "已结束整个进程树（taskkill /T /F）"}


def t_read_file(args: dict) -> dict:
    workspace = str(args.get("workspace", "") or "")
    rel_path = args.get("rel_path", "")
    max_chars = _clamp_int(args.get("max_chars", 8000), 8000, 1, MAX_READ_CHARS)
    try:
        target = common.safe_read_path(workspace, rel_path)
    except ValueError as ex:
        return _err(str(ex), workspace=workspace)
    except PermissionError as ex:
        return _err(str(ex), workspace=workspace)
    if not target.is_file():
        return _err("不是文件或不存在：%s" % rel_path, workspace=workspace)

    try:
        with target.open("r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars + 1)
        size_bytes = target.stat().st_size
    except Exception as ex:  # noqa: BLE001
        return _err("读取失败：%s: %s" % (type(ex).__name__, ex), workspace=workspace)

    truncated = len(content) > max_chars
    content = content[:max_chars]
    return {
        "ok": True,
        "workspace": workspace,
        "rel_path": str(rel_path),
        "path": str(target),
        "size_bytes": size_bytes,
        "max_chars": max_chars,
        "chars": len(content),
        "truncated": truncated,
        "content": content,
    }


_HANDLERS = {
    "dsh_list_workspaces": t_list_workspaces,
    "dsh_dispatch": t_dispatch,
    "dsh_status": t_status,
    "dsh_result": t_result,
    "dsh_cancel": t_cancel,
    "dsh_read_file": t_read_file,
}

# ws_* 由 ws_tools 自己执行并在内部逐条写审计（成功/参数错误/异常都写），
# 因此 _call_tool 对它们不再调用 _audit_call，避免同一次调用记两条。
_WS_HANDLERS = dict(ws_tools.HANDLERS)
_ALL_HANDLERS = {**_HANDLERS, **_WS_HANDLERS}


# --------------------------------------------------------------------------- #
# MCP 工具面                                                                    #
# --------------------------------------------------------------------------- #

def _tool_schemas() -> list[types.Tool]:
    workspace_prop = {
        "type": "string",
        "enum": list(common.WORKSPACES),
        "description": "白名单工作区逻辑名：%s" % " / ".join(common.WORKSPACES),
    }
    apply_prop = {
        "type": "boolean",
        "default": False,
        "description": "默认 false=dry-run（只预览命令）；true 才在后台真正启动 DSH 子进程",
    }
    return [
        types.Tool(
            name="dsh_list_workspaces",
            description="只读：列出白名单工作区、各自 git 分支/状态，以及当前是否已有 running 的 job_id。",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="dsh_dispatch",
            description="写：给指定工作区派一个 DSH 子代理任务。默认 dry-run 只返回命令预览；"
                        "apply=true 时检查同工作区互斥锁后在后台拉起 headless DSH，并立即返回 job_id（不等待执行完）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": workspace_prop,
                    "prompt": {"type": "string", "description": "交给 DSH 子代理的完整任务文本"},
                    "apply": apply_prop,
                    "timeout_sec": {"type": "integer", "default": 600,
                                    "description": "任务超时秒数（10–86400），超时会结束整个进程树"},
                },
                "required": ["workspace", "prompt"],
            },
        ),
        types.Tool(
            name="dsh_status",
            description="只读：按 job_id 查任务状态（running/succeeded/failed/cancelled/timeout）、耗时、退出码和日志尾部。",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "dsh_dispatch 返回的 job_id"},
                    "tail_lines": {"type": "integer", "default": 50,
                                   "description": "返回 output.log 尾部行数（1–500）"},
                },
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="dsh_result",
            description="只读：返回任务最终输出摘要、session、退出码，以及目标工作区的 git status --short 和精简 diff（超 8000 字符截断）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "dsh_dispatch 返回的 job_id"},
                    "include_git_diff": {"type": "boolean", "default": True,
                                         "description": "是否附带 git status/diff"},
                },
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="dsh_cancel",
            description="写（急停）：强制结束该 job_id 的整个进程树并标记 cancelled。",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "要急停的 job_id"},
                },
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="dsh_read_file",
            description="只读：读取白名单工作区内指定相对路径的文本（受绝对路径/`..` 穿越/敏感文件黑名单保护）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": workspace_prop,
                    "rel_path": {"type": "string",
                                 "description": "工作区内相对路径，如 battle_report.json、candidate.json"},
                    "max_chars": {"type": "integer", "default": 8000,
                                  "description": "最多返回字符数（1–200000）"},
                },
                "required": ["workspace", "rel_path"],
            },
        ),
        # ---- ws_*：网关进程内直接执行（不经过第二层 DSH 子代理）----
        types.Tool(
            name="ws_glob",
            description="只读：在工作区内按相对 glob 列文件（支持 `**`；不含 `/` 的 pattern 按 basename 匹配）。"
                        "跳过 .git/node_modules/__pycache__/runtime、符号链接/联接点与敏感文件。",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": workspace_prop,
                    "pattern": {"type": "string",
                                "description": "工作区内相对 glob，如 **/*.py、docs/*.md、*.json"},
                    "max_results": {"type": "integer", "default": 200,
                                    "description": "最多返回文件数（1–2000）"},
                },
                "required": ["workspace", "pattern"],
            },
        ),
        types.Tool(
            name="ws_grep",
            description="只读：在工作区内按正则逐行检索文本，返回 path/line/text；跳过二进制、超大文件与敏感文件。",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": workspace_prop,
                    "pattern": {"type": "string", "description": "Python 正则（最长 2000 字符）"},
                    "rel_path": {"type": "string", "default": "",
                                 "description": "限定检索的相对文件/目录；空=整个工作区"},
                    "glob_filter": {"type": "string", "default": "",
                                    "description": "可选相对 glob，只检索匹配的文件"},
                    "max_matches": {"type": "integer", "default": 100,
                                    "description": "最多返回匹配行数（1–1000）"},
                    "ignore_case": {"type": "boolean", "default": False,
                                    "description": "是否忽略大小写"},
                },
                "required": ["workspace", "pattern"],
            },
        ),
        types.Tool(
            name="ws_edit_file",
            description="写：把文件里的 old_string 替换为 new_string（原子写入）。replace_all=false 时要求 old_string 恰好出现 1 次，否则拒绝修改。",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": workspace_prop,
                    "rel_path": {"type": "string", "description": "工作区内相对路径"},
                    "old_string": {"type": "string", "description": "要被替换的原文（不能为空）"},
                    "new_string": {"type": "string", "description": "替换后的文本（可为空）"},
                    "replace_all": {"type": "boolean", "default": False,
                                    "description": "true=替换全部出现；false=要求恰好出现 1 次"},
                },
                "required": ["workspace", "rel_path", "old_string", "new_string"],
            },
        ),
        types.Tool(
            name="ws_write_file",
            description="写：UTF-8 原子写入工作区内文件；目标已存在时默认拒绝，需显式 overwrite=true。",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": workspace_prop,
                    "rel_path": {"type": "string", "description": "工作区内相对路径"},
                    "content": {"type": "string", "description": "完整文件内容（UTF-8）"},
                    "overwrite": {"type": "boolean", "default": False,
                                  "description": "目标已存在时是否覆盖"},
                },
                "required": ["workspace", "rel_path", "content"],
            },
        ),
        types.Tool(
            name="ws_run_cmd",
            description="写（受控执行）：在工作区根目录以 shell=False 运行白名单程序 "
                        "（python/py/go/git/npm/npx/rg），返回 rc/stdout/stderr；git 危险子命令与重定向选项会被拒绝。",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": workspace_prop,
                    "argv": {"type": "array", "items": {"type": "string"},
                             "description": "argv 列表（argv[0] 必须是白名单程序名），如 [\"go\", \"test\", \"./...\"]"},
                    "timeout_sec": {"type": "integer", "default": 30,
                                    "description": "超时秒数（1–60），超时会结束整棵进程树"},
                },
                "required": ["workspace", "argv"],
            },
        ),
    ]


server = Server(SERVER_NAME, version=SERVER_VERSION)


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return _tool_schemas()


def _audit_call(name: str, args: dict, res) -> None:
    workspace = args.get("workspace") if isinstance(args.get("workspace"), str) else None
    job_id = args.get("job_id") if isinstance(args.get("job_id"), str) else None
    apply = args.get("apply") if isinstance(args.get("apply"), bool) else None
    status = "error"
    error = None
    if isinstance(res, dict):
        workspace = res.get("workspace") or workspace
        job_id = res.get("job_id") or job_id
        error = res.get("error")
        if res.get("dry_run"):
            status = "dry_run"
        elif not res.get("ok", False):
            status = "error"
        elif name == "dsh_dispatch":
            status = "started"
        elif name == "dsh_cancel":
            status = str(res.get("status") or "cancelled")
        else:
            status = "ok"
    common.audit(name, workspace=workspace, apply=apply, job_id=job_id,
                 status=status, error=error)


@server.call_tool()
async def _call_tool(name: str, arguments: dict | None):
    args = arguments or {}
    handler = _ALL_HANDLERS.get(name)
    if handler is None:
        res: dict = _err("unknown tool %r" % name)
    else:
        try:
            res = await anyio.to_thread.run_sync(lambda: handler(args))
        except Exception as ex:  # noqa: BLE001
            res = _err("%s: %s" % (type(ex).__name__, ex))
    # ws_* 的 handler 已在 ws_tools 内部写过详细审计，这里只给 dsh_* 记录。
    if name not in _WS_HANDLERS:
        _audit_call(name, args, res)
    text = json.dumps(res, ensure_ascii=False, indent=2)
    return [types.TextContent(type="text", text=text)]


# --------------------------------------------------------------------------- #
# HTTP 面：仅 127.0.0.1，/health 免鉴权，/mcp 强制 Bearer                        #
# --------------------------------------------------------------------------- #

class _AuthGate:
    """包在 /mcp 外层的鉴权：OAuth access_token 或本地 DSH_MCP_TOKEN，其余 401。

    401 必须带 RFC 9728 的 resource_metadata，MCP 客户端据此走 OAuth 发现。
    """

    def __init__(self, app, passphrase: str, store: oauth.OAuthStore):
        self.app = app
        self._passphrase = passphrase.encode("utf-8")
        self._store = store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = ""
        for key, value in scope.get("headers", []):
            if key.decode("latin-1").lower() == "authorization":
                raw = value.decode("latin-1")
                if raw.lower().startswith("bearer "):
                    token = raw[7:].strip()
                break

        authorized = False
        if token:
            if hmac.compare_digest(token.encode("utf-8", errors="replace"), self._passphrase):
                authorized = True
            elif self._store.validate_access_token(token):
                authorized = True

        if not authorized:
            origin = oauth.public_origin(Request(scope))
            metadata_url = oauth.resource_metadata_url(origin)
            response = JSONResponse(
                {"error": "unauthorized",
                 "detail": "missing or invalid bearer token; "
                           "OAuth discovery: %s" % metadata_url},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer resource_metadata="%s"' % metadata_url},
            )
            return await response(scope, receive, send)
        await self.app(scope, receive, send)


def build_app(token: str) -> Starlette:
    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=True,
    )
    store = oauth.OAuthStore(oauth.store_path())

    async def health(request):  # noqa: ANN001
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def lifespan(app):  # noqa: ANN001
        async with manager.run():
            yield

    routes = [
        Route(HEALTH_PATH, health),
        Route(MCP_PATH, endpoint=_AuthGate(manager.handle_request, token, store)),
        *oauth.make_routes(store, token),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def create_app() -> Starlette:
    """uvicorn --reload 用的应用工厂（从环境变量读取 DSH_MCP_TOKEN）。"""
    token = (os.environ.get("DSH_MCP_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("DSH_MCP_TOKEN 未设置，拒绝启动")
    return build_app(token)


# --------------------------------------------------------------------------- #
# 启动 / 冒烟                                                                    #
# --------------------------------------------------------------------------- #

def _serve(port: int, reload: bool = False) -> int:
    token = (os.environ.get("DSH_MCP_TOKEN") or "").strip()
    if not token:
        print("[ERR] DSH_MCP_TOKEN 未设置，拒绝启动（防止无鉴权裸奔）。"
              "请先设置环境变量后再启动。", file=sys.stderr)
        return 2
    if len(token) < 16:
        print("[WARN] DSH_MCP_TOKEN 长度 < 16，建议用至少 32 位随机串。", file=sys.stderr)
    dsh = common.find_dsh()
    if not dsh:
        print("[WARN] 未找到 dsh CLI（派活会失败）；可用 DSH_CLI 指定 dsh.cmd 路径。",
              file=sys.stderr)
    if not common.PATCH_FILE.is_file():
        print("[WARN] 缺少 %s；headless 可能因凭据/模型配置无法工作。" % common.PATCH_FILE,
              file=sys.stderr)
    for name, root in common.WORKSPACES.items():
        if not root.is_dir():
            print("[WARN] 白名单工作区 %s 不存在：%s" % (name, root), file=sys.stderr)

    app = build_app(token)
    print("[dsh-mcp] MCP 端点: http://%s:%d%s  （health: %s）"
          % (DEFAULT_HOST, port, MCP_PATH, HEALTH_PATH), file=sys.stderr)
    print("[dsh-mcp] OAuth 发现端点: %s 、 %s"
          % (oauth.RESOURCE_METADATA_PATH, oauth.AS_METADATA_PATH), file=sys.stderr)
    print("[dsh-mcp] OAuth 状态文件: %s" % oauth.store_path(), file=sys.stderr)
    print("[dsh-mcp] 仅监听 %s；外部访问请走 Cloudflare Tunnel。" % DEFAULT_HOST,
          file=sys.stderr)
    if reload:
        here = str(Path(__file__).resolve().parent)
        print("[dsh-mcp] 热重载已开启（监控 %s/*.py）" % here, file=sys.stderr)
        uvicorn.run(
            "server:create_app",
            factory=True,
            host=DEFAULT_HOST,
            port=port,
            log_level="info",
            reload=True,
            reload_dirs=[here],
            reload_includes=["*.py"],
            app_dir=here,
        )
    else:
        uvicorn.run(app, host=DEFAULT_HOST, port=port, log_level="info")
    return 0


def _port_bindable(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((DEFAULT_HOST, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _wait_health(port: int, timeout: float = 30.0) -> bool:
    import httpx
    url = "http://%s:%d%s" % (DEFAULT_HOST, port, HEALTH_PATH)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with httpx.Client(trust_env=False, timeout=2.0) as client:
                resp = client.get(url)
                if resp.status_code == 200 and resp.json().get("ok") is True:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    return False


def _audit_line_count() -> int:
    if not common.AUDIT_PATH.is_file():
        return 0
    with common.AUDIT_PATH.open("rb") as f:
        return sum(1 for _ in f)


def _audit_new_records(before: int) -> list[dict]:
    if not common.AUDIT_PATH.is_file():
        return []
    lines = common.AUDIT_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    out = []
    for line in lines[before:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


async def _mcp_checks(port: int, token: str) -> dict:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = "http://%s:%d%s" % (DEFAULT_HOST, port, MCP_PATH)
    client = httpx.AsyncClient(
        trust_env=False,
        headers={"Authorization": "Bearer " + token},
        timeout=60.0,
    )
    try:
        async with streamable_http_client(url, http_client=client) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                list_res = await session.call_tool("dsh_list_workspaces", {})
                list_text = _first_text(list_res)
                dispatch_res = await session.call_tool(
                    "dsh_dispatch",
                    {"workspace": "slot2", "prompt": "ping", "apply": False},
                )
                dispatch_text = _first_text(dispatch_res)
                ws_glob_res = await session.call_tool(
                    "ws_glob",
                    {"workspace": "slot2", "pattern": "*.md", "max_results": 5},
                )
                ws_glob_text = _first_text(ws_glob_res)
                return {
                    "server_name": init.serverInfo.name if init.serverInfo else None,
                    "tools": names,
                    "list_workspaces": json.loads(list_text) if list_text else None,
                    "dispatch_dry_run": json.loads(dispatch_text) if dispatch_text else None,
                    "ws_glob": json.loads(ws_glob_text) if ws_glob_text else None,
                }
    finally:
        await client.aclose()


def _first_text(result) -> str:
    for block in (result.content or []):
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


async def _mcp_tools_with_token(port: int, bearer: str) -> list[str]:
    """用给定 bearer（OAuth access_token）连一次 MCP，只做 tools/list。"""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = "http://%s:%d%s" % (DEFAULT_HOST, port, MCP_PATH)
    client = httpx.AsyncClient(
        trust_env=False,
        headers={"Authorization": "Bearer " + bearer},
        timeout=60.0,
    )
    try:
        async with streamable_http_client(url, http_client=client) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [t.name for t in tools.tools]
    finally:
        await client.aclose()


def _oauth_flow(client, base: str, passphrase: str, oauth_file: Path):
    """完整走一遍 OAuth：发现 → DCR → PKCE 授权码 → token → refresh。"""
    results: list[tuple[bool, str]] = []
    info: dict = {}

    def rec(cond, good_msg: str, bad_msg: str) -> None:
        results.append((bool(cond), good_msg if cond else bad_msg))

    # 1) 受保护资源元数据（两条路径）
    for path in (oauth.RESOURCE_METADATA_PATH,
                 oauth.RESOURCE_METADATA_PATH + "/mcp"):
        rr = client.get(base + path)
        try:
            data = rr.json()
        except Exception:  # noqa: BLE001
            data = {}
        good = (rr.status_code == 200
                and data.get("resource") == base + "/mcp"
                and data.get("authorization_servers") == [base]
                and data.get("bearer_methods_supported") == ["header"])
        rec(good, "GET %s 元数据正确" % path,
            "GET %s 异常：%s %s" % (path, rr.status_code, rr.text[:300]))

    # 2) 授权服务器元数据
    rr = client.get(base + oauth.AS_METADATA_PATH)
    try:
        meta = rr.json()
    except Exception:  # noqa: BLE001
        meta = {}
    good = (
        rr.status_code == 200
        and meta.get("issuer") == base
        and meta.get("authorization_endpoint") == base + "/oauth/authorize"
        and meta.get("token_endpoint") == base + "/oauth/token"
        and meta.get("registration_endpoint") == base + "/oauth/register"
        and meta.get("response_types_supported") == ["code"]
        and meta.get("grant_types_supported") == ["authorization_code", "refresh_token"]
        and meta.get("code_challenge_methods_supported") == ["S256"]
        and set(meta.get("token_endpoint_auth_methods_supported") or [])
            >= {"none", "client_secret_post", "client_secret_basic"}
    )
    rec(good, "GET %s 授权服务器元数据正确" % oauth.AS_METADATA_PATH,
        "授权服务器元数据异常：%s %s"
        % (rr.status_code, json.dumps(meta, ensure_ascii=False)[:400]))
    if not good:
        return results, info

    # 3) DCR
    callback = "http://127.0.0.1:8765/callback"
    rr = client.post(base + "/oauth/register", json={
        "client_name": "dsh-mcp-check",
        "redirect_uris": [callback],
        "token_endpoint_auth_method": "none",
    })
    try:
        reg = rr.json()
    except Exception:  # noqa: BLE001
        reg = {}
    client_id = reg.get("client_id")
    rec(rr.status_code == 201 and bool(client_id),
        "POST /oauth/register -> 201 Created（client_id=%s）" % client_id,
        "DCR 失败：%s %s" % (rr.status_code, rr.text[:300]))
    if not client_id:
        return results, info
    info["client_id"] = client_id

    # 4) PKCE + 授权页
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("utf-8")).digest()
    ).rstrip(b"=").decode("ascii")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": callback,
        "state": "check-state",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "mcp",
    }
    page = client.get(base + "/oauth/authorize", params=params)
    rec(page.status_code == 200 and "允许授权" in page.text and "dsh-mcp-check" in page.text,
        "GET /oauth/authorize -> 200 授权确认页",
        "授权页异常：%s %s" % (page.status_code, page.text[:300]))

    # 5) 错误口令必须被拒
    wrong = client.post(base + "/oauth/authorize", data={**params, "passphrase": "wrong-passphrase"})
    rec(wrong.status_code == 401,
        "错误授权口令 -> 401（被拒绝）",
        "错误口令未被拒绝，status=%s" % wrong.status_code)

    # 6) 正确口令 -> 302 + code
    good = client.post(base + "/oauth/authorize", data={**params, "passphrase": passphrase})
    location = good.headers.get("location", "")
    parts = urllib.parse.urlsplit(location)
    query = dict(urllib.parse.parse_qsl(parts.query))
    code = query.get("code")
    rec(good.status_code == 302 and parts.netloc == "127.0.0.1:8765"
        and query.get("state") == "check-state" and bool(code),
        "POST /oauth/authorize -> 302 回跳，携带 code + state",
        "授权回跳异常：status=%s location=%r" % (good.status_code, location[:300]))
    if not code:
        return results, info

    # 7) 授权码 + PKCE 换 token
    def _exchange():
        return client.post(base + "/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": callback,
            "client_id": client_id,
            "code_verifier": verifier,
        })

    tok = _exchange()
    try:
        tdata = tok.json()
    except Exception:  # noqa: BLE001
        tdata = {}
    access = tdata.get("access_token")
    refresh = tdata.get("refresh_token")
    rec(tok.status_code == 200 and bool(access) and bool(refresh)
        and tdata.get("token_type") == "Bearer",
        "POST /oauth/token（code + PKCE S256）-> access_token + refresh_token",
        "token 交换失败：%s %s" % (tok.status_code, tok.text[:400]))
    if not access:
        return results, info

    # 8) 授权码一次性
    replay = _exchange()
    rec(replay.status_code == 400,
        "授权码重复使用被拒（400 invalid_grant）",
        "授权码被重复使用，status=%s" % replay.status_code)

    # 9) refresh_token 轮换
    ref = client.post(base + "/oauth/token", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client_id,
    })
    try:
        rdata = ref.json()
    except Exception:  # noqa: BLE001
        rdata = {}
    access2 = rdata.get("access_token")
    rec(ref.status_code == 200 and bool(access2)
        and rdata.get("refresh_token") not in (None, refresh),
        "POST /oauth/token（refresh_token）-> 新 access_token（刷新令牌轮换）",
        "refresh 失败：%s %s" % (ref.status_code, ref.text[:400]))
    info["access_token"] = access2 or access

    # 10) 无效 OAuth token -> 401 + 发现头
    bogus = client.post(base + MCP_PATH, json={},
                        headers={"Authorization": "Bearer bad-" + os.urandom(8).hex()})
    wa = bogus.headers.get("www-authenticate", "")
    rec(bogus.status_code == 401 and "resource_metadata=" in wa,
        "无效 OAuth Token POST /mcp -> 401 且带 resource_metadata",
        "无效 OAuth Token 的 401 头异常：%s %r" % (bogus.status_code, wa))

    # 11) 状态落盘
    rec(oauth_file.is_file(),
        "OAuth 状态已落盘：%s" % oauth_file,
        "OAuth 状态文件缺失：%s" % oauth_file)
    return results, info



def _check(port: int) -> int:
    print("=== dsh_mcp 冒烟自检（%s:%d）===" % (DEFAULT_HOST, port))
    problems: list[str] = []
    checks: list[str] = []

    def ok(msg: str) -> None:
        checks.append(msg)
        print("[ok] " + msg)

    def bad(msg: str) -> None:
        problems.append(msg)
        print("[ERR] " + msg)

    # 1) 依赖探测
    try:
        import mcp  # noqa: F401
        import starlette  # noqa: F401
        import uvicorn  # noqa: F401
        import httpx  # noqa: F401
        ok("依赖可用：mcp / starlette / uvicorn / httpx")
    except Exception as ex:  # noqa: BLE001
        bad("依赖缺失：%s（pip install mcp uvicorn httpx）" % ex)
        return 2
    dsh = common.find_dsh()
    if dsh:
        ok("dsh CLI：%s" % dsh)
    else:
        bad("找不到 dsh CLI（可用 DSH_CLI 指定）")
    if common.PATCH_FILE.is_file():
        ok("headless 凭据 patch：%s" % common.PATCH_FILE)
    else:
        bad("缺少 headless 凭据 patch：%s" % common.PATCH_FILE)
    if problems:
        return 2

    # 2) 红线段：空 Token 必须拒绝启动（用 8796，避免占用正式端口）
    empty_env = common.child_env()
    empty_env.pop("DSH_MCP_TOKEN", None)
    server_py = str(Path(__file__).resolve())
    try:
        p = subprocess.run(
            [sys.executable, server_py, "--port", "8796"],
            cwd=str(common.PROJECT), env=empty_env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, stdin=subprocess.DEVNULL,
            creationflags=common.CREATE_NO_WINDOW,
        )
        if p.returncode == 0:
            bad("空 DSH_MCP_TOKEN 仍然启动成功（违反安全红线）")
        else:
            ok("空 Token 拒绝启动（rc=%d）" % p.returncode)
    except Exception as ex:  # noqa: BLE001
        bad("空 Token 启动探测失败：%s" % ex)

    # 3) 端口可用性
    if not _port_bindable(port):
        bad("端口 %d 已被占用（先停掉已有 dsh_mcp 实例再自检）" % port)
        return 2

    # 4) 启动服务并等 /health
    token = "check-" + os.urandom(16).hex()
    env = common.child_env()
    env["DSH_MCP_TOKEN"] = token
    oauth_file = common.RUNTIME_DIR / "dsh_mcp_oauth_check.json"
    try:
        oauth_file.unlink()
    except FileNotFoundError:
        pass
    env["DSH_MCP_OAUTH_FILE"] = str(oauth_file)
    server_log = common.RUNTIME_DIR / "dsh_mcp_check_server.log"
    server_log.parent.mkdir(parents=True, exist_ok=True)
    proc = None
    audit_before = _audit_line_count()
    try:
        with server_log.open("wb") as logf:
            proc = subprocess.Popen(
                [sys.executable, server_py, "--port", str(port)],
                cwd=str(common.PROJECT), env=env,
                stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                creationflags=common.CREATE_NO_WINDOW,
            )
        if _wait_health(port):
            ok("服务已启动，GET /health -> 200 {\"ok\": true}")
        else:
            bad("服务 30s 内未就绪；日志尾部：\n" + "\n".join(
                common.tail_lines(server_log, 30)))

        if not problems:
            import httpx
            base = "http://%s:%d" % (DEFAULT_HOST, port)
            oauth_access = None
            with httpx.Client(trust_env=False, timeout=20.0) as client:
                # 5) /health 免鉴权
                r = client.get(base + HEALTH_PATH)
                if r.status_code == 200 and r.json() == {"ok": True}:
                    ok("GET /health 无需 Token，返回 {\"ok\": true}")
                else:
                    bad("GET /health 异常：%s %s" % (r.status_code, r.text[:120]))

                # 6) 无 Token /mcp -> 401 + WWW-Authenticate(resource_metadata)
                r = client.post(base + MCP_PATH, json={})
                expected_meta = base + oauth.RESOURCE_METADATA_PATH
                wa = r.headers.get("www-authenticate", "")
                if r.status_code == 401 and "resource_metadata=" in wa and expected_meta in wa:
                    ok("无 Token POST /mcp -> 401，WWW-Authenticate 指向 %s" % expected_meta)
                else:
                    bad("无 Token 401/WWW-Authenticate 异常：status=%s header=%r"
                        % (r.status_code, wa))

                # 7) OAuth 全流程（发现 → DCR → PKCE → token → refresh）
                flow_results, flow_info = _oauth_flow(client, base, token, oauth_file)
                for good, message in flow_results:
                    (ok if good else bad)(message)
                oauth_access = flow_info.get("access_token")

            # 8) OAuth access_token 访问 /mcp tools/list
            if oauth_access:
                try:
                    oauth_tools = anyio.run(_mcp_tools_with_token, port, oauth_access)
                    if set(_ALL_HANDLERS) <= set(oauth_tools):
                        ok("OAuth access_token 连接 /mcp 成功，tools/list 返回 %d 个工具"
                           % len(oauth_tools))
                    else:
                        bad("OAuth access_token tools/list 缺工具：%s"
                            % ", ".join(sorted(set(_ALL_HANDLERS) - set(oauth_tools))))
                except Exception as ex:  # noqa: BLE001
                    bad("OAuth access_token 连接 /mcp 失败：%s: %s" % (type(ex).__name__, ex))
            else:
                bad("OAuth 流程未产出 access_token，无法验证 OAuth 鉴权")

            # 9) 原 DSH_MCP_TOKEN 兼容（本地脚本/冒烟）
            jobs_before = {d.name for d in common.iter_job_dirs(limit=1000)}
            result = anyio.run(_mcp_checks, port, token)
            expected = set(_ALL_HANDLERS)
            got = set(result.get("tools") or [])
            if expected <= got:
                ok("原 DSH_MCP_TOKEN 兼容：tools/list 返回 %d 个工具：%s"
                   % (len(got), ", ".join(sorted(got))))
            else:
                bad("tools/list 缺工具：%s" % ", ".join(sorted(expected - got)))

            lw = result.get("list_workspaces") or {}
            names = [w.get("workspace") for w in (lw.get("workspaces") or [])]
            if lw.get("ok") and set(names) == set(common.WORKSPACES):
                ok("dsh_list_workspaces 返回白名单：%s" % ", ".join(names))
            else:
                bad("dsh_list_workspaces 结果异常：%s" % json.dumps(lw, ensure_ascii=False)[:400])

            dr = result.get("dispatch_dry_run") or {}
            if dr.get("ok") and dr.get("dry_run") and dr.get("workspace") == "slot2":
                ok("dsh_dispatch(workspace=slot2, prompt=ping, apply=false) dry-run 正常"
                   "（未启动进程）")
            else:
                bad("dsh_dispatch dry-run 结果异常：%s"
                    % json.dumps(dr, ensure_ascii=False)[:400])

            wg = result.get("ws_glob") or {}
            if (wg.get("ok") and wg.get("workspace") == "slot2"
                    and isinstance(wg.get("files"), list)):
                ok("ws_glob(workspace=slot2, pattern=*.md, max_results=5) 只读冒烟正常"
                   "（%d 个文件）" % len(wg["files"]))
            else:
                bad("ws_glob 只读冒烟异常：%s" % json.dumps(wg, ensure_ascii=False)[:400])
            jobs_after = {d.name for d in common.iter_job_dirs(limit=1000)}
            if jobs_after == jobs_before:
                ok("dry-run 未创建任何 job 目录")
            else:
                bad("dry-run 意外创建了 job：%s" % ", ".join(sorted(jobs_after - jobs_before)))

            # 10) 审计落盘
            new_recs = _audit_new_records(audit_before)
            has_list = any(r.get("tool") == "dsh_list_workspaces" for r in new_recs)
            has_dry = any(r.get("tool") == "dsh_dispatch" and r.get("status") == "dry_run"
                          for r in new_recs)
            if has_list and has_dry:
                ok("审计落盘：%s（新增 %d 条）" % (common.AUDIT_PATH, len(new_recs)))
            else:
                bad("审计记录缺失（list=%s, dry_run=%s）" % (has_list, has_dry))

            ws_recs = [r for r in new_recs if r.get("tool") == "ws_glob"]
            if len(ws_recs) == 1:
                ok("ws_* 自审计：ws_glob 只记 1 条（server._audit_call 未重复记录）")
            else:
                bad("ws_glob 审计条数异常：%d（应为 1；0=缺失，>1=server 重复记录）"
                    % len(ws_recs))
    finally:
        if proc is not None:
            common.kill_tree(proc.pid)
            time.sleep(0.3)

    print()
    if problems:
        print("=== 冒烟失败：%d 项 ===" % len(problems))
        for item in problems:
            print("  - " + item)
        return 2
    print("=== 冒烟全部通过：%d 项 ===" % len(checks))
    return 0


# --------------------------------------------------------------------------- #
# CLI                                                                           #
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dsh_mcp/server.py",
        description="DSH 远程 MCP 网关（Streamable HTTP + 内置 OAuth 2.1）。"
                    "无参数启动服务；--check 跑本地冒烟。",
        epilog="工具（11 个）：dsh_list_workspaces / dsh_dispatch / dsh_status / "
               "dsh_result / dsh_cancel / dsh_read_file / ws_glob / ws_grep / "
               "ws_edit_file / ws_write_file / ws_run_cmd。写动作默认 dry-run。",
    )
    parser.add_argument("--check", action="store_true",
                        help="冒烟自检：空 Token 拒启、401+发现头、OAuth 全流程、tools/list、ws_glob、审计落盘")
    parser.add_argument("--list", action="store_true", help="打印 11 个 MCP 工具名后退出")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("DSH_MCP_PORT", DEFAULT_PORT)),
                        help="监听端口（默认 8795；地址固定 127.0.0.1）")
    parser.add_argument("--reload", action="store_true",
                        help="监控 tools/dsh_mcp/*.py 自动热重载（配合 stateless=True 不中断对话）")
    args = parser.parse_args(argv)

    if args.list:
        for spec in _tool_schemas():
            print("%-24s %s" % (spec.name, spec.description))
        return 0
    if args.check:
        return _check(args.port)
    try:
        return _serve(args.port, reload=args.reload)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
