#!/usr/bin/env python3
"""Unit tests for tools/spa_agent (Route B standalone Stronghold Protocol agent)."""

import json
import tempfile
import time
import unittest
from pathlib import Path

import cv2
import numpy as np

from tools.spa_agent.models import ActionKind, BenchUnit, SceneType, ShopCard, ShopState
from tools.spa_agent.perception import match_tier_badge
from tools.spa_agent.policy import SpaPolicyEngine


class TestSpaAgent(unittest.TestCase):
    def test_promote_policy_never_upgrades_and_prioritizes_triples(self):
        eng = SpaPolicyEngine("config/spa_policies/promote_200.json")
        eng.owned_counts["泡普卡"] = 2

        state = ShopState(
            scene=SceneType.PREP_SHOP,
            round_num=5,
            gold=10,
            hp=100,
            shop_level=1,
            upgrade_cost=4,
            upgrade_x=80,
            cards=[
                ShopCard(0, 300, name="泡普卡", cost=1, tier=1),
                ShopCard(1, 500, name="维什戴尔", cost=5, tier=6),
            ],
        )
        act = eng.decide(state)
        self.assertEqual(act.kind, ActionKind.BUY)
        self.assertEqual(act.params["name"], "泡普卡")
        self.assertIn("TRIPLE", act.reason)

        # Even with no cards, promote_200 must NOT upgrade shop; it refreshes instead
        state_empty = ShopState(
            scene=SceneType.PREP_SHOP,
            round_num=5,
            gold=10,
            hp=100,
            shop_level=1,
            upgrade_cost=4,
            upgrade_x=80,
            cards=[],
        )
        act2 = eng.decide(state_empty)
        self.assertEqual(act2.kind, ActionKind.REFRESH)

    def test_promote_policy_sells_elite_bench_to_recycle(self):
        eng = SpaPolicyEngine("config/spa_policies/promote_200.json")
        bench = [
            BenchUnit(0, 175, 425, name="克洛丝", tier=1, elite=False),
            BenchUnit(1, 285, 425, name="泡普卡", tier=1, elite=True),
            BenchUnit(2, 395, 425, name="米格鲁", tier=1, elite=False),
            BenchUnit(3, 505, 425, name="芬", tier=1, elite=False),
            BenchUnit(4, 615, 425, name="芙蓉", tier=1, elite=False),
        ]
        state = ShopState(
            scene=SceneType.PREP_SHOP,
            round_num=3,
            gold=4,
            bench=bench,
            cards=[ShopCard(0, 300, name="芬", cost=1, tier=1)],
        )
        act = eng.decide(state)
        self.assertEqual(act.kind, ActionKind.SELL)
        self.assertEqual(act.params["slot_idx"], 1)  # elite unit sold first

    def test_regulation12_hp_guard_blocks_greedy_upgrade(self):
        eng = SpaPolicyEngine("config/spa_policies/regulation_12.json")
        eng.deployed_count = 4
        # Low HP (25 < 40): must NOT upgrade shop, should buy target-pact stabilizer
        state = ShopState(
            scene=SceneType.PREP_SHOP,
            round_num=5,
            gold=8,
            hp=25,
            shop_level=2,
            upgrade_cost=6,
            upgrade_x=80,
            cards=[ShopCard(0, 400, name="银灰", pact="谢拉格", cost=3, tier=3)],
        )
        act = eng.decide(state)
        self.assertEqual(act.kind, ActionKind.BUY)
        self.assertEqual(act.params["name"], "银灰")

        # High HP (80 >= 40) & safe board: should upgrade shop before mediocre buy
        state.hp = 80
        state.cards = [ShopCard(0, 400, name="普通干员", pact="无", cost=2, tier=2)]
        act2 = eng.decide(state)
        self.assertEqual(act2.kind, ActionKind.UPGRADE_SHOP)

    def test_regulation12_buys_core_operator_even_when_upgrade_available(self):
        eng = SpaPolicyEngine("config/spa_policies/regulation_12.json")
        eng.deployed_count = 4
        state = ShopState(
            scene=SceneType.PREP_SHOP,
            round_num=6,
            gold=10,
            hp=85,
            shop_level=4,
            upgrade_cost=8,
            upgrade_x=80,
            cards=[ShopCard(0, 500, name="维什戴尔", pact="萨尔贡", cost=5, tier=6)],
        )
        act = eng.decide(state)
        self.assertEqual(act.kind, ActionKind.BUY)
        self.assertEqual(act.params["name"], "维什戴尔")

    def test_reward_shop_picks_highest_score(self):
        eng = SpaPolicyEngine("config/spa_policies/regulation_12.json")
        state = ShopState(
            scene=SceneType.PREP_SHOP,
            round_num=4,
            gold=0,
            is_reward_shop=True,
            cards=[
                ShopCard(0, 300, name="普通A", cost=0, tier=3, is_free_reward=True),
                ShopCard(1, 550, name="逻各斯", pact="莱塔尼亚", cost=0, tier=6, is_free_reward=True),
                ShopCard(2, 800, name="普通B", cost=0, tier=4, is_free_reward=True),
            ],
        )
        act = eng.decide(state)
        self.assertEqual(act.kind, ActionKind.PICK_REWARD)
        self.assertEqual(act.params["name"], "逻各斯")

    def test_policy_hot_reload(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hot.json"
            cfg = json.loads(Path("config/spa_policies/regulation_12.json").read_text(encoding="utf-8"))
            p.write_text(json.dumps(cfg), encoding="utf-8")
            eng = SpaPolicyEngine(p)
            self.assertEqual(eng.cfg["economy"]["min_hp_to_upgrade"], 40)

            time.sleep(0.05)
            cfg["economy"]["min_hp_to_upgrade"] = 65
            p.write_text(json.dumps(cfg), encoding="utf-8")
            self.assertTrue(eng.reload_if_changed())
            self.assertEqual(eng.cfg["economy"]["min_hp_to_upgrade"], 65)

    def test_tier_badge_hsv_classification(self):
        def solid_hsv(h: int, s: int, v: int) -> np.ndarray:
            hsv = np.full((26, 26, 3), (h, s, v), dtype=np.uint8)
            return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

        self.assertEqual(match_tier_badge(solid_hsv(15, 200, 200)), 6)   # Tier VI orange
        self.assertEqual(match_tier_badge(solid_hsv(25, 200, 200)), 5)   # Tier V gold
        self.assertEqual(match_tier_badge(solid_hsv(95, 200, 200)), 4)   # Tier IV blue
        self.assertEqual(match_tier_badge(solid_hsv(81, 200, 200)), 3)   # Tier III green


if __name__ == "__main__":
    unittest.main(verbosity=2)
