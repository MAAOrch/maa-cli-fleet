# -*- coding: utf-8 -*-
"""Plan B：白盒战斗观测（高频轻量采样 + 事故九宫格 + action_trace）。

设计目标（2026-11-22 用户要求，2026-11-23 调整）：
  1. 默认每 3.0s 采一帧（进入战斗后前 25s 静默不截帧），**只进内存环形缓冲**
     （deque，保留最近 ~36s），不逐帧落盘；战斗前探测间隔放宽到 ≥4.0s，避免和
     编队/加载抢 ADB；
  2. 采样时只对顶部/右下几个**极小 ROI** 做 OCR（击杀数/费用/蓝门生命），不跑全图 OCR，
     避免拖慢模拟器或干扰 MaaCore 触控；
  3. 命中异常（蓝门掉血 / 任务失败页 / 99费且击杀停滞25s / 结算<3★）时，把出事前最近
     9 帧拼成 3×3 九宫格分镜图（1920 宽 JPEG），每格叠加
     `[#n T-XXs | kills=XX/58 | cost=XX | life=X]` 标注；
  4. 演习模式默认 `--early-stop`：确认异常（战斗≥20s 且连续 2 帧命中同一异常）后先落盘分镜图，
     再中止本局（省掉后面 2~3 分钟无效等待），由 iterate.py 负责退出演习回主界面；
  5. 结束后把 asst.log 的 Copilot 动作轨迹整理成 `action_trace`（含成功部署列表、
     最后成功动作、卡住/失败动作）。

用法（对已有 run 离线出战报）：
  python tools/plan_b/battle_observer.py --run <run目录> [--out report.json]
"""

from __future__ import annotations

import argparse
import collections
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
PI_IMG = REPO / "pi" / "arknights-event" / "resource" / "image" / "EventNav"

# 失败页模板按 1280x720 参考分辨率裁剪；其它分辨率按宽度等比缩放模板后再匹配。
FAIL_TPL_REF_WIDTH = 1280
FAIL_MATCH_THRESHOLD = 0.75
# 进入战斗前（编队/加载/过场）放宽探测间隔，别和 MaaCore 抢 ADB。
PREBATTLE_MIN_INTERVAL = 4.0
# 战斗内低频 OCR 补标签的周期（分镜标注用）。
LABEL_REFRESH_SECS = 20.0

_OCR = None


def _ocr():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
    return _OCR


def _ocr_text(img) -> list[tuple[str, int, int, int, int]]:
    res, _ = _ocr()(img)
    out = []
    for it in res or []:
        box, text = it[0], str(it[1]).strip()
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append((text, int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))))
    return out


_KILL_RE = re.compile(r"(\d{1,3})\s*/\s*(\d{1,3})")
_NUM_RE = re.compile(r"\b(\d{1,3})\b")

_FAIL_TEMPLATES: list[np.ndarray] = []


def _fail_template():
    global _FAIL_TEMPLATES
    if not _FAIL_TEMPLATES:
        p = PI_IMG / "result_fail.png"
        t = cv2.imread(str(p)) if p.exists() else None
        if t is not None:
            _FAIL_TEMPLATES.append(t)
    return _FAIL_TEMPLATES[0] if _FAIL_TEMPLATES else None


def _decode_raw_screencap(data: bytes):
    """解析 `screencap` 原始帧（RGBA）并转成 OpenCV 约定的 **BGR**。

    坑：`adb exec-out screencap` 原始帧是 RGBA，直接 `arr[:, :, :3]` 得到的是 **RGB**；
    而 `cv2.imread` 读出的模板是 BGR，通道反了会严重拉低 `matchTemplate` 得分。
    """
    if not data or len(data) < 16:
        return None
    import struct
    w, h = struct.unpack_from("<II", data, 0)
    if w <= 0 or h <= 0 or len(data) < 12 + w * h * 4:
        return None
    arr = np.frombuffer(data, dtype=np.uint8, count=w * h * 4, offset=12).reshape(h, w, 4)
    return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)


