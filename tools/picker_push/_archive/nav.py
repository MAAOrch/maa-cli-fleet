# -*- coding: utf-8 -*-
"""状态识别 + 逐跳校验的导航：先认页面，再决定点哪里；每跳都验，不中就停。

为什么这么做：2026-11-22 一晚上靠「固定坐标盲点一串」点错好几次（点进干员页、
点进卫戍协议小游戏、拿地图页当详情页起 copilot 报 BattleStartPre）。
根因是坐标/页面会变，而盲点链没有反馈。这里改成：

1. `classify()` 用固定 ROI 锚点模板（`templates/*.png`）认 7 种页面；
2. `goto_detail()` 按当前状态选**下一跳**，每跳后重新认页面，不中就停；
3. 认不出的页面（小游戏/弹层）→ 直接调 picker 的 `/api/nav/restart` 关游戏重开，
   再用 `/api/nav/home` 回主界面。
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
TPL_DIR = HERE / "templates"

# 状态 → 该状态下「去活动 EX 图」的下一跳（None = 已经是目标/需要复位）
TAP_EVENT_BANNER = (1120, 400)   # 主界面右侧活动缩略图
TAP_TERMINAL = (980, 265)        # 主界面「终端」大牌子
TAP_EVENT_CARD = (280, 380)      # 终端页左侧活动卡
TAP_HUB_CARD_EX = (845, 545)     # hub「长冬初青」（EX 图）
TAP_HUB_CARD_S = (1135, 680)     # hub「世纪之春」（S 图）
TAP_START = (520, 680)           # 标题页 START


class NavError(RuntimeError):
    pass


def _load_templates() -> dict[str, dict]:
    meta = json.loads((TPL_DIR / "meta.json").read_text(encoding="utf-8"))
    out = {}
    for state, info in meta.items():
        img = np.asarray(Image.open(TPL_DIR / info["file"]).convert("RGB"), dtype=np.float32)
        out[state] = {"roi": info["roi"], "img": img}
    return out


_TEMPLATES = _load_templates()

# 认页面的误差阈值：真匹配实测 0.0，次优 ≥20，取 15 当界线
UNKNOWN_ERR = 15.0

# 锚点允许的平移范围（像素，1280x720 坐标）：主界面/地图会因为语音气泡、解锁进度
# 轻微改布局，固定 ROI 直接比对会误判成 unknown，所以在小窗口里滑一滑取最小误差。
SHIFT_PX = 8


def _best_local(shot: np.ndarray, roi, tpl: np.ndarray) -> float:
    """在 ROI 附近 ±SHIFT_PX 内滑动模板，返回最小平均绝对差。"""
    h, w = shot.shape[:2]
    sx, sy = w / 1280.0, h / 720.0
    x0, y0, x1, y1 = (int(v) for v in roi)
    th, tw = tpl.shape[:2]
    best = float("inf")
    step = 2
    for dy in range(-SHIFT_PX, SHIFT_PX + 1, step):
        for dx in range(-SHIFT_PX, SHIFT_PX + 1, step):
            cx0 = max(0, int((x0 + dx) * sx))
            cy0 = max(0, int((y0 + dy) * sy))
            cx1 = min(w, cx0 + tw)
            cy1 = min(h, cy0 + th)
            if cx1 - cx0 < tw or cy1 - cy0 < th:
                continue
            crop = shot[cy0:cy1, cx0:cx1]
            best = min(best, float(np.mean(np.abs(crop - tpl))))
    return best


def classify(path: Path) -> tuple[str, dict[str, float]]:
    """返回 (最可能的页面, 各页面误差)。误差 = 锚点附近滑动后的最小平均绝对差。

    最像的那个误差超过 `UNKNOWN_ERR` 就判 "unknown"——宁可停下报「认不出」，
    也不要把未知页面当成地图去乱点（2026-11-22 就因为没阈值，在加载页上狂点关卡）。
    """
    shot = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
    scores: dict[str, float] = {}
    for state, tpl in _TEMPLATES.items():
        scores[state] = _best_local(shot, tpl["roi"], tpl["img"])
    best = min(scores, key=scores.get)
    if scores[best] > UNKNOWN_ERR:
        return "unknown", scores
    return best, scores


class Navigator:
    def __init__(self, cfg: dict, dev, shot_path: Path):
        self.cfg = cfg
        self.dev = dev
        self.shot_path = shot_path
        self.api = cfg["api"]

    # ---- 基本动作 ----

    def shot(self) -> bool:
        return self.dev.shot(self.shot_path)

    def state(self) -> tuple[str, dict[str, float]]:
        if not self.shot():
            raise NavError("截图失败（模拟器/adb 掉线？）")
        return classify(self.shot_path)

    def tap(self, xy) -> None:
        self.dev.tap(xy)
        time.sleep(2.2)

    def _post(self, path: str) -> dict:
        data = json.dumps({"accountId": self.cfg["account"]}).encode()
        req = urllib.request.Request(
            self.api + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)

    # ---- 复位 ----

    def reset_home(self, reason: str) -> None:
        print(f"    [nav] {reason} → 关游戏重开 + maa startup 回主界面")
        self._post("/api/nav/restart")
        self._post("/api/nav/home")
        time.sleep(2)

    # ---- 目标导航 ----

    def goto_detail(self, map_key: str, node_xy, raid: bool) -> str:
        """走到关卡详情页；返回走过的状态路径。

        原则：**只往前点、认不出就停**。绝不自动重启游戏——2026-11-22 因为「认不出就
        重启游戏」把用户整疯了（一直在重启）。要复位请显式加 --allow-restart。
        """
        visited = []
        for step in range(12):
            state, scores = self.state()
            visited.append(state)
            err = scores.get(state, min(scores.values()))
            print(f"    [nav] 第{step + 1}跳：页面={state} err={err:.1f}", flush=True)
            if state == "detail":
                if raid:
                    self.tap(self.cfg["ui"]["raid_button"])
                return " → ".join(visited)
            if state == "title":
                self.tap(TAP_START)
                time.sleep(6)
                continue
            if state == "loading":
                time.sleep(4)  # 过场/加载：只等，别点
                continue
            if state == "home":
                self.tap(TAP_EVENT_BANNER)
                continue
            if state == "terminal":
                self.tap(TAP_EVENT_CARD)
                continue
            if state == "hub":
                self.tap(TAP_HUB_CARD_EX if map_key == "ex" else TAP_HUB_CARD_S)
                continue
            if state in ("map_ex", "map_s"):
                want = "map_ex" if map_key == "ex" else "map_s"
                if state == want:
                    self.tap(node_xy)
                else:
                    self.tap(TAP_HUB_CARD_EX)
                continue
            # 认不出的页面（干员页 / 小游戏 / 结算 / 弹层）：停手，把页面告诉人
            raise NavError(
                f"当前停在 {state}（err={min(scores.values()):.1f}），不敢乱点；"
                "要么人工退到主界面，要么显式 --allow-restart 让我关游戏重开"
            )
        raise NavError("导航 7 跳没到详情页：状态=" + " → ".join(visited))
