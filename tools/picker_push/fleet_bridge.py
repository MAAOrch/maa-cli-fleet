# -*- coding: utf-8 -*-
"""把 PI 导航任务写入 fleet 的 queue.in，让 fleet 调度执行。

用法：
    from fleet_bridge import enqueue_pi
    enqueue_pi("arknights-event", "进入S-1详情", "a01")
"""

from __future__ import annotations

import json
from pathlib import Path


def enqueue_pi(project: str, entry: str, account: str, state_dir: str | Path | None = None) -> Path:
    """写一条 PI 任务到 queue.in。

    返回 queue.in 路径。调用方需自己确保 fleet runner 正在跑，且 state_dir 指向正确的台账目录。
    """
    if state_dir is None:
        # 默认写到工作区根的 runtime 或当前目录（调用方应显式传入）
        state_dir = Path.cwd()
    else:
        state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    q = state_dir / "queue.in"
    kind = f"pi.{project}.{entry}"
    line = json.dumps({"op": "enqueue", "kind": kind, "account": account}, ensure_ascii=False)
    with q.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return q
