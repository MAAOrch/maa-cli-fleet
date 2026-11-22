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
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

# 仓库根 = 本文件往上三层（tools/picker_push/pi_runner.py）。绝不写死主目录：
# 双开时主仓（D:\maa-cli-fleet）和 worktree（D:\maa-cli-fleet-b）各跑各的 runtime，
# 写死主目录会让 B 会话把 PI 日志/配置写进主仓的 runtime。
ROOT = Path(__file__).resolve().parents[2]
PI_PROJECT = ROOT / "pi" / "arknights-event"
RUNTIME = Path(os.environ.get("PICKER_PI_RUNTIME") or (ROOT / "runtime" / "maa_cli_event"))
CONFIG = RUNTIME / "config" / "maa_pi_config.json"

# 设备默认值只从环境变量取（不写死实例 0 的 16384/1-MuMuPlayer v5+）：
#   PICKER_ADB_NAME      MaaToolkit 探测名，如 "2-MuMuPlayer v5+"
#   PICKER_ADB_ADDRESS   adb 地址，如 "127.0.0.1:16416"
# 没设时保留 config/maa_pi_config.json 里已有值（setup_pi.ps1 生成的那份）。
DEFAULT_CONFIG = {
    "controller": {"name": "Android"},
    "adb": {
        "name": os.environ.get("PICKER_ADB_NAME", ""),
        "adb_path": "D:/Xiach/MuMuPlayer/nx_main/adb.exe",
        "address": os.environ.get("PICKER_ADB_ADDRESS", ""),
    },
    "resource": "Arknights",
    "task": [],
}

# set_device 由驱动（push_event.py）在解析出目标实例后调用，覆盖 DEFAULT_CONFIG。
_DEVICE = {"name": "", "address": ""}


def set_device(address: str | None = None, name: str | None = None) -> None:
    """显式指定 PI 要连的模拟器（双开时区分实例 0/1 的唯一可靠入口）。"""
    if address:
        _DEVICE["address"] = address
    if name:
        _DEVICE["name"] = name


def runtime_dir() -> Path:
    return RUNTIME


def _write_config(task_name: str) -> None:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝，别把 DEFAULT_CONFIG 里的 adb 字典改脏
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    # 显式设备优先于文件里的旧值（双开：setup 时是实例 A，之后切实例 B 不重跑 setup 也能生效）
    adb = dict(cfg.get("adb") or {})
    for key in ("name", "address"):
        if _DEVICE.get(key):
            adb[key] = _DEVICE[key]
    cfg["adb"] = adb
    if not adb.get("name") or not adb.get("address"):
        raise RuntimeError(
            "PI 设备地址未配置：先跑 setup_pi.ps1，或设 PICKER_ADB_NAME/PICKER_ADB_ADDRESS，"
            "或调用 pi_runner.set_device(address, name)"
        )
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
