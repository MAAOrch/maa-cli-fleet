# -*- coding: utf-8 -*-
"""调用 MaaFramework PI 任务（pi/arknights-event）做活动图导航。

为什么不自己 adb 点击：用户 2026-11-22 明确要求「UI/菜单/任务流走 MaaFw PI」，
而且 PI 的识别/点击/超时/重试都是框架在管，比脚本盲点稳。

用法（Python API）：
    from pi_runner import run_task
    ok, log = run_task("进入EX图")          # 从任意界面导航到 EX 图
    ok, log = run_task("收结算页")          # 收结算页并回主界面

前提：先跑 `pwsh -File pi/arknights-event/setup_pi.ps1` 生成 runtime/maa_cli_event。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(r"D:\maa-cli-fleet")
PI_PROJECT = ROOT / "pi" / "arknights-event"
RUNTIME = ROOT / "runtime" / "maa_cli_event"
CONFIG = RUNTIME / "config" / "maa_pi_config.json"

DEFAULT_CONFIG = {
    "controller": {"name": "Android"},
    "adb": {"name": "1-MuMuPlayer v5+", "adb_path": "D:/Xiach/MuMuPlayer/nx_main/adb.exe", "address": "127.0.0.1:16384"},
    "resource": "Arknights",
    "task": [],
}


def _write_config(task_name: str) -> None:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    cfg["task"] = [{"name": task_name, "option": []}]
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sync_interface() -> None:
    """MaaPiCli 读的是运行根里的 interface.json；每次跑前从 PI 项目同步一次，
    免得改完节点/任务忘了重新 setup。resource 是 junction，不用同步。"""
    src = PI_PROJECT / "interface.json"
    dst = RUNTIME / "interface.json"
    if src.exists():
        dst.write_bytes(src.read_bytes())


_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\]")


def _parse_ts(line: str):
    m = _TS_RE.search(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None


def _judge_from_log(since: datetime) -> bool | None:
    """只认**本次启动之后**写下的 `Tasker.Task.Succeeded/Failed` 行。

    背景：MaaPiCli 即使任务超时失败也会打印「All tasks have been completed」，所以不能
    信 stdout（2026-11-22 假成功事故）。同时它是每次启动轮转日志——正常情况下当前
    maafw.log 就是本次日志；但**进程若在写出 Tasker 行之前就闪退**，当前文件可能还是上一轮
    的日志，直接取最后一条会误读到上一轮的 Succeeded。因此按行首时间戳过滤：
    `since` = 启动时刻 - 2s，只统计此后的行；一行都没命中就返回 None（调用方判失败）。
    """
    p = RUNTIME / "debug" / "maafw.log"
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    last = None
    for line in text.splitlines():
        if "Tasker.Task." not in line:
            continue
        ts = _parse_ts(line)
        if ts is not None and ts < since:
            continue
        if "[msg=Tasker.Task.Succeeded]" in line:
            last = True
        elif "[msg=Tasker.Task.Failed]" in line:
            last = False
    return last


def run_task(task_name: str, timeout: int = 300, log=print) -> tuple[bool, str]:
    """跑一个 PI 任务，返回 (是否成功, 输出)。失败不抛异常，交给调用方决定重试/回退。

    成功判定只看 `debug/maafw.log` 里本次启动后的 Tasker.Task.Succeeded/Failed：
    **删掉了「stdout 打印 completed 就算成功」的 fallback**（那正是假成功来源）。
    """
    if not (RUNTIME / "MaaPiCli.exe").exists():
        raise RuntimeError(f"找不到 {RUNTIME / 'MaaPiCli.exe'}，先跑 pi/arknights-event/setup_pi.ps1")
    _sync_interface()
    _write_config(task_name)
    log(f"    [pi] 任务「{task_name}」…")
    t0 = time.time()
    since = datetime.now() - timedelta(seconds=2)
    try:
        p = subprocess.run(
            [str(RUNTIME / "MaaPiCli.exe"), "-d"],
            cwd=str(RUNTIME), capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"PI 任务超时（{timeout}s）"
    took = time.time() - t0
    out = p.stdout.decode("gbk", errors="replace") + p.stderr.decode("gbk", errors="replace")
    judged = _judge_from_log(since)
    ok = judged is True
    tail = out.strip().splitlines()[-4:]
    log(f"    [pi] {'成功' if ok else '失败'} {took:.1f}s rc={p.returncode} "
        f"log={'成功' if judged else '失败' if judged is False else '无本次记录'} " + " | ".join(tail))
    return ok, out


if __name__ == "__main__":
    import sys

    name = sys.argv[1] if len(sys.argv) > 1 else "收结算页"
    ok, _ = run_task(name)
    raise SystemExit(0 if ok else 1)
