# -*- coding: utf-8 -*-
"""活动图「手动模式」推关驱动：定位关卡详情页 → 起 copilot-picker → 逐份作业重试。

为什么需要它：copilot-picker 的作业集模式能在**主章节列表里能查到的关**（活动 S 关等）
自动导航，但活动 EX 图不在章节列表里（MAA 会一路
`Copilot@ChapterSwipeToTheRightAndPlot` 超限失败）。所以 EX 关必须「人先把游戏停在
关卡详情页」走手动模式——本脚本就是那个人手：按 stage_map.json 里的坐标点关卡、
切突袭，然后循环换作业。

判定完全用 picker 服务端的结论（`/api/run/<id>` 的 status）：服务端已经按
「MAA FightMissionFailed + 结算页三星徽章」判真假成功（2026-11-21 修），
这里不再自己截图数星。

用法：
  python tools/picker_push/push_stage.py TL-EX-4 --raid --jobs 109802,109584,109634
  python tools/picker_push/push_stage.py TL-S-2 --jobs 110188,110085 --map-json <path>
  python tools/picker_push/push_stage.py TL-EX-4 --raid --jobs 109802 --dry-run

退出码：0 = 通关 / 1 = 全部作业失败 / 2 = 参数或环境问题（adb 不通、没坐标）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_MAP = Path(__file__).with_name("stage_map.json")


def load_map(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def api_get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.load(r)


def api_post(base: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


class Device:
    def __init__(self, adb: str, serial: str) -> None:
        self.adb = adb
        self.serial = serial

    def _run(self, *args: str) -> None:
        subprocess.run(
            [self.adb, "-s", self.serial, *args],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def tap(self, xy) -> None:
        self._run("shell", "input", "tap", str(xy[0]), str(xy[1]))

    def shot(self, path: Path) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            r = subprocess.run(
                [self.adb, "-s", self.serial, "exec-out", "screencap", "-p"],
                stdout=f,
                stderr=subprocess.DEVNULL,
            )
        return r.returncode == 0 and path.stat().st_size > 10_000  # 全黑帧很小

    def alive(self) -> bool:
        """设备可不可用。adb 有时会抖（刚重启 adb server / 游戏正忙），重试几次。"""
        for attempt in range(3):
            r = subprocess.run(
                [self.adb, "-s", self.serial, "shell", "true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if r.returncode == 0:
                return True
            time.sleep(2)
        return False


def start_run(cfg: dict, stage: str, stage_name: str, job: int, raid: bool) -> str:
    body = {
        "accountId": cfg["account"],
        "client": cfg["client"],
        "stages": [
            {
                "stageCode": stage,
                "stageName": stage_name,
                "jobIds": [job],
                "raidJobIds": [job] if raid else [],
            }
        ],
        "manualMode": True,
        "formation": True,
        "addTrust": True,
        "ignoreRequirements": True,
        "maxAttempts": 1,
        "supportUnitUsage": 0,
        "timeoutMin": 20,
    }
    return api_post(cfg["api"], "/api/run/start", body)["runId"]


def wait_done(base: str, run_id: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(4)
        snap = api_get(base, f"/api/run/{run_id}")
        if snap.get("finishedAt"):
            return snap
    return {"status": "timeout", "error": "轮询超时"}


def goto_map(dev: Device, cfg: dict, map_key: str, ui: dict) -> None:
    """从任意页面回到活动地图。

    用 MAA 的「主界面」捷径（左上角图标 → 弹层里的「首页」）而不是按返回键：
    返回键从「关卡详情页」按两下只到活动 hub，不会到首页，之后点终端会点歪。
    """
    for _ in range(3):
        dev.tap(ui["center"])
        time.sleep(2.2)
    dev.tap(ui["home_icon"])
    time.sleep(1.4)
    dev.tap(ui["home_entry"])
    time.sleep(1.6)
    dev.tap(ui["terminal"])
    time.sleep(2.0)
    dev.tap(ui["event_card"])
    time.sleep(2.8)
    dev.tap(cfg["maps"][map_key]["card"])
    time.sleep(2.5)


def looks_like_detail(path: Path) -> bool | None:
    """粗判「是不是关卡详情页」：右下角『开始行动』是亮蓝色大按钮。

    坐标会随解锁进度/皮肤变化，所以定位之后必须验一下，别拿错页面去起 copilot
    （起错的典型报错：SubTaskError first=BattleStartPre / BattleStartAll）。
    没有 PIL 时返回 None（跳过校验）。
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(path).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    w, h = im.size
    sx, sy = w / 1280.0, h / 720.0
    px = im.load()
    blue = 0
    for y in range(int(615 * sy), int(700 * sy), 2):
        for x in range(int(1055 * sx), int(1215 * sx), 2):
            r, g, b = px[x, y]
            if b > 150 and b - r > 60 and g > 120:
                blue += 1
    return blue >= 200


