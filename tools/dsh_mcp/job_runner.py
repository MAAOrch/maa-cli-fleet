# -*- coding: utf-8 -*-
r"""单个 dsh 任务的监督进程（由 server.py 在后台拉起）。

职责：
  1. 按 job meta 组装 `dsh --profile headless ...` 命令，cwd 锁定白名单工作区；
  2. prompt 通过 stdin 传入（避免命令行转义/注入），stdout+stderr 实时写 output.log；
  3. 执行 timeout_sec 超时兜底：超时结束整棵进程树，写 exit.json(status=timeout)；
  4. 解析 --json 输出里的 final 事件，写 exit.json（状态/退出码/session/final 文本）。

进程树被 dsh_cancel 杀死时本进程也会消失，此时不会写 exit.json，由 server 标 cancelled。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def _fail(job_dir: Path, started_ts: float, error: str) -> int:
    common.write_json(job_dir / "exit.json", {
        "status": "failed",
        "exit_code": None,
        "ended_at": common.now_iso(),
        "duration_sec": round(time.time() - started_ts, 1),
        "error": error,
    })
    print("[job_runner] %s" % error, file=sys.stderr, flush=True)
    return 1


def _build_argv(meta: dict) -> list[str]:
    dsh = common.find_dsh()
    if not dsh:
        raise RuntimeError("找不到 dsh CLI（可用 DSH_CLI 环境变量指定 dsh.cmd 路径）")
    perm = meta.get("permission_mode") or os.environ.get("DSH_MCP_PERMISSION_MODE") or "workspace-write"
    if perm not in ("read-only", "workspace-write", "danger-full-access"):
        raise RuntimeError("非法 permission_mode：%r" % perm)
    argv = [dsh, "--profile", "headless"]
    if common.PATCH_FILE.is_file():
        argv += ["--patch", str(common.PATCH_FILE)]
    argv += [
        "--permission-mode", perm,
        "--approval-policy", "never",
        "--json",
        "-",  # 任务文本从 stdin 读
    ]
    return argv


def _parse_output(path: Path) -> dict:
    return common.parse_ndjson_output(path)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: job_runner.py <job_dir>", file=sys.stderr)
        return 2
    job_dir = Path(argv[1]).resolve()
    started_ts = time.time()
    meta = common.read_meta(job_dir)
    if not meta:
        return _fail(job_dir, started_ts, "meta.json 缺失或不可读")

    workspace = meta.get("workspace")
    prompt = meta.get("prompt")
    timeout_sec = meta.get("timeout_sec") or 600
    try:
        workspace_root = common.resolve_workspace(workspace)
        timeout_sec = float(timeout_sec)
    except Exception as ex:  # noqa: BLE001
        return _fail(job_dir, started_ts, "非法 job 参数：%s" % ex)
    if not isinstance(prompt, str) or not prompt.strip():
        return _fail(job_dir, started_ts, "prompt 为空")
    if not workspace_root.is_dir():
        return _fail(job_dir, started_ts, "工作区目录不存在：%s" % workspace_root)

    try:
        dsh_argv = _build_argv(meta)
    except Exception as ex:  # noqa: BLE001
        return _fail(job_dir, started_ts, str(ex))

    common.write_json(job_dir / "runner.json", {
        "runner_pid": os.getpid(),
        "started_ts": started_ts,
        "started_at": common.now_iso(),
        "cwd": str(workspace_root),
        "command": dsh_argv,
        "timeout_sec": timeout_sec,
    })

    output_path = job_dir / "output.log"
    exit_code = None
    timed_out = False
    spawn_error = None
    proc = None

    try:
        with output_path.open("ab") as out:
            proc = subprocess.Popen(
                dsh_argv,
                cwd=str(workspace_root),
                env=common.child_env(),
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=subprocess.STDOUT,
                creationflags=common.CREATE_NO_WINDOW,
            )
    except Exception as ex:  # noqa: BLE001
        spawn_error = "拉起 dsh 失败：%s: %s" % (type(ex).__name__, ex)

    if proc is None:
        return _fail(job_dir, started_ts, spawn_error or "拉起 dsh 失败")

    common.write_json(job_dir / "runner.json", {
        "runner_pid": os.getpid(),
        "dsh_pid": proc.pid,
        "started_ts": started_ts,
        "started_at": common.now_iso(),
        "cwd": str(workspace_root),
        "command": dsh_argv,
        "timeout_sec": timeout_sec,
    })

    # prompt 走 stdin，写完即关；子进程用 `-` 读取到 EOF。
    try:
        if proc.stdin is not None:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
    except Exception:  # noqa: BLE001 —— 子进程提前退出时可能 EPIPE
        pass

    try:
        exit_code = proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        common.kill_tree(proc.pid)
        try:
            exit_code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            exit_code = None

    duration = round(time.time() - started_ts, 1)
    info = _parse_output(output_path)
    reason = info.get("reason") or {}
    reason_kind = reason.get("kind") if isinstance(reason, dict) else None

    if timed_out:
        status = "timeout"
    elif exit_code != 0:
        status = "failed"
    elif reason_kind == "error":
        status = "failed"
    else:
        status = "succeeded"

    exit_rec = {
        "status": status,
        "exit_code": exit_code,
        "ended_at": common.now_iso(),
        "duration_sec": duration,
        "timed_out": timed_out,
        "session_id": info.get("session_id"),
        "final_text": info.get("final_text"),
        "reason": info.get("reason"),
        "usage": info.get("usage"),
        "events": info.get("events", 0),
    }
    if timed_out:
        exit_rec["error"] = "超过 timeout_sec=%ss，已结束进程树" % timeout_sec
    common.write_json(job_dir / "exit.json", exit_rec)

    final_text = info.get("final_text")
    if isinstance(final_text, str) and final_text:
        try:
            (job_dir / "final.txt").write_text(final_text, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    print("[job_runner] job=%s status=%s rc=%s elapsed=%ss" % (
        job_dir.name, status, exit_code, duration), file=sys.stderr, flush=True)
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
