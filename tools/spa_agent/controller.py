from __future__ import annotations

import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

from .perception import READY_CLICK, REFRESH_CLICK, ROI_SHOP_SELL, SHOP_BUY_Y, SHOP_CARD_Y


def has_active_mas_lock(locks_dir: str, max_age_sec: int = 180) -> str:
    if not locks_dir:
        return ""
    p = Path(locks_dir)
    if not p.is_dir():
        return ""
    cutoff = time.time() - max_age_sec
    try:
        for f in p.glob("*.lock"):
            try:
                if f.stat().st_mtime >= cutoff:
                    return f.name
            except OSError:
                continue
    except OSError:
        return ""
    return ""


class AdbController:
    """Thin ADB execution layer for 1280x720 MuMu instances."""

    def __init__(self, adb_path: str, addr: str):
        self.adb = adb_path
        self.addr = addr

    def connect(self) -> bool:
        r = subprocess.run([self.adb, "connect", self.addr], capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and ("connected" in r.stdout.lower() or "already" in r.stdout.lower())

    def screencap(self) -> np.ndarray:
        r = subprocess.run(
            [self.adb, "-s", self.addr, "exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=10,
        )
        if r.returncode != 0 or not r.stdout:
            raise RuntimeError(f"screencap failed (rc={r.returncode})")
        arr = np.frombuffer(r.stdout, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("screencap PNG decode failed")
        return img

    def click(self, x: int, y: int, delay: float = 0.45) -> None:
        subprocess.run(
            [self.adb, "-s", self.addr, "shell", "input", "tap", str(int(x)), str(int(y))],
            capture_output=True,
            timeout=10,
        )
        if delay > 0:
            time.sleep(delay)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 400, delay: float = 0.5) -> None:
        subprocess.run(
            [
                self.adb,
                "-s",
                self.addr,
                "shell",
                "input",
                "swipe",
                str(int(x1)),
                str(int(y1)),
                str(int(x2)),
                str(int(y2)),
                str(int(duration_ms)),
            ],
            capture_output=True,
            timeout=10,
        )
        if delay > 0:
            time.sleep(delay)

    def buy_card(self, x: int) -> None:
        # Select card then click Buy button below it (mirrors MAA StrongholdProtocol)
        self.click(x, SHOP_CARD_Y, delay=0.35)
        self.click(x, SHOP_BUY_Y, delay=0.55)

    def upgrade_shop(self, x: int) -> None:
        self.click(x, 665, delay=0.6)

    def refresh_shop(self) -> None:
        self.click(REFRESH_CLICK[0], REFRESH_CLICK[1], delay=0.65)

    def deploy_from_bench(self, from_x: int, from_y: int, to_x: int, to_y: int) -> None:
        self.swipe(from_x, from_y, to_x, to_y, duration_ms=450, delay=0.55)

    def sell_from_bench(self, from_x: int, from_y: int) -> None:
        sx = ROI_SHOP_SELL[0] + ROI_SHOP_SELL[2] // 2
        sy = ROI_SHOP_SELL[1] + ROI_SHOP_SELL[3] // 2
        self.swipe(from_x, from_y, sx, sy, duration_ms=450, delay=0.55)

    def click_ready(self) -> None:
        self.click(READY_CLICK[0], READY_CLICK[1], delay=0.6)

    def click_confirm_dialog(self) -> None:
        # Standard right-side confirm button in Stronghold Protocol confirmation dialog
        self.click(860, 505, delay=0.6)
