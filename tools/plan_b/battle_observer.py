# -*- coding: utf-8 -*-
"""Plan B：战斗战报采集。

两部分：
1. `BattleObserver`：演习/正式打期间后台轮询（默认 12s 一帧，只读截图，不碰触控），
   记录击杀数/费用时间线，在「击杀数新高 / 蓝门掉血 / 战斗结束」时保存关键帧。
2. `build_report`：跑完后把 run 目录（settle.png、events.log）、MAA asst.log（按启动
   时间戳过滤）和时间线汇总成 `battle_report.json`。

用法（对已有 run 离线出战报，便于调参）：
  python tools/plan_b/battle_observer.py --run D:\\copilot-picker-local\\runs\\<id> --out report.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

import cv2
import numpy as np

REPO = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_RUNS = pathlib.Path(r"D:\copilot-picker-local\runs")
MAA_LOG = pathlib.Path(r"D:\copilot-picker-local\data_copilot\maa_state\debug\asst.log")
ADB = r"D:\Xiach\MuMuPlayer\nx_main\adb.exe"

_OCR = None


def _ocr():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
    return _OCR


def _shot(device: str, dst: pathlib.Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dst.open("wb") as f:
            subprocess.run([ADB, "-s", device, "exec-out", "screencap", "-p"], stdout=f,
                           stderr=subprocess.DEVNULL, timeout=20)
        return dst.exists() and dst.stat().st_size > 10_000
    except Exception:  # noqa: BLE001
        return False


_KILL_RE = re.compile(r"(\d{1,3})\s*/\s*(\d{1,3})")
_NUM_RE = re.compile(r"\b(\d{1,3})\b")


def _ocr_text(img) -> list[tuple[str, int, int, int, int]]:
    res, _ = _ocr()(img)
    out = []
    for it in res or []:
        box, text = it[0], str(it[1]).strip()
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append((text, int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))))
    return out


def _read_battle_hud(img) -> dict:
    """读战斗 HUD（分区域裁剪后 OCR，比整屏识准得多）。

    区域（1280x720 战斗界面实测）：
      击杀 24/58  → 顶部中右 [780..1280, 0..120]
      生命 5000/5000 → 左侧中 [260..560, 440..540]
      费用 54     → 右下 [980..1280, 500..720]
      演习中横幅  → 顶部中 [480..800, 30..90]
    """
    out: dict = {}
    if img is None or img.shape[0] < 700:
        return out

    def _up(roi, fx=2.0):
        return cv2.resize(roi, None, fx=fx, fy=fx, interpolation=cv2.INTER_CUBIC)

    # 击杀 24/58：顶部中偏左（实测 x≈540-620, y≈15-50）
    for text, *_b in _ocr_text(_up(img[0:90, 380:820])):
        m = _KILL_RE.search(text)
        if m:
            out["kills"] = int(m.group(1))
            out["kills_total"] = int(m.group(2))
            break
    # 生命（保护目标剩余）：顶部偏右，心形图标旁的小数字（实测 x≈740-780, y≈20-45）
    for text, *_b in _ocr_text(_up(img[0:60, 700:820], 3.0)):
        if _NUM_RE.fullmatch(text):
            v = int(text)
            if 0 <= v <= 99:
                out["life"] = v
                break
    # 费用：右下角大数字
    for text, *_b in _ocr_text(img[500:720, 980:1280]):
        if _NUM_RE.fullmatch(text):
            v = int(text)
            if 0 <= v <= 99:
                out["cost"] = v
                break
    # 演习横幅（顶部中：演习中）
    banner = "".join(t for t, *_b in _ocr_text(_up(img[30:90, 520:800])))
    if "演习" in banner:
        out["drill"] = True
    return out


def detect_stars(settle_png: pathlib.Path) -> dict:
    """从结算截图判星（与 picker 的判定口径对齐）。

    - 任务失败页 → 0 星
    - 活动徽章结算（单个大白色六边形）→ 3 星（picker 的 themed 口径）
    - 普通结算页：数左边三颗六角星里亮起的青色数量
    """
    img = cv2.imread(str(settle_png))
    if img is None:
        return {"stars": None, "reason": "结算图读不到"}
    h, w = img.shape[:2]
    res, _ = _ocr()(img)
    text = "".join(str(it[1]) for it in (res or []))
    if "任务失败" in text or "MISSION FAILED" in text.upper():
        return {"stars": 0, "reason": "任务失败页"}

    roi = img[280:390, 50:260]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    # 亮青色（三星徽章点亮色）
    cyan = cv2.inRange(hsv, (80, 80, 150), (105, 255, 255))
    n, _lab, stats, _c = cv2.connectedComponentsWithStats(cyan, 8)
    blobs = [int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] > 300]
    if blobs:
        return {"stars": min(len(blobs), 3), "reason": f"数到 {len(blobs)} 颗亮星"}
    # 活动徽章：大面积白色
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    white = cv2.inRange(gray, 200, 255)
    n2, _l2, st2, _c2 = cv2.connectedComponentsWithStats(white, 8)
    big = [int(st2[i, cv2.CC_STAT_AREA]) for i in range(1, n2) if st2[i, cv2.CC_STAT_AREA] > 3000]
    if big:
        return {"stars": 3, "reason": "活动徽章结算（themed）"}
    return {"stars": None, "reason": "认不出星数（既不是失败页也不是常见结算样式）"}


class BattleObserver(threading.Thread):
    """战斗期间轮询截图 + 记 HUD 时间线 + 关键帧。"""

    def __init__(self, device: str, out_dir: pathlib.Path, interval: float = 12.0):
        super().__init__(daemon=True)
        self.device = device
        self.frames = out_dir / "frames"
        self.tmp = out_dir / "tmp"
        self.frames.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.interval = interval
        self.timeline: list[dict] = []
        self.stop_flag = threading.Event()
        self._best_kills = -1
        self._last_key_t = 0.0

    def _save_frame(self, src: pathlib.Path, tag: str) -> str:
        dst = self.frames / f"{int(time.time())}-{tag}.png"
        try:
            dst.write_bytes(src.read_bytes())
        except Exception:  # noqa: BLE001
            return ""
        return str(dst)

    def run(self) -> None:
        i = 0
        while not self.stop_flag.is_set():
            i += 1
            p = self.tmp / f"poll-{i}.png"
            if not _shot(self.device, p):
                time.sleep(self.interval)
                continue
            img = cv2.imread(str(p))
            if img is None:
                time.sleep(self.interval)
                continue
            hud = _read_battle_hud(img)
            entry = {"t": round(time.time(), 1), "poll": i, **hud}
            kills = hud.get("kills")
            if kills is not None and kills > self._best_kills:
                self._best_kills = kills
                frame = self._save_frame(p, f"kills-{kills}")
                if frame:
                    entry["frame"] = frame
            # 费用卡满（>=99）连续出现时留一帧
            if hud.get("cost") == 99 and time.time() - self._last_key_t > 30:
                self._last_key_t = time.time()
                frame = self._save_frame(p, "cost-max")
                if frame:
                    entry["frame"] = frame
            self.timeline.append(entry)
            time.sleep(self.interval)

    def stop(self) -> None:
        self.stop_flag.set()


def _filter_log(since: datetime, log_path: pathlib.Path = MAA_LOG, tail_bytes: int = 40_000_000) -> list[str]:
    if not log_path.exists():
        return []
    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in data.splitlines():
        m = re.search(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\]", line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            continue
        if ts >= since:
            out.append(line)
    return out


def build_report(run_dir: pathlib.Path, timeline: list[dict] | None = None) -> dict:
    run_dir = run_dir.resolve()
    ev = run_dir / "events.log"
    events = ev.read_text(encoding="utf-8", errors="replace").splitlines() if ev.exists() else []
    started = None
    for line in events:
        m = re.match(r"(\d\d:\d\d:\d\d)", line)
        if m and started is None:
            started = m.group(1)
    # run 当天的启动时刻
    day = None
    m = re.search(r"runs[\\/](\d{8})-(\d{6})", str(run_dir))
    since = datetime.now()
    if m:
        since = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    log_lines = _filter_log(since)
    errors = [ln for ln in log_lines if "SubTaskError" in ln or "TaskChainError" in ln]
    last_actions = [ln for ln in log_lines if "cur_task" in ln][-5:]

    settle = run_dir / "settle.png"
    stars = detect_stars(settle) if settle.exists() else {"stars": None, "reason": "没有 settle.png"}
    job_title = ""
    for jf in run_dir.glob("job-*.json"):
        try:
            job_title = (json.loads(jf.read_text(encoding="utf-8")).get("doc") or {}).get("title", "")
        except Exception:  # noqa: BLE001
            pass
        break
    result = "unknown"
    for line in events:
        if "任务结束：" in line:
            result = line.split("任务结束：")[-1].strip()
    return {
        "runDir": str(run_dir),
        "jobTitle": job_title,
        "result": result,
        "stars": stars.get("stars"),
        "starsReason": stars.get("reason"),
        "settlePng": str(settle) if settle.exists() else "",
        "pretflightPng": str(run_dir / "preflight.png"),
        "timeline": timeline or [],
        "maxKills": max((e.get("kills") or 0 for e in (timeline or [])), default=None),
        "asst": {
            "since": since.isoformat(),
            "errorCount": len(errors),
            "lastErrors": [e[:300] for e in errors[-5:]],
            "lastTaskLines": [l.strip()[:200] for l in last_actions],
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Plan B 战报采集")
    ap.add_argument("--run", required=True, help="run 目录（D:\\copilot-picker-local\\runs\\<id>）")
    ap.add_argument("--out", default="", help="battle_report.json 输出路径（默认写进 run 目录）")
    args = ap.parse_args(argv)
    run_dir = pathlib.Path(args.run)
    rep = build_report(run_dir)
    out = pathlib.Path(args.out) if args.out else run_dir / "battle_report.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: rep[k] for k in ("runDir", "result", "stars", "starsReason", "maxKills")},
                     ensure_ascii=False, indent=2))
    print("battle_report ->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
