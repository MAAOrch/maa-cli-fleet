from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .models import BenchUnit, SceneType, ShopCard, ShopState

# Exact 1280x720 ROIs ported from MAA v6.23.1 StrongholdProtocol.json
ROI_READY_BTN = (1125, 85, 140, 60)
ROI_SHOP_GOLD = (1173, 507, 80, 43)
ROI_SHOP_ROUND = (485, 21, 33, 31)
ROI_SHOP_SLOTS = (1241, 377, 24, 28)
ROI_SHOP_UPGRADE_COST = (0, 513, 160, 120)
ROI_SHOP_BOTTOM = (0, 636, 507, 66)
ROI_SHOP_CARDS = (120, 513, 1013, 153)
ROI_SHOP_CARD_NAMES = (120, 666, 1013, 44)
ROI_SHOP_EXPANDED = (1100, 644, 150, 59)
ROI_CONFIRM_READY = (430, 255, 420, 110)
ROI_SETTLEMENT = (65, 156, 450, 160)
ROI_BATTLE_ROUND = (340, 8, 180, 60)
ROI_LOBBY_SOLO_QUICK = (680, 540, 260, 180)
ROI_LOBBY_TABS = (880, 500, 360, 110)
ROI_BENCH_SCAN = (120, 380, 930, 35)
ROI_SHOP_SELL = (1080, 480, 200, 240)

SHOP_BUY_Y = 681
SHOP_CARD_Y = 610
SHOP_COST_Y = 527
SHOP_PACT_Y = 645
SHOP_NAME_Y = 672
REFRESH_CLICK = (1191, 457)
READY_CLICK = (1187, 113)

# HSV ranges for Tier I..VI badges ported from MAA v6.23.1 tier_badges.json
TIER_HSV_RANGES: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = [
    ((0, 0, 48), (179, 70, 215)),      # Tier I
    ((0, 0, 100), (179, 70, 255)),     # Tier II
    ((75, 90, 50), (87, 255, 255)),    # Tier III (green)
    ((90, 90, 50), (101, 255, 255)),   # Tier IV (cyan/blue)
    ((21, 95, 22), (29, 255, 255)),    # Tier V (gold)
    ((12, 95, 50), (19, 255, 255)),    # Tier VI (orange)
]


def crop_roi(img: np.ndarray, roi: tuple[int, int, int, int]) -> np.ndarray:
    h, w = img.shape[:2]
    x, y, rw, rh = roi
    x1 = max(0, min(w, x))
    y1 = max(0, min(h, y))
    x2 = max(x1, min(w, x + rw))
    y2 = max(y1, min(h, y + rh))
    return img[y1:y2, x1:x2]


def ensure_720p(img: np.ndarray) -> np.ndarray:
    if img.shape[1] == 1280 and img.shape[0] == 720:
        return img
    return cv2.resize(img, (1280, 720), interpolation=cv2.INTER_AREA)


def match_tier_badge(patch_bgr: np.ndarray) -> int:
    """Classify a 26x26 tier badge crop into Tier 1..6 using MAA's HSV palette.

    Returns 0 if no tier badge is detected.
    """
    if patch_bgr.size == 0:
        return 0
    h, w = patch_bgr.shape[:2]
    ix, iy = max(1, int(w * 0.14)), max(1, int(h * 0.20))
    inner = patch_bgr[iy : max(iy + 1, h - iy), ix : max(ix + 1, w - ix)]
    if inner.size == 0:
        return 0
    hsv = cv2.cvtColor(inner, cv2.COLOR_BGR2HSV)
    total = float(hsv.shape[0] * hsv.shape[1])

    # Check chromatic tiers VI -> III first (unambiguous hue bands)
    for tier in (6, 5, 4, 3):
        lo, hi = TIER_HSV_RANGES[tier - 1]
        mask = cv2.inRange(hsv, np.array(lo, dtype=np.uint8), np.array(hi, dtype=np.uint8))
        frac = float(np.count_nonzero(mask)) / total
        if frac >= 0.08:
            return tier

    # Fall back to bright grayscale Tiers II / I by mean value
    v_channel = hsv[:, :, 2]
    s_channel = hsv[:, :, 1]
    gray_mask = s_channel <= 70
    if float(np.count_nonzero(gray_mask)) / total >= 0.5:
        hi_v = float(np.percentile(v_channel, 90))
        if hi_v >= 175:
            return 2
        if hi_v >= 115:
            return 1
    return 0