def _shot_raw(device: str, timeout: float = 10.0):
    """`adb exec-out screencap` 原始帧（RGBA→BGR），比 `-p` 的 PNG 快一个数量级。

    实测 `screencap -p` 单帧 ~3.4s（设备端 PNG 编码 + 管道传输），这是之前"白盒采样
    把模拟器拖慢、MAA 超时"的元凶。原始帧只需传输+解码，单帧 ~0.4s。
    """
    try:
        p = subprocess.run([ADB, "-s", device, "exec-out", "screencap"],
                           capture_output=True, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    return _decode_raw_screencap(p.stdout)


def _shot_bytes(device: str, timeout: float = 15.0) -> bytes | None:
    try:
        p = subprocess.run([ADB, "-s", device, "exec-out", "screencap", "-p"],
                           capture_output=True, timeout=timeout)
        if p.returncode == 0 and len(p.stdout) > 10_000:
            return p.stdout
    except Exception:  # noqa: BLE001
        pass
    return None


def _decode(data: bytes):
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def read_hud(img, life_roi_hint=None) -> dict:
    """轻量 HUD 读取：只 OCR 三个极小 ROI（击杀 / 蓝门生命 / 费用）。

    实测坐标（1280x720 战斗界面）：
      击杀 24/58   x 540-620, y 15-50
      蓝门生命 1   x 740-780, y 20-45
      费用 54      x 1060-1120, y 540-600
    """
    out: dict = {}
    if img is None or img.shape[0] < 600:
        return out

    def _up(roi, fx=3.0):
        return cv2.resize(roi, None, fx=fx, fy=fx, interpolation=cv2.INTER_CUBIC)

    for text, *_b in _ocr_text(_up(img[8:60, 500:680], 2.5)):
        m = _KILL_RE.search(text)
        if m:
            out["kills"] = int(m.group(1))
            out["kills_total"] = int(m.group(2))
            break
    for text, *_b in _ocr_text(_up(img[8:60, 720:800], 3.0)):
        if _NUM_RE.fullmatch(text):
            v = int(text)
            if 0 <= v <= 99:
                out["life"] = v
                break
    for text, *_b in _ocr_text(_up(img[520:620, 1040:1200], 2.5)):
        if _NUM_RE.fullmatch(text):
            v = int(text)
            if 0 <= v <= 99:
                out["cost"] = v
                break
    return out


_KILLS_CROP = (8, 60, 500, 680)   # y0,y1,x0,x1
_LIFE_CROP = (8, 60, 720, 800)
_COST_CROP = (520, 620, 1040, 1200)
_cost99_tpl = None


def _cost99_template(np_img=None):
    """费用 99 的模板（从真实战斗帧裁一次，之后模板匹配判断是否满费）。"""
    global _cost99_tpl
    if _cost99_tpl is None and np_img is not None:
        _cost99_tpl = np_img[_COST_CROP[0]:_COST_CROP[1], _COST_CROP[2]:_COST_CROP[3]].copy()
    return _cost99_tpl


class FastHud:
    """轻量 HUD：每帧只做像素哈希 + 模板匹配（毫秒级），OCR 低频兜底。

    为什么不用 OCR：实测 RapidOCR 单次 1.2~4.5s，3.0s 采样根本跑不动，还会拖慢本机。
    这里只判「击杀数变没变 / 是否满费 99 / 是否任务失败页」，数值标签用低频 OCR 的值。
    """

    def __init__(self, fail_tpl):
        self.fail_tpl = fail_tpl
        self.kills_hash: int | None = None
        self.kills_value: int | None = None
        self.kills_total: int | None = None
        self.life: int | None = None
        self.cost: int | None = None
        self.last_kills_change = 0.0
        self.cost99_since: float | None = None
        self._cost99_ref = None
        self.fail_streak = 0

    @staticmethod
    def _h(img) -> int:
        # 只对 kills 小 ROI 做 8x8 均值哈希（字节哈希，够稳够快）
        c = img[_KILLS_CROP[0]:_KILLS_CROP[1], _KILLS_CROP[2]:_KILLS_CROP[3]]
        g = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(g, (8, 8), interpolation=cv2.INTER_AREA)
        return hash(small.tobytes())

    def cost_is_99(self, img) -> bool:
        global _cost99_tpl
        if self.cost99_since is None:
            return False
        if _cost99_tpl is None:
            return self.cost == 99
        c = img[_COST_CROP[0]:_COST_CROP[1], _COST_CROP[2]:_COST_CROP[3]]
        if c.shape != _cost99_tpl.shape:
            return False
        r = cv2.matchTemplate(c, _cost99_tpl, cv2.TM_CCOEFF_NORMED)
        return float(r.max()) >= 0.85

    def fail_score(self, img) -> float:
        """失败页模板匹配得分（0~1）。

        round-11 事故：原实现把整帧 `resize(fx=0.5)` 却**不缩放模板**，
        345x185 的模板去匹配缩成 ~140x62 的 logo，得分只有 ~0.19，
        连续 6 帧失败页全部漏判。这里保持全分辨率，只在分辨率不是 1280 宽时
        按宽度等比缩放模板；同时 `_shot_raw` 已统一返回 BGR，与 `cv2.imread` 模板同通道。
        """
        if self.fail_tpl is None or img is None:
            return 0.0
        h, w = img.shape[:2]
        scale = w / float(FAIL_TPL_REF_WIDTH)
        tpl = self.fail_tpl
        if abs(scale - 1.0) > 0.01:
            tw = max(1, int(round(tpl.shape[1] * scale)))
            th = max(1, int(round(tpl.shape[0] * scale)))
            tpl = cv2.resize(tpl, (tw, th),
                             interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        if tpl.shape[0] > h or tpl.shape[1] > w:
            return 0.0
        r = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
        return float(r.max())

    def is_fail_page(self, img) -> bool:
        return self.fail_score(img) >= FAIL_MATCH_THRESHOLD

    def update(self, img, now: float) -> dict:
        h = self._h(img)
        changed = self.kills_hash is not None and h != self.kills_hash
        if self.kills_hash is None or changed:
            self.kills_hash = h
            if changed:
                self.last_kills_change = now
        return {"kills_changed": changed}

    def refresh_labels(self, img, now: float) -> dict:
        """低频 OCR：把当前击杀/生命/费用数值补上（用于分镜标注）。"""
        hud = read_hud(img)
        if hud.get("kills") is not None:
            if hud["kills"] != self.kills_value:
                self.last_kills_change = now
            self.kills_value = hud["kills"]
            self.kills_total = hud.get("kills_total")
        if hud.get("life") is not None:
            self.life = hud["life"]
        if hud.get("cost") is not None:
            self.cost = hud["cost"]
            if hud["cost"] == 99 and self.cost99_since is None:
                self.cost99_since = now
                global _cost99_tpl
                if _cost99_tpl is None:
                    _cost99_tpl = img[_COST_CROP[0]:_COST_CROP[1], _COST_CROP[2]:_COST_CROP[3]].copy()
            elif hud["cost"] != 99:
                self.cost99_since = None
        return {"kills": self.kills_value, "kills_total": self.kills_total,
                "life": self.life, "cost": self.cost}


def detect_stars(settle_png: pathlib.Path) -> dict:
    """结算图判星（与 picker 口径一致）：失败页 0；活动徽章 3；普通结算数亮星。"""
    img = cv2.imread(str(settle_png))
    if img is None:
        return {"stars": None, "reason": "结算图读不到"}
    res, _ = _ocr()(img)
    text = "".join(str(it[1]) for it in (res or []))
    if "任务失败" in text or "MISSION FAILED" in text.upper():
        return {"stars": 0, "reason": "任务失败页"}
    roi = img[280:390, 50:260]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    cyan = cv2.inRange(hsv, (80, 80, 150), (105, 255, 255))
    n, _lab, stats, _c = cv2.connectedComponentsWithStats(cyan, 8)
    blobs = [int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] > 300]
    if blobs:
        return {"stars": min(len(blobs), 3), "reason": f"数到 {len(blobs)} 颗亮星"}
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    white = cv2.inRange(gray, 200, 255)
    n2, _l2, st2, _c2 = cv2.connectedComponentsWithStats(white, 8)
    big = [int(st2[i, cv2.CC_STAT_AREA]) for i in range(1, n2) if st2[i, cv2.CC_STAT_AREA] > 3000]
    if big:
        return {"stars": 3, "reason": "活动徽章结算（themed）"}
    return {"stars": None, "reason": "认不出星数（既不是失败页也不是常见结算样式）"}


def _annotate(img, text: str, scale: float = 0.5):
    """左上角半透明黑底 + 白字（只用 ASCII，cv2 不带中文字体）。"""
    out = img.copy()
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    overlay = out.copy()
    cv2.rectangle(overlay, (0, 0), (tw + 12, th + 10), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, out, 0.45, 0, out)
    cv2.putText(out, text, (6, th + 6), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def make_contact_sheet(frames: list[dict], out_path: pathlib.Path, incident_t: float,
                       cell_w: int = 640, cell_h: int = 360, refresh_hud: bool = False) -> str:
    """3×3 九宫格分镜图：每格 [编号 T-XXs | kills/cost/life] 标注。

    refresh_hud=True 时会对选中的帧重新 OCR 击杀数（低频 OCR 存的值可能滞后，
    分镜要的是"那一刻"的数字，误差太大就失去复盘意义）。
    """
    if not frames:
        return ""
    canvas = np.full((cell_h * 3, cell_w * 3, 3), 20, dtype=np.uint8)
    for i, fr in enumerate(frames[-9:]):
        img = fr["img"]
        if img is None:
            continue
        cell = cv2.resize(img, (cell_w, cell_h), interpolation=cv2.INTER_AREA)
        hud = dict(fr.get("hud") or {})
        if refresh_hud:
            full = fr.get("full")
            if full is not None:
                fresh = read_hud(full)
                for k in ("kills", "kills_total", "life", "cost"):
                    if fresh.get(k) is not None:
                        hud[k] = fresh[k]
        t_off = int(round(incident_t - fr.get("t", incident_t)))
        label = (f"#{i + 1} T-{t_off:02d}s | kills={hud.get('kills', '?')}/{hud.get('kills_total', '?')}"
                 f" | cost={hud.get('cost', '?')} | life={hud.get('life', '?')}")
        cell = _annotate(cell, label, 0.52)
        r, c = divmod(i, 3)
        canvas[r * cell_h:(r + 1) * cell_h, c * cell_w:(c + 1) * cell_w] = cell
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ok, enc = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if ok:
        out_path.write_bytes(enc.tobytes())
        return str(out_path)
    return ""


class BattleObserver(threading.Thread):
    """高频轻量采样 + 环形缓冲 + 异常熔断。"""

    def __init__(self, device: str, out_dir: pathlib.Path, interval: float = 3.0,
                 ring: int = 12, early_stop: bool = False, stop_cb=None,
                 battle_min_confirm: float = 20.0, stall_secs: float = 25.0,
                 startup_quiet_secs: float = 25.0):
        super().__init__(daemon=True)
        self.device = device
        self.out_dir = out_dir
        self.interval = interval
        self.buf: collections.deque = collections.deque(maxlen=ring)
        self.early_stop = early_stop
        self.stop_cb = stop_cb
        self.battle_min_confirm = battle_min_confirm
        self.stall_secs = stall_secs
        self.startup_quiet_secs = max(0.0, float(startup_quiet_secs))
        self.stop_flag = threading.Event()
        self.timeline: list[dict] = []
        self.perf: list[float] = []
        self.battle_start: float | None = None
        self.initial_life: int | None = None
        self.initial_kills_total: int | None = None
        self.last_kills_change = 0.0
        self._last_kills: int | None = None
        self._anomaly_streak: dict[str, int] = {}
        self.incidents: list[dict] = []
        self.contact_sheets: list[str] = []
        self.stars_at_end: int | None = None
        self.picker = ""
        self.run_id = ""

    # ── 内部 ──
    def bind_run(self, picker: str, run_id: str) -> None:
        """绑定 picker run：采样改用 /api/run/<id>/shot（MaaCore 自己截的帧，零额外 adb 负载）。

        为什么：即使 3.0s 一次的 `adb exec-out screencap` 也会和 MaaCore 抢 adb（实测单帧 3.4s、
        MAA 识别超时 → 任务链报错）。改用 picker 缓存的截图后对 MAA 完全无干扰。
        """
        self.picker = picker.rstrip("/")
        self.run_id = run_id

    def _grab(self):
        """返回 BGR 图像：优先 raw screencap（无 PNG 编解码）。"""
        img = _shot_raw(self.device)
        if img is not None:
            return img
        data = _shot_bytes(self.device)
        return _decode(data) if data else None

    def _battle_elapsed(self) -> float:
        return (time.time() - self.battle_start) if self.battle_start else 0.0

    def _probe_interval(self) -> float:
        """本轮采样后的等待间隔：战斗前 ≥4.0s，避免抢编队/加载期的 ADB。"""
        if self.battle_start is None:
            return max(PREBATTLE_MIN_INTERVAL, self.interval)
        return self.interval

    def _quiet_remaining(self, now: float | None = None) -> float:
        """开局静默期剩余秒数；>0 时主循环只等待，绝不向 ADB 发 screencap。"""
        if self.battle_start is None:
            return 0.0
        now = time.time() if now is None else now
        return max(0.0, self.battle_start + self.startup_quiet_secs - now)

    def _confirm(self, kind: str, hit: bool, now: float) -> bool:
        """连续 2 帧命中同一异常才算确认（防加载黑屏/单帧 OCR 抖动误熔断）。"""
        streak = self._anomaly_streak.get(kind, 0)
        streak = streak + 1 if hit else 0
        self._anomaly_streak[kind] = streak
        return hit and streak >= 2 and self._battle_elapsed() >= self.battle_min_confirm

    def _fire_incident(self, kind: str, now: float, drop: bool) -> None:
        frames = list(self.buf)
        hud = (frames[-1].get("hud") if frames else None) or {}
        sheet = make_contact_sheet(frames, self.out_dir / f"incident_{kind}_{int(now)}.jpg", now,
                                  refresh_hud=True)
        if sheet:
            self.contact_sheets.append(sheet)
        self.incidents.append({"kind": kind, "at": round(now, 1), "elapsed": round(self._battle_elapsed(), 1),
                               "hud": hud, "sheet": sheet})
        if drop and self.early_stop and self.stop_cb:
            try:
                self.stop_cb()
            except Exception:  # noqa: BLE001
                pass
            self.stop_flag.set()

    # ── 主循环 ──
    def run(self) -> None:
        fail_tpl = _fail_template()
        fast = FastHud(fail_tpl)
        i = 0
        last_labels = 0.0
        while not self.stop_flag.is_set():
            # 0) 开局静默期：确认 battle_start 后 startup_quiet_secs 内完全不截帧，
            #    把开局的 ADB/CPU 全部让给 MaaCore 部署关键干员；只等停止信号。
            quiet = self._quiet_remaining()
            if quiet > 0:
                self.stop_flag.wait(quiet)
                continue

            i += 1
            t0 = time.time()
            img = self._grab()
            if img is None:
                self.stop_flag.wait(self._probe_interval())
                continue
            now = time.time()
            fast.update(img, now)
            fail = fast.is_fail_page(img)
            if fail:
                fast.fail_streak += 1
            else:
                fast.fail_streak = 0
            # OCR：战斗前每次探测都做（尽早确认 battle_start）；战斗后低频 ~20s 一次。
            if self.battle_start is None or (now - last_labels) >= LABEL_REFRESH_SECS:
                fast.refresh_labels(img, now)
                last_labels = now
            labels = {"kills": fast.kills_value, "kills_total": fast.kills_total,
                      "life": fast.life, "cost": fast.cost}
            # 确认进入战斗：击杀数可读，或「蓝门生命 + 费用」同时可读（都是战斗 HUD 特征）。
            # 只认击杀数会在 OCR 漏读击杀 ROI 时永远进不了战斗（round-11 就是如此）。
            if self.battle_start is None and (
                    fast.kills_value is not None
                    or (fast.life is not None and fast.cost is not None)):
                self.battle_start = now
                # 丢掉编队/加载期探测帧，分镜只保留战斗画面。
                self.buf.clear()
            if fast.life is not None and self.initial_life is None:
                self.initial_life = fast.life
            # 环形缓冲只在战斗内采样（静默期已在循环顶部跳过）。
            if self.battle_start is not None:
                small = cv2.resize(img, (640, 360), interpolation=cv2.INTER_AREA)
                self.buf.append({"t": now, "img": small, "full": img, "hud": dict(labels)})
            self.perf.append(now - t0)
            self.timeline.append({"t": round(now, 1), "poll": i,
                                  "elapsed": round(self._battle_elapsed(), 1), **labels,
                                  "cost99": fast.cost_is_99(img), "fail": fail,
                                  "ms": round((now - t0) * 1000)})

            # ---- 异常判定（连续 2 帧确认 + 战斗 ≥20s；静默期不会走到这里）----
            if self.battle_start is not None:
                life_drop = (self.initial_life is not None and fast.life is not None
                             and fast.life < self.initial_life)
                if self._confirm("life_drop", life_drop, now):
                    self._fire_incident("life_drop", now, drop=True)

                stall = False
                if self._battle_elapsed() >= self.battle_min_confirm and fast.last_kills_change:
                    stalled = (now - fast.last_kills_change) >= self.stall_secs
                    stall = stalled and fast.cost_is_99(img)
                if self._confirm("stall99", stall, now):
                    self._fire_incident("stall99", now, drop=True)

                if self._confirm("task_fail", fail, now):
                    self._fire_incident("task_fail", now, drop=True)
            self.stop_flag.wait(max(0.0, self._probe_interval() - (time.time() - t0)))

    def stop(self) -> None:
        self.stop_flag.set()

    def perf_summary(self) -> dict:
        if not self.perf:
            return {}
        arr = sorted(self.perf)
        return {"frames": len(arr), "avg_ms": round(sum(arr) / len(arr) * 1000, 1),
                "p50_ms": round(arr[len(arr) // 2] * 1000, 1),
                "p95_ms": round(arr[int(len(arr) * 0.95)] * 1000, 1), "max_ms": round(arr[-1] * 1000, 1)}

    def save_final_sheet(self, incident_t: float | None = None, tag: str = "final") -> str:
        frames = list(self.buf)
        if not frames:
            return ""
        sheet = make_contact_sheet(frames, self.out_dir / f"{tag}_contact_sheet.jpg",
                                  incident_t or time.time(), refresh_hud=True)
        if sheet:
            self.contact_sheets.append(sheet)
        return sheet

    def final_hud(self) -> dict:
        for fr in reversed(list(self.buf)):
            if fr.get("hud"):
                return fr["hud"]
        return {}


# ── asst.log → action_trace ──

_TS = re.compile(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\]")
_BP = re.compile(r'SubTaskExtraInfo \{"class":"asst::BattleProcessTask","details":\{"action":"([^"]+)"'
                 r'(?:,"doc":(?:""|"[^"]*"),"doc_color":(?:""|"[^"]*"),"elapsed_time":-?\d+,"target":"([^"]*)"})?')
_ERR_GROUP = re.compile(r"BattleProcessTask::to_group Group not found in combat data: (.+)$")
_DEPLOY_ENTER = re.compile(r"BattleHelper::deploy_oper \| enter")
_SLEEP = re.compile(r"sleep_and_do_strategy ms: (\d+)")


def action_trace(since: datetime, until: datetime | None = None,
                 log_path: pathlib.Path = MAA_LOG) -> dict:
    lines = _filter_log(since, log_path, until=until)
    trace: list[dict] = []
    deployed: list[str] = []
    errors: list[str] = []
    last_sleep = 0
    for line in lines:
        ts = ""
        m = _TS.search(line)
        if m:
            ts = m.group(1)[11:]
        if _DEPLOY_ENTER.search(line):
            # 紧跟的上一条 Deploy 目标就是这个实际部署
            target = next((t["target"] for t in reversed(trace) if t["action"] == "Deploy" and t.get("_pending")), "")
            if target:
                for t in reversed(trace):
                    if t.get("_pending") and t["action"] == "Deploy":
                        t["_pending"] = False
                        break
                deployed.append(target)
            trace.append({"ts": ts, "action": "deploy_oper", "target": target, "status": "ok"})
            continue
        m = _BP.search(line)
        if m:
            action, target = m.group(1), (m.group(2) or "")
            trace.append({"ts": ts, "action": action, "target": target, "status": "executed",
                          "_pending": action == "Deploy"})
            continue
        m = _ERR_GROUP.search(line)
        if m:
            errors.append(m.group(1).strip())
            trace.append({"ts": ts, "action": "to_group", "target": m.group(1).strip(), "status": "error"})
            continue
        m = _SLEEP.search(line)
        if m:
            last_sleep = int(m.group(1))
            if last_sleep >= 1000:
                trace.append({"ts": ts, "action": "wait_trigger", "target": "", "status": f"waiting {last_sleep}ms"})
    # 清理内部字段
    for t in trace:
        t.pop("_pending", None)
    # 最后成功动作（非等待类）
    ok_actions = [t for t in trace if t["status"] in ("executed", "ok")]
    last_ok = ok_actions[-1] if ok_actions else None
    # 卡住/失败：最后一条真错误（排除 to_group 这种无害告警）或最后一条 waiting
    real_errors = [t for t in trace if t["status"] == "error" and t["action"] != "to_group"]
    stuck = real_errors[-1] if real_errors else None
    if stuck is None:
        waiting = [t for t in trace if t["status"].startswith("waiting")]
        stuck = waiting[-1] if waiting else None
    if stuck is None:
        stuck = trace[-1] if trace else None
    return {
        "trace": trace[-120:],
        "deployed_opers": deployed,
        "last_ok_action": last_ok,
        "stuck_or_failed_action": stuck,
    }


def _filter_log(since: datetime, log_path: pathlib.Path = MAA_LOG, tail_bytes: int = 40_000_000,
                until: datetime | None = None) -> list[str]:
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
        m = _TS.search(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            continue
        if ts >= since and (until is None or ts <= until):
            out.append(line)
    return out


def build_report(run_dir: pathlib.Path, timeline: list[dict] | None = None,
                 observer: "BattleObserver | None" = None) -> dict:
    run_dir = run_dir.resolve()
    ev = run_dir / "events.log"
    events = ev.read_text(encoding="utf-8", errors="replace").splitlines() if ev.exists() else []
    m = re.search(r"runs[\\/](\d{8})-(\d{6})", str(run_dir))
    since = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S") if m else datetime.now()
    settle = run_dir / "settle.png"
    # 日志上界：用本 run 的 settle.png 修改时间（没有就用目录里最新文件）+10s，
    # 否则会把后续几轮 run 的 action 也扫进来（实测串轮）。
    until = None
    stamp = settle.stat().st_mtime if settle.exists() else 0
    if not stamp:
        mtimes = [f.stat().st_mtime for f in run_dir.glob("*") if f.is_file()]
        stamp = max(mtimes) if mtimes else 0
    if stamp:
        until = datetime.fromtimestamp(stamp + 10)
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
    tl = timeline if timeline is not None else []
    kills_seq = [e.get("kills") for e in tl if e.get("kills") is not None]
    rep = {
        "runDir": str(run_dir),
        "jobTitle": job_title,
        "result": result,
        "stars": stars.get("stars"),
        "starsReason": stars.get("reason"),
        "settlePng": str(settle) if settle.exists() else "",
        "timeline": tl,
        "maxKills": max(kills_seq) if kills_seq else None,
        "killsSeries": kills_seq,
        "action_trace": action_trace(since, until),
        "asst": {"since": since.isoformat()},
    }
    if observer is not None:
        rep["perf"] = observer.perf_summary()
        rep["incidents"] = observer.incidents
        rep["contactSheets"] = observer.contact_sheets
        rep["initialLife"] = observer.initial_life
        rep["battleSeconds"] = round(observer._battle_elapsed(), 1)
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Plan B 战报采集（离线对已有 run）")
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    run_dir = pathlib.Path(args.run)
    rep = build_report(run_dir)
    out = pathlib.Path(args.out) if args.out else run_dir / "battle_report.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: rep[k] for k in ("runDir", "result", "stars", "starsReason", "maxKills")},
                     ensure_ascii=False, indent=2))
    print("deployed:", rep["action_trace"]["deployed_opers"])
    print("last_ok:", rep["action_trace"]["last_ok_action"])
    print("stuck:", rep["action_trace"]["stuck_or_failed_action"])
    print("battle_report ->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
