# -*- coding: utf-8 -*-
"""设备小工具：客户端隔离 + 截图读理智。

为什么单独一个文件：UI 操作已经全部交给 MaaFw PI（`pi/arknights-event`），Python 这边
只留两类「不该由 pipeline 管」的事：
1. 客户端隔离——模拟器里官服/B服/日服/国际服可能同时装着，别的服在前台时 MaaFw/MaaCore
   的模板不认客户端，会把别的号当成目标号（2026-11-22 05:32 官服白打 3 场）；
2. 读理智——纯读屏（adb screencap + RapidOCR），不存在点错的风险。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import cv2

KNOWN_PACKAGES = [
    "com.hypergryph.arknights",          # 官服
    "com.hypergryph.arknights.bilibili",  # B服
    "com.YoStarJP.Arknights",            # 日服
    "com.YoStarEN.Arknights",            # 国际服
    "com.YoStarKR.Arknights",            # 韩服
]

# 截图落盘目录：默认放在本仓/worktree 内（双开时主仓与 worktree 各写各的），
# 可用 PICKER_SHOT_DIR 覆盖。
_SHOT = Path(os.environ.get("PICKER_SHOT_DIR") or (Path(__file__).resolve().parents[2] / "_tmp" / "ev"))
_OCR = None


def _adb(cfg: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [cfg["adb"], "-s", cfg["device"], *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def foreground_package(cfg: dict) -> str:
    out = _adb(cfg, "shell", "dumpsys window | grep mCurrentFocus").stdout or ""
    m = re.search(r"u0 ([A-Za-z0-9_.]+)/", out)
    return m.group(1) if m else ""


def ensure_only_client(cfg: dict, log=print) -> None:
    """关掉非目标客户端，并确保目标客户端在前台。"""
    keep = cfg["client_package"]
    for pkg in KNOWN_PACKAGES:
        if pkg != keep:
            _adb(cfg, "shell", "am", "force-stop", pkg)
    fg = foreground_package(cfg)
    if fg != keep:
        log(f"    [client] 前台是 {fg or '未知'}，拉起 {keep}")
        _adb(cfg, "shell", "monkey", "-p", keep, "-c", "android.intent.category.LAUNCHER", "1")
        time.sleep(18)
    fg = foreground_package(cfg)
    if fg != keep:
        raise RuntimeError(f"目标客户端没在前台：{fg}（期望 {keep}）")


def page_hint(cfg: dict) -> str:
    """只读判断当前页面类型：home / map / detail / other。

    用途：手动模式推关前决定「要不要先导航到区域图」，避免从主界面直接调
    「进入S-N详情」白等 180 秒超时（2026-11-22 反复踩到）。
    """
    path = screenshot(cfg, "page_hint")
    img = cv2.imread(str(path))
    res, _ = _ocr()(img)
    texts = [item[1].strip() for item in (res or [])]
    joined = "|".join(texts)
    if "采购中心" in joined:
        return "home"
    if "开始行动" in joined or "开始突袭" in joined:
        return "detail"
    for t in texts:
        # OCR 常把 OS- 认成 0S-（MAA 也有这毛病），这里两种都收
        if re.fullmatch(r"[O0]S-(EX-|S-)?\d+", t) or t in ("OS-ST-1", "OS-ST-2", "OS-ST-3", "OS-ST-4", "OS-TR-1"):
            return "map"
    return "other"


def _online_devices(cfg: dict) -> set[str]:
    try:
        out = subprocess.run([cfg["adb"], "devices"], capture_output=True, text=True,
                             timeout=20, encoding="utf-8", errors="replace").stdout
    except Exception:  # noqa: BLE001
        return set()
    devs = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device" and parts[0].startswith("127.0.0.1:"):
            devs.add(parts[0])
    return devs


def resolve_device(cfg: dict, log=print) -> str:
    """把 cfg["device"] 校正成真正在线的 adb 地址。

    坑（2026-11-22 踩到）：MuMu 实例重启后 adb 端口会漂（16384 → 16385），配置里写死的
    端口就永远 offline。这里先问 MuMuManager 要实时 adb_port；拿不到再退回「唯一在线的
    本机设备」。PI 那边按设备名匹配，不受影响；这里只修 Python/adb 侧和日志。
    """
    want = cfg.get("device", "")
    online = _online_devices(cfg)
    if want in online:
        return want
    candidates: list[str] = []
    # 双开：没显式给实例号就**不猜**（以前默认 0 会悄悄查实例 0 的端口，把 B 会话带偏）
    emu = str(cfg.get("emu", "") or "").strip()
    mm = os.path.join(os.path.dirname(cfg.get("adb", "")), "MuMuManager.exe")
    if emu and os.path.exists(mm):
        try:
            out = subprocess.run(
                [mm, "info", "-v", emu],
                capture_output=True, text=True, timeout=20,
                encoding="utf-8", errors="replace",
            ).stdout
            m = re.search(r'"adb_port"\s*:\s*(\d+)', out)
            if m:
                candidates.append("127.0.0.1:" + m.group(1))
        except Exception:  # noqa: BLE001
            pass
    locals_online = sorted(d for d in online if d.startswith("127.0.0.1:"))
    if len(locals_online) == 1:
        candidates.append(locals_online[0])
    for cand in candidates:
        if cand in online and cand != want:
            log(f"    [device] adb 端口漂移：{want} 不在线，改用 {cand}（MuMu 重启后端口会变）")
            cfg["device"] = cand
            return cand
    return want


def _ocr():
    """惰性初始化 RapidOCR（只用于读屏，不参与点击）。"""
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
    return _OCR


def screenshot(cfg: dict, tag: str = "now", retries: int = 3) -> Path:
    _SHOT.mkdir(parents=True, exist_ok=True)
    p = _SHOT / f"{tag}.png"
    for i in range(retries):
        with p.open("wb") as f:
            subprocess.run(
                [cfg["adb"], "-s", cfg["device"], "exec-out", "screencap", "-p"],
                stdout=f, stderr=subprocess.DEVNULL,
            )
        if p.stat().st_size >= 10_000:
            return p
        time.sleep(1.5 + i)
    raise RuntimeError("截图异常（全黑/很小），模拟器可能在过场或掉线")


def _plausible_suffix(text: str, cap: int) -> int | None:
    """从 OCR token 里取理智数字。

    RapidOCR 常把「+」按钮和数字粘在一起（实测「+99」→ token `9099`），所以取
    数字串的**最短合法后缀**：先 3 位再 2 位再 1 位，不能有前导零，且 ≤ 上限。
    """
    digits = re.sub(r"\D", "", text or "")
    if not digits:
        return None
    for n in (3, 2, 1):
        if len(digits) < n:
            continue
        s = digits[-n:]
        if n > 1 and s[0] == "0":
            continue
        v = int(s)
        if 0 <= v <= cap:
            return v
    return None


def read_sanity(cfg: dict) -> tuple[int, int] | None:
    """读理智 → (当前, 上限)；认不出返回 None。

    三种页面形态都要认：主界面「99 理智/210」、关卡详情页右上角「53/210」、弹层里的「N/210」。
    """
    path = screenshot(cfg, "sanity")
    img = cv2.imread(str(path))
    res, _ = _ocr()(img)
    rows = []
    for item in res or []:
        box, text = item[0], item[1]
        xs = [q[0] for q in box]
        ys = [q[1] for q in box]
        rows.append((text.strip(), (min(xs), min(ys), max(xs), max(ys))))
    # 1) 同一个 token 里就带「N/210」或「N 理智/210」（上限只认 130~230，防其他 N/M 数字误判）
    for t, _b in rows:
        m = re.search(r"(\d{1,3})\s*(?:理智)?\s*/\s*(\d{2,3})", t)
        if m and 130 <= int(m.group(2)) <= 230 and int(m.group(1)) <= int(m.group(2)):
            return int(m.group(1)), int(m.group(2))
    # 2) 主界面形态：大号数字在固定位置（x≈760-900, y≈125-215）。
    #    整屏 OCR 容易把旁边的「+」图标粘进数字（实测 91 → 191），所以对这块单独再 OCR 一次。
    h, w = img.shape[:2]
    tight = img[115:225, 772:895] if (w, h) == (1280, 720) else None
    if tight is not None and tight.size:
        res2, _ = _ocr()(tight)
        vals = [re.sub(r"\D", "", it[1]) for it in (res2 or [])]
        vals = [re.sub(r"\D", "", v) for v in vals if v]
        nums = [int(v) for v in vals if 0 < len(v) <= 3]
        for v in nums:
            if 0 <= v <= 300:
                # 上限：先找 /210 这类 token 拿上限，找不到按 210 兜底
                cap0 = 210
                for t, _b in rows:
                    m = re.search(r"理智\s*/\s*(\d{2,3})", t)
                    if m and 130 <= int(m.group(1)) <= 230:
                        cap0 = int(m.group(1))
                        break
                if v <= cap0:
                    return v, cap0
    # 2) 找「理智/210」（或单独 "/210"）拿上限。上限只可能是 130~230 这种理智上限值，
    #    不然地图页的「0/100」之类会被误认（2026-11-22 实测读到 (0,100)）。
    cap, capbox = None, None
    for t, b in rows:
        m = re.search(r"理智\s*/\s*(\d{2,3})", t) or re.search(r"/\s*(\d{2,3})", t)
        if m and 130 <= int(m.group(1)) <= 230:
            cap, capbox = int(m.group(1)), b
            break
    if cap is None:
        return None
    # 3) 上限左边、同一行的大号数字（可能是「9099」这种含 + 粘连的）
    best, best_area = None, -1
    for t, b in rows:
        dy = capbox[1] - b[3]
        cx = (b[0] + b[2]) / 2
        # 大号数字框可能和「+」连在一起向右延伸，所以用「框中心在 /210 左侧」判断
        if b[0] < capbox[2] and cx < capbox[0] + 150 and -50 <= dy <= 100 and (b[3] - b[1]) >= 40:
            v = _plausible_suffix(t, cap)
            if v is None:
                continue
            area = (b[2] - b[0]) * (b[3] - b[1])
            if area > best_area:
                best, best_area = v, area
    return (best, cap) if best is not None else None


def swipe_find_stage(cfg: dict, code: str, log=print, max_swipes: int = 8) -> bool:
    """在当前活动图上左右滑动找关卡标签。找到返回 True（不点）。"""
    import time, subprocess
    adb = cfg.get("adb", r"D:\Xiach\MuMuPlayer\nx_main\adb.exe")
    dev = cfg.get("device", "127.0.0.1:16384")
    def swipe(x1, y1, x2, y2):
        subprocess.run([adb, "-s", dev, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), "400"],
                       capture_output=True, timeout=10)
        time.sleep(1.0)
    def has_code() -> bool:
        try:
            from device import page_hint
            return page_hint(cfg) == "map"
        except Exception:
            return False
    # 先原地查
    if has_code():
        log(f"    [scroll] {code} 已在视野")
        return True
    # 向右滑
    for i in range(max_swipes):
        log(f"    [scroll] 向右滑找 {code} ({i+1}/{max_swipes})")
        swipe(1000, 430, 380, 430)
        if has_code():
            return True
    # 滑回最左再向右
    log("    [scroll] 滑回最左")
    for _ in range(max_swipes):
        swipe(380, 430, 1000, 430)
    for i in range(max_swipes):
        log(f"    [scroll] 再向右滑 ({i+1}/{max_swipes})")
        swipe(1000, 430, 380, 430)
        if has_code():
            return True
    return False