class SpaPerception:
    """Vision & OCR frontend for Stronghold Protocol Regulation (1280x720)."""

    def __init__(self, ocr_engine: Any | None = None):
        self._ocr = ocr_engine

    def _get_ocr(self) -> Any:
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR

            self._ocr = RapidOCR()
        return self._ocr

    def ocr_roi(self, img: np.ndarray, roi: tuple[int, int, int, int]) -> list[tuple[str, tuple[int, int, int, int], float]]:
        crop = crop_roi(img, roi)
        if crop.size == 0:
            return []
        result, _ = self._get_ocr()(crop)
        if not result:
            return []
        ox, oy = roi[0], roi[1]
        items: list[tuple[str, tuple[int, int, int, int], float]] = []
        for box, text, score in result:
            xs = [int(p[0]) for p in box]
            ys = [int(p[1]) for p in box]
            x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
            items.append((str(text).strip(), (ox + x1, oy + y1, x2 - x1, y2 - y1), float(score)))
        return items

    def read_int(self, img: np.ndarray, roi: tuple[int, int, int, int]) -> int | None:
        items = self.ocr_roi(img, roi)
        for text, _, _ in items:
            m = re.search(r"\d+", text)
            if m:
                return int(m.group(0))
        return None

    def detect_scene(self, img_raw: np.ndarray) -> SceneType:
        img = ensure_720p(img_raw)

        ready_texts = [t for t, _, _ in self.ocr_roi(img, ROI_READY_BTN)]
        if any("准备就绪" in t or "准备" in t for t in ready_texts):
            return SceneType.PREP_SHOP

        confirm_texts = [t for t, _, _ in self.ocr_roi(img, ROI_CONFIRM_READY)]
        if any(k in t for t in confirm_texts for k in ("确认准备", "资金将在", "临时整备区")):
            return SceneType.CONFIRM_READY

        settle_texts = [t for t, _, _ in self.ocr_roi(img, ROI_SETTLEMENT)]
        if any(k in t for t in settle_texts for k in ("模拟完成", "模拟中止", "模拟结束", "返回主页")):
            return SceneType.SETTLEMENT

        quick_texts = [t for t, _, _ in self.ocr_roi(img, ROI_LOBBY_SOLO_QUICK)]
        if any("极速模拟" in t or "规程等级" in t for t in quick_texts):
            return SceneType.LOBBY_SOLO

        tab_texts = [t for t, _, _ in self.ocr_roi(img, ROI_LOBBY_TABS)]
        if any("联合模拟" in t or "个人模拟" in t for t in tab_texts):
            return SceneType.LOBBY_COOP

        battle_texts = [t for t, _, _ in self.ocr_roi(img, ROI_BATTLE_ROUND)]
        if any("回合" in t for t in battle_texts):
            return SceneType.BATTLE

        return SceneType.UNKNOWN

    def parse_shop(self, img_raw: np.ndarray) -> ShopState:
        img = ensure_720p(img_raw)
        scene = self.detect_scene(img)
        state = ShopState(scene=scene)
        if scene != SceneType.PREP_SHOP:
            return state

        state.round_num = self.read_int(img, ROI_SHOP_ROUND) or 0
        state.gold = self.read_int(img, ROI_SHOP_GOLD) or 0
        state.deploy_slots_free = self.read_int(img, ROI_SHOP_SLOTS) or 0

        # Check upgrade button / maxed state on bottom-left
        bottom_items = self.ocr_roi(img, ROI_SHOP_BOTTOM)
        for text, rect, _ in bottom_items:
            state.raw_texts.append(text)
            if any(k in text for k in ("已满级", "满级", "MAX")):
                state.shop_maxed = True
                state.shop_level = 6
                state.upgrade_x = rect[0] + rect[2] // 2
            elif any(k in text for k in ("升级", "升阶", "扩容")):
                state.upgrade_x = rect[0] + rect[2] // 2

        if not state.shop_maxed:
            if state.upgrade_x:
                cost_roi = (max(0, state.upgrade_x - 35), 515, 70, 50)
                state.upgrade_cost = self.read_int(img, cost_roi)
            if state.upgrade_cost is None:
                state.upgrade_cost = self.read_int(img, ROI_SHOP_UPGRADE_COST)

        # Parse shop cards (names + pacts + costs + tier badges)
        name_items = self.ocr_roi(img, ROI_SHOP_CARD_NAMES)
        card_items = self.ocr_roi(img, ROI_SHOP_CARDS)

        # Filter out upgrade button region if upgrade_x is known
        min_card_x = (state.upgrade_x + 95) if state.upgrade_x else 160
        cards: list[ShopCard] = []
        for idx, (text, rect, _score) in enumerate(sorted(name_items, key=lambda it: it[1][0])):
            cx = rect[0] + rect[2] // 2
            if cx < min_card_x or len(text) < 1:
                continue
            # Skip non-operator button labels
            if any(skip in text for skip in ("升级", "收起", "已满级", "刷新", "购买")):
                continue
            # Look for cost number near top of this card column
            cost = 0
            pact = ""
            for ct, cr, _ in card_items:
                ccx = cr[0] + cr[2] // 2
                if abs(ccx - cx) <= 65:
                    if cr[1] <= 565 and re.fullmatch(r"\d+", ct):
                        cost = int(ct)
                    elif cr[1] >= 625 and not re.fullmatch(r"\d+", ct):
                        pact = ct

            # Tier badge is offset [-71, 529, 26, 26] relative to card center x
            tier_patch = crop_roi(img, (cx - 71, 529, 26, 26))
            tier = match_tier_badge(tier_patch)
            cards.append(
                ShopCard(
                    slot_idx=len(cards),
                    x=cx,
                    name=text,
                    pact=pact,
                    cost=cost,
                    tier=tier,
                    is_free_reward=(cost == 0),
                )
            )

        state.cards = cards
        # Three 0-cost cards simultaneously indicates a promotion reward shop
        if len(cards) == 3 and all(c.cost == 0 for c in cards):
            state.is_reward_shop = True

        state.bench = self._detect_bench_slots(img)
        return state

    def _detect_bench_slots(self, img: np.ndarray) -> list[BenchUnit]:
        """Detect occupied bench slots along ROI_BENCH_SCAN using saturation/edge energy."""
        units: list[BenchUnit] = []
        # 8 evenly spaced bench slots across x=140..1000, y~420
        slot_centers_x = [175, 285, 395, 505, 615, 725, 835, 945]
        for idx, sx in enumerate(slot_centers_x):
            patch = crop_roi(img, (sx - 28, 385, 56, 36))
            if patch.size == 0:
                continue
            hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
            # Empty bench slots are dark/low-variance; occupied slots have distinct portrait color/edges
            std_v = float(np.std(hsv[:, :, 2]))
            mean_s = float(np.mean(hsv[:, :, 1]))
            if std_v >= 28.0 or mean_s >= 42.0:
                # Gold/bright top corner suggests promoted (elite) border
                gold_mask = cv2.inRange(hsv, np.array([15, 110, 140], np.uint8), np.array([34, 255, 255], np.uint8))
                elite = (float(np.count_nonzero(gold_mask)) / float(patch.shape[0] * patch.shape[1])) >= 0.08
                units.append(BenchUnit(slot_idx=idx, x=sx, y=425, elite=elite, occupied=True))
        return units


def load_image(path: str | Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"Failed to decode image: {path}")
    return img
