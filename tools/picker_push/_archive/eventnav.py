# -*- coding: utf-8 -*-
"""活动图 OCR 导航：认页面 → 找关卡标签 → 点图标 → 校验详情页 → 起 copilot → 收结算。

替代 `nav.py` 的固定 ROI 模板方案。模板在换活动/换皮肤时必失效，而 OCR 认的是
「OPERATION / OS-3 / 积雪清理系统 / 圣巡之旅」这些真实文字，换活动只要改
`events/<event>.json` 里的区域名和三张图入口坐标。

设计要点（2026-11-22 一堆点错的教训）：
1. 每一步都截图 OCR 认页面，不认就停，绝不盲点一串坐标；
2. 找关卡按 OCR 文本精确匹配（`OS-3` 不会命中 `OS-30`），点到图标（标签左边 34px 的六边形）；
3. 点完必校验详情页右上角就是目标关卡代号，不是就退回来重找；
4. 结算页用 OCR 认「行动结束 / 是否将此阵容保存为代理指挥阵容」，点完再认一次；
5. 认不出的页面 → 返回键退一级再认；连退 3 次还认不出才调 picker 关游戏重开。

用法见 `push_event.py`。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np
from rapidocr_onnxruntime import RapidOCR

HERE = Path(__file__).resolve().parent
SHOT_DIR = Path(r"D:\maa-cli-fleet\_tmp\ev")

# 1280x720 设计坐标
BTN_BACK = (45, 42)          # 左上返回
BTN_CONFIRM = (1150, 690)    # 结算页「确认」
BTN_SAVE_FORMATION = (1170, 262)  # 保存代理指挥阵容 ✔
BTN_NOSAVE_FORMATION = (975, 262)  # ✖


class NavError(RuntimeError):
    pass


class Game:
    """一台设备：截图 / 点击 / 滑动 / OCR。"""

    def __init__(self, adb: str, serial: str, shot_dir: Path = SHOT_DIR):
        self.adb = adb
        self.serial = serial
        self.shot_dir = shot_dir
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self._ocr = RapidOCR()
        self._n = 0

    # ---- 设备动作 ----

    def _run(self, *args: str) -> None:
        subprocess.run(
            [self.adb, "-s", self.serial, *args],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def tap(self, xy) -> None:
        self._run("shell", "input", "tap", str(int(xy[0])), str(int(xy[1])))

    def swipe(self, x1, y1, x2, y2, ms: int = 350) -> None:
        self._run("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))

    def back(self) -> None:
        self._run("shell", "input", "keyevent", "4")

    def foreground_package(self) -> str:
        out = subprocess.run(
            [self.adb, "-s", self.serial, "shell", "dumpsys window | grep mCurrentFocus"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout or ""
        m = re.search(r"u0 ([A-Za-z0-9_.]+)/", out)
        return m.group(1) if m else ""

    def shot(self, tag: str = "now", retries: int = 3) -> Path:
        self._n += 1
        p = self.shot_dir / f"{tag}_{self._n:03d}.png"
        for i in range(retries):
            with p.open("wb") as f:
                subprocess.run(
                    [self.adb, "-s", self.serial, "exec-out", "screencap", "-p"],
                    stdout=f, stderr=subprocess.DEVNULL,
                )
            if p.stat().st_size >= 10_000:
                return p
            # 地图/战斗过场会出全黑小帧，等一拍重截，不要直接抛
            time.sleep(1.5 + i)
        raise NavError("截图异常（全黑/很小），模拟器可能在过场或掉线")

    # ---- OCR ----

    def ocr(self, path: Path) -> list[tuple[str, tuple[float, float, float, float]]]:
        img = cv2.imread(str(path))
        if img is None:
            raise NavError(f"读不了截图 {path}")
        res, _ = self._ocr(img)
        out = []
        for item in res or []:
            box, text, _score = item
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            out.append((text.strip(), (min(xs), min(ys), max(xs), max(ys))))
        return out

    def look(self, tag: str = "now") -> tuple[Path, list]:
        p = self.shot(tag)
        return p, self.ocr(p)


# ───────────────────────── 页面识别 ─────────────────────────

def texts_of(rows) -> list[str]:
    return [t for t, _ in rows]


def has(rows, *needles: str) -> bool:
    joined = "|".join(texts_of(rows))
    return all(n in joined for n in needles)


def classify(rows) -> str:
    ts = texts_of(rows)
    joined = "|".join(ts)
    if "行动结束" in joined or "MISSION RESULTS" in joined or "MISSION RESULT" in joined:
        return "result"
    if "开始行动" in joined and "OPERATION" in joined:
        return "detail"
    # 顺序要紧：hub 和 map 都有「观测站后勤处/圣巡之旅」，用各自独有文字区分；
    # 2026-11-22 就是 hub 被当成 map，来回点返回把状态搞丢的。
    if "情报凭证" in joined or "喀兰贸易" in joined:
        return "hub"
    if "积雪清理" in joined:
        return "map"
    if "采购中心" in joined:
        return "home"
    if "数据增补条" in joined or "剿灭作战" in joined:
        return "terminal"
    return "unknown"


def find_text(rows, *cands: str, xmin=0, xmax=1280, ymin=0, ymax=720):
    """找第一段与候选完全相等的文本，返回 (text, box)。"""
    for t, box in rows:
        if t in cands and box[0] >= xmin and box[2] <= xmax and box[1] >= ymin and box[3] <= ymax:
            return t, box
    return None, None


def detect_area(rows) -> str | None:
    """从当前活动图上的关卡标签判断是哪个区域（main/ex/s）。"""
    ts = texts_of(rows)
    if any(t.startswith("OS-EX-") for t in ts):
        return "ex"
    if any(t.startswith("OS-S-") for t in ts):
        return "s"
    if any(re.fullmatch(r"OS-\d+", t) for t in ts):
        return "main"
    if any(t.startswith("OS-ST-") or t.startswith("OS-TR-") for t in ts):
        return "main"
    return None


# ───────────────────────── 导航 ─────────────────────────

def sanity_on_screen(rows) -> tuple[int, int] | None:
    """主界面/详情页右上角「86 理智/210」→ (当前, 上限)；读不到返回 None。

    2026-11-22 踩过：只按「在 /210 上方」挑数字会读到别处的 247（主界面顶部数字多）。
    现在要求数字框与「理智/xxx」框横向重叠，再取纵向最近的那个。
    """
    cap = None
    capbox = None
    for t, box in rows:
        m = re.search(r"理智\s*/\s*(\d{2,3})", t)
        if m:
            cap = int(m.group(1))
            capbox = box
            break
    if cap is None:
        return None
    best = None
    best_dy = 1e9
    for t, box in rows:
        if not re.fullmatch(r"\d{1,3}", t):
            continue
        # 数字框和「理智/xxx」框要有横向重叠
        if min(box[2], capbox[2]) - max(box[0], capbox[0]) <= 0:
            continue
        dy = capbox[1] - box[3]  # 数字在标签上方
        if -5 <= dy <= 120 and dy < best_dy:
            best_dy = dy
            best = int(t)
    if best is None:
        return None
    return best, cap


class Navigator:
    def __init__(self, game: Game, cfg: dict, log=print):
        self.g = game
        self.cfg = cfg
        self.log = log
        self.area = None

    # ---- 客户端守卫（这台模拟器官服/B服/日服都装了，别的客户端在跑很容易被
    # MAA 的 startup 模板误唤醒 —— 2026-11-22 05:32 就串到官服号上浪费了 30 理智）----

    def ensure_client(self) -> None:
        pkg = self.cfg["client_package"]
        others = self.cfg.get("other_clients", [])
        for other in others:
            if other and other != pkg:
                self.g._run("shell", "am", "force-stop", other)
        if self.g.foreground_package() != pkg:
            self.log(f"    [nav] 前台不是 {pkg}，用 adb 拉起来")
            self.g._run("shell", "monkey", "-p", pkg,
                        "-c", "android.intent.category.LAUNCHER", "1")
            time.sleep(18)
        fg = self.g.foreground_package()
        if fg != pkg:
            raise NavError(f"客户端没起来：前台={fg}，期望={pkg}")

    # ---- 入口链 ----

    def to_home(self, reason: str = "") -> None:
        """把游戏带到主界面（能退就退，退不动就让 picker 关游戏重开）。"""
        if reason:
            self.log(f"    [nav] {reason} → 回主界面")
        for step in range(6):
            p, rows = self.g.look("homeflow")
            page = classify(rows)
            self.log(f"    [nav] {page}")
            if page == "home":
                return
            if page == "result":
                self.dismiss_result()
                continue
            if page == "detail":
                self.g.tap(BTN_BACK)
                time.sleep(1.6)
                continue
            if page == "map":
                self.g.tap(BTN_BACK)  # 地图 → hub
                time.sleep(1.6)
                continue
            if page == "hub":
                self.g.tap(BTN_BACK)  # hub → terminal
                time.sleep(1.6)
                continue
            if page == "terminal":
                self.g.tap(BTN_BACK)  # terminal → home
                time.sleep(1.6)
                continue
            self.g.back()
            time.sleep(1.5)
        # 还不认：交给 picker 重启游戏
        self.log("    [nav] 退不出来，关游戏重开")
        self.api_post("/api/nav/restart", {})
        self.api_post("/api/nav/home", {})
        time.sleep(2)

    def to_map(self, area: str) -> None:
        """从任意页面走到指定区域的活动地图。"""
        hub_xy = self.cfg["areas"][area]["hub"]
        tab_xy = self.cfg["areas"][area]["tab"]
        for step in range(8):
            p, rows = self.g.look("mapflow")
            page = classify(rows)
            self.log(f"    [nav] 第 {step + 1} 跳：{page}")
            if page == "home":
                self.g.tap(self.cfg["ui"]["terminal"])
                time.sleep(2.4)
                continue
            if page == "terminal":
                self.g.tap(self.cfg["ui"]["event_card"])
                time.sleep(2.8)
                continue
            if page == "hub":
                self.g.tap(hub_xy)
                time.sleep(3.0)
                continue
            if page == "map":
                self.g.tap(tab_xy)  # 切到目标区域（点当前区域也无害）
                time.sleep(2.4)
                self.area = area
                return
            if page == "detail":
                self.g.tap(BTN_BACK)
                time.sleep(1.6)
                continue
            if page == "result":
                self.dismiss_result()
                continue
            # 认不出：退一级再认
            self.g.back()
            time.sleep(1.6)
        raise NavError("8 跳没到活动地图：" + " → ".join(texts_of(rows))[:120])

    # ---- 关卡定位 ----

    def _node_tap_point(self, box):
        """标签文本左边 34px 是六边形图标中心（点标签白条也有反应，但图标更稳）。"""
        x0, y0, x1, y1 = box
        return (x0 - 34, (y0 + y1) / 2)

    def _scan_for(self, code: str):
        p, rows = self.g.look("scan")
        hit, box = find_text(rows, code, xmin=60, xmax=1230, ymin=100, ymax=520)
        if hit:
            return self._node_tap_point(box), p
        return None, p

    def on_detail_of(self, code: str, rows) -> bool:
        joined = "|".join(texts_of(rows))
        if "开始行动" not in joined:
            return False
        # 详情页右上角的关卡代号
        _t, box = find_text(rows, code, xmin=820, xmax=1280, ymin=60, ymax=200)
        return box is not None

    def ensure_area(self, area: str) -> None:
        """只要保证「当前停在指定区域的活动图」。

        2026-11-22 用户点名要的优化：不是每关都退回主界面再走一遍
        终端→hub→地图。打完一关游戏本来就在活动图上，直接留在原地滑到下一关即可；
        只有页面被带跑（detail/结算/unknown）才退一级，真的回不去了才回主界面。
        """
        for step in range(6):
            p, rows = self.g.look("area")
            page = classify(rows)
            if page == "map":
                cur = detect_area(rows)
                if cur == area:
                    self.area = area
                    return
                self.log(f"    [nav] 当前是 {cur or '?'} 图，切到 {area}")
                self.g.tap(self.cfg["areas"][area]["tab"])
                time.sleep(2.4)
                p, rows = self.g.look("area2")
                if classify(rows) == "map" and detect_area(rows) == area:
                    self.area = area
                    return
                continue
            if page == "detail":
                self.log(f"    [nav] 在详情页，退一级")
                self.g.tap(BTN_BACK)
                time.sleep(1.5)
                continue
            if page == "result":
                self.dismiss_result()
                continue
            if page == "hub":
                self.log(f"    [nav] 在活动 hub，进 {area} 图")
                self.g.tap(self.cfg["areas"][area]["hub"])
                time.sleep(3.0)
                continue
            if page == "terminal":
                self.g.tap(self.cfg["ui"]["event_card"])
                time.sleep(2.6)
                continue
            if page == "home":
                self.log("    [nav] 在主界面，去终端")
                self.g.tap(self.cfg["ui"]["terminal"])
                time.sleep(2.4)
                continue
            # 认不出的页面（任务失败/过场/弹层）：退一级再看
            self.g.back()
            time.sleep(1.6)
        raise NavError(f"切不到 {area} 活动图")

    def _scan_for(self, code: str):
        p, rows = self.g.look("scan")
        hit, box = find_text(rows, code, xmin=60, xmax=1230, ymin=100, ymax=560)
        if hit:
            return self._node_tap_point(box), p
        # 锁着/刚解锁的标签是灰底灰字，普通 OCR 会漏（2026-11-22：OS-EX-4 就在屏幕上却没认出来）。
        # 做一次灰度 + CLAHE 对比度增强再认一遍。
        try:
            img = cv2.imread(str(p))
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            enh = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
            res, _ = self.g._ocr(cv2.cvtColor(enh, cv2.COLOR_GRAY2BGR))
            rows2 = []
            for item in res or []:
                box_pts, text = item[0], item[1]
                xs = [q[0] for q in box_pts]
                ys = [q[1] for q in box_pts]
                rows2.append((text.strip(), (min(xs), min(ys), max(xs), max(ys))))
            hit, box = find_text(rows2, code, xmin=60, xmax=1230, ymin=100, ymax=560)
            if hit:
                self.log(f"    [nav] 对比度增强后认出了 {code}")
                return self._node_tap_point(box), p
        except Exception:  # noqa: BLE001
            pass
        return None, p

    def _refresh_map(self, area: str) -> None:
        """退回 hub 再进一次区域图：刚通关解锁的下一关，原地地图有时不刷新（EX-4 实测）。"""
        self.log("    [nav] 回 hub 重进地图刷新解锁状态")
        self.g.tap(BTN_BACK)      # 地图 → hub
        time.sleep(1.8)
        self.g.tap(self.cfg["areas"][area]["hub"])  # hub → 区域图
        time.sleep(3.0)

    def ensure_visible(self, code: str, area: str):
        """只把关卡标签滑到视野里（不点），返回可点的坐标 —— 之后交给 MAA 的 find_stage。"""
        self.ensure_area(area)
        for _round in range(2):
            # 先原地扫两次（这个活动的地图一屏能放下，乱滑反而把标签滑出去）
            for _ in range(2):
                point, _ = self._scan_for(code)
                if point:
                    return point
            self._refresh_map(area)
            point, _ = self._scan_for(code)
            if point:
                return point
            # 还找不到：本地图是长条时左右滑着找
            for _i in range(6):
                self.log(f"    [nav] 视野里没有 {code}，向右滑一屏")
                self.g.swipe(1000, 430, 380, 430, 400)
                time.sleep(1.1)
                point, _ = self._scan_for(code)
                if point:
                    return point
            self.log("    [nav] 扫到底了，滑回最左重扫")
            for _ in range(12):
                self.g.swipe(380, 430, 1000, 430, 300)
                time.sleep(0.6)
        raise NavError(f"地图上找不到 {code}（可能还没解锁）")

    def open_stage(self, code: str, area: str) -> None:
        """走到 code 的详情页并校验；找不到就抛 NavError。"""
        self.ensure_area(area)
        for round_ in range(2):
            point, _ = self._scan_for(code)
            if not point:
                self._refresh_map(area)
                point, _ = self._scan_for(code)
            if point:
                self.g.tap(point)
                time.sleep(2.6)
                p, rows = self.g.look("detail")
                if classify(rows) == "detail" and self.on_detail_of(code, rows):
                    self.log(f"    [nav] 已在 {code} 详情页")
                    return
                self.log(f"    [nav] 点到别处（{classify(rows)}），退回重找")
                self.g.tap(BTN_BACK)
                time.sleep(1.6)
                continue
            # 视野里没有：长条地图往右滑一屏
            for _i in range(6):
                self.log(f"    [nav] 视野里没有 {code}，向右滑一屏")
                self.g.swipe(1000, 430, 380, 430, 400)
                time.sleep(1.2)
                point, _ = self._scan_for(code)
                if point:
                    break
            if not point:
                self.log("    [nav] 扫到底了，滑回最左重扫")
                for _ in range(10):
                    self.g.swipe(380, 430, 1000, 430, 300)
                    time.sleep(0.7)
        raise NavError(f"地图上找不到 {code}（可能还没解锁）")

    # ---- 已通关探测 ----

    # 复刻活动里去年通过的关不会再弹提示，地图节点样式也一模一样（2026-11-22 逐像素比过）。
    # 试过拿「代理指挥」当通关标记：**不成立**——未通关的关点它也会变白（OS-5/OS-6 实测），
    # 它只是个可勾选开关。所以别再拿它判「已通关」，`stage_cleared` 只留作实验代码。
    CB_BOX = (1052, 579, 1082, 608)
    CB_CENTER = (1067, 593)

    def _cb_filled(self, path: Path) -> bool:
        img = cv2.imread(str(path))
        if img is None:
            return False
        x0, y0, x1, y1 = self.CB_BOX
        box = img[y0:y1, x0:x1]
        white = int(((box[:, :, 0] > 220) & (box[:, :, 1] > 220) & (box[:, :, 2] > 220)).sum())
        return white >= 200

    def stage_cleared(self, code: str, area: str) -> bool | None:
        """探测关卡是否 3 星通关（点「代理指挥」再点回来）。判不出返回 None。"""
        self.open_stage(code, area)
        self.g.shot("cb0")
        self.g.tap(self.CB_CENTER)
        time.sleep(1.2)
        filled = self._cb_filled(self.g.shot("cb1"))
        if filled:
            self.g.tap(self.CB_CENTER)  # 还原，别把代理指挥留在开着的状态
            time.sleep(0.8)
        return filled

    # ---- 结算页 ----
    def dismiss_result(self, tries: int = 8) -> None:
        for _ in range(tries):
            p, rows = self.g.look("settle")
            txt = "|".join(texts_of(rows))
            if "是否将此阵容保存为代理指挥阵容" in txt or "保存为代理指挥阵容" in txt:
                self.g.tap(BTN_SAVE_FORMATION)  # 保存，代理指挥以后可用
                time.sleep(1.6)
                continue
            # 「理智不足 → 补充理智」弹层（点开始行动时理智不够会弹这个）
            if "清空选择" in txt or "使用药剂恢复" in txt or "至纯源石恢复" in txt:
                self.log("    [nav] 关掉理智补充弹层")
                self.g.tap((833, 668))  # X
                time.sleep(1.4)
                continue
            # 「任务失败 → 点击画面以继续」页：点一下进 0 星结算
            if "任务失败" in txt or "MISSION FAILED" in txt:
                self.g.tap((640, 360))
                time.sleep(2.6)
                continue
            if classify(rows) == "result":
                self.g.tap(BTN_CONFIRM)
                time.sleep(2.0)
                continue
            return

    # ---- picker API ----

    def api(self, path: str) -> str:
        return self.cfg["api"] + path

    def api_get(self, path: str) -> dict:
        with urllib.request.urlopen(self.api(path), timeout=30) as r:
            return json.load(r)

    def api_post(self, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            self.api(path), data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)

    def search_jobs(self, code: str, stage_name: str, limit: int = 8) -> list[dict]:
        body = {"stageCode": code, "stageName": stage_name, "limit": limit}
        out = self.api_post("/api/jobs/search", body)
        jobs = [j for j in out.get("jobs", []) if j.get("mode") == "ready" and j.get("met")]
        return jobs

    def start_run(self, code: str, stage_name: str, job_ids: list[int], mode: str = "normal",
                  timeout_min: int = 25, on_map: bool = True) -> dict:
        stage = {"stageCode": code, "stageName": stage_name, "jobIds": job_ids}
        if mode == "raid":
            stage["raidJobIds"] = job_ids
        body = {
            "accountId": self.cfg["account"],
            "client": self.cfg.get("client", ""),
            "stages": [stage],
            "manualMode": not on_map,
            "onMap": on_map,
            "formation": True,
            "addTrust": True,
            "ignoreRequirements": True,
            "maxAttempts": len(job_ids),
            "supportUnitUsage": 0,
            "timeoutMin": timeout_min,
        }
        return self.api_post("/api/run/start", body)

    def wait_run(self, run_id: str, timeout: int = 1500, log=print) -> dict:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            time.sleep(6)
            try:
                snap = self.api_get(f"/api/run/{run_id}")
            except Exception:  # noqa: BLE001
                continue
            st = snap.get("status")
            if st and st != last:
                log(f"    [run] {st} attempt={snap.get('attempt')}/{snap.get('total')}")
                last = st
            if snap.get("finishedAt"):
                return snap
        return {"status": "timeout"}