def reposition(dev: Device, ui: dict, node, raid: bool, shot_dir: Path) -> bool:
    """从地图点关卡 →（突袭）切到突袭 → 确认详情页。返回 False 表示没定位到。"""
    dev.tap(node)
    time.sleep(2.0)
    if raid:
        dev.tap(ui["raid_button"])
        time.sleep(1.5)
    probe = shot_dir / "preflight_driver.png"
    for attempt in range(2):
        if not dev.shot(probe):
            time.sleep(2.0)
            continue
        ok = looks_like_detail(probe)
        if ok is None or ok:
            return True
        # 可能点到空处/别处：退一级再点一次
        dev.tap(ui["back"])
        time.sleep(1.5)
        dev.tap(node)
        time.sleep(2.0)
        if raid:
            dev.tap(ui["raid_button"])
            time.sleep(1.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage")
    ap.add_argument("--jobs", required=True, help="逗号分隔的作业 ID，按顺序试")
    ap.add_argument("--raid", action="store_true", help="切突袭模式再开抄")
    ap.add_argument("--map-json", type=Path, default=DEFAULT_MAP)
    ap.add_argument("--timeout", type=int, default=600, help="单份作业最长等待秒数")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ready", action="store_true",
                    help="人已经把游戏停在关卡详情页：只校验页面、不做导航")
    ap.add_argument("--on-map", action="store_true", help="（旧参数，等价于 --ready 之外仍走导航）")
    args = ap.parse_args()

    cfg = load_map(args.map_json)
    st = cfg["stages"].get(args.stage)
    if not st:
        print(f"[err] stage_map.json 里没有 {args.stage} 的坐标，先补一条", file=sys.stderr)
        return 2
    jobs = [int(x) for x in args.jobs.split(",") if x.strip()]
    dev = Device(cfg["adb"], cfg["device"])
    if not dev.alive():
        print("[err] adb 连不上设备，先起模拟器", file=sys.stderr)
        return 2

    print(f"stage={args.stage} ({st['stageName']}) raid={args.raid} jobs={jobs}")
    if args.dry_run:
        for j in jobs:
            print(f"  would try #{j}")
        return 0

    shot_dir = Path("_tmp/pk")
    shot_dir.mkdir(parents=True, exist_ok=True)
    nav = None
    if not args.ready:
        from nav import NavError, Navigator  # 同目录：状态识别 + 逐跳校验导航

        nav = Navigator(cfg, dev, shot_dir / "driver_shot.png")

    for idx, job in enumerate(jobs, 1):
        if nav is not None:
            try:
                path = nav.goto_detail(st["map"], st["node"], args.raid)
                print(f"    定位完成：{path}")
            except NavError as e:
                print(f"[err] 导航没走到 {args.stage} 详情页：{e}", file=sys.stderr)
                return 2
        else:
            # --ready：相信人已经把游戏停在关卡详情页，只做一次校验
            probe = shot_dir / "driver_shot.png"
            if not dev.shot(probe):
                print("[err] 截图失败，无法确认页面", file=sys.stderr)
                return 2
            ok = looks_like_detail(probe)
            if ok is False:
                print("[err] 当前不在关卡详情页（--ready 模式不做导航）", file=sys.stderr)
                return 2
        try:
            run_id = start_run(cfg, args.stage, st["stageName"], job, args.raid)
        except Exception as e:  # noqa: BLE001
            print(f"[{idx}/{len(jobs)}] #{job} 起任务失败：{e}", file=sys.stderr)
            return 2
        print(f"[{idx}/{len(jobs)}] #{job} run={run_id} 等待结算…", flush=True)
        snap = wait_done(cfg["api"], run_id, args.timeout)
        status = snap.get("status")
        print(f"    -> {status} {snap.get('error', '')}".rstrip())
        if status == "success":
            print(f"✅ {args.stage} 通关（作业 #{job}）")
            return 0
    print(f"❌ {args.stage} 这些作业都没过：{jobs}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
