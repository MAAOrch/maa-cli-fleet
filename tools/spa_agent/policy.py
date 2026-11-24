from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Action, ActionKind, BenchUnit, SceneType, ShopCard, ShopState


class SpaPolicyEngine:
    """Hot-reloadable external decision brain for Stronghold Protocol.

    Reads a JSON policy file (reloading automatically on file mtime change) and
    decides the next atomic action from structured ShopState + tracked roster.
    """

    def __init__(self, policy_path: str | Path):
        self.policy_path = Path(policy_path)
        self._mtime: float = -1.0
        self.cfg: dict[str, Any] = {}
        self.reload_if_changed()

        # Lightweight cross-round memory
        self.owned_counts: dict[str, int] = {}
        self.refreshes_this_round: int = 0
        self.last_round: int = -1
        self.deployed_count: int = 0

    def reload_if_changed(self) -> bool:
        if not self.policy_path.exists():
            raise FileNotFoundError(f"Policy file not found: {self.policy_path}")
        mtime = self.policy_path.stat().st_mtime
        if mtime != self._mtime:
            self.cfg = json.loads(self.policy_path.read_text(encoding="utf-8"))
            self._mtime = mtime
            return True
        return False

    @property
    def mode(self) -> str:
        return str(self.cfg.get("mode", "high_diff"))

    def reset_run(self) -> None:
        self.owned_counts.clear()
        self.refreshes_this_round = 0
        self.last_round = -1
        self.deployed_count = 0

    def score_card(self, card: ShopCard, state: ShopState) -> tuple[float, list[str]]:
        scoring = self.cfg.get("scoring", {})
        blacklist = set(self.cfg.get("blacklist_operators", []))
        if card.name and any(b in card.name for b in blacklist):
            return -999.0, ["blacklisted"]

        reasons: list[str] = []
        tier_weights = scoring.get("tier_weights", {})
        score = float(tier_weights.get(str(card.tier), 8))
        reasons.append(f"tier{card.tier}={score:.0f}")

        # Core operator bonus
        cores = self.cfg.get("core_operators", [])
        if card.name and any(c in card.name for c in cores):
            bonus = float(scoring.get("core_operator_bonus", 50))
            score += bonus
            reasons.append(f"core+{bonus:.0f}")

        # Pact synergy bonus
        target_pacts = self.cfg.get("target_pacts", [])
        secondary_pacts = self.cfg.get("secondary_pacts", [])
        if card.pact:
            if any(p in card.pact for p in target_pacts):
                b = float(scoring.get("target_pact_bonus", 30))
                score += b
                reasons.append(f"target_pact({card.pact})+{b:.0f}")
            elif any(p in card.pact for p in secondary_pacts):
                b = float(scoring.get("secondary_pact_bonus", 12))
                score += b
                reasons.append(f"sec_pact({card.pact})+{b:.0f}")

        # Pair / Triple promotion bonus
        owned = self.owned_counts.get(card.name, 0) if card.name else 0
        if owned >= 2:
            b = float(scoring.get("triple_bonus", 85))
            score += b
            reasons.append(f"TRIPLE+{b:.0f}")
        elif owned == 1:
            b = float(scoring.get("pair_bonus", 28))
            score += b
            reasons.append(f"pair+{b:.0f}")

        # Early-round low-cost stabilizer bonus
        if state.round_num <= 3 and card.cost <= 2:
            b = float(scoring.get("early_round_low_cost_bonus", 15))
            score += b
            reasons.append(f"early_low_cost+{b:.0f}")

        # Cost penalty
        if not card.is_free_reward and card.cost > 0:
            pen = float(scoring.get("cost_penalty_per_gold", 3)) * card.cost
            score -= pen
            reasons.append(f"cost-{pen:.0f}")

        return score, reasons

    def should_upgrade_shop(self, state: ShopState) -> tuple[bool, str]:
        econ = self.cfg.get("economy", {})
        if state.shop_maxed:
            return False, "shop_maxed"
        max_lvl = int(econ.get("max_shop_level", 6))
        if state.shop_level >= max_lvl:
            return False, f"at_max_shop_level({max_lvl})"
        if state.upgrade_cost is None or state.upgrade_x is None:
            return False, "upgrade_cost_unknown"
        if state.gold < state.upgrade_cost:
            return False, f"gold({state.gold})<upgrade_cost({state.upgrade_cost})"

        min_round = int(econ.get("min_round_to_upgrade", 3))
        if state.round_num < min_round:
            return False, f"round({state.round_num})<min_round({min_round})"

        min_hp = int(econ.get("min_hp_to_upgrade", 40))
        if state.hp < min_hp:
            return False, f"hp_guard({state.hp}<{min_hp})"

        min_board = int(econ.get("min_board_units_before_upgrade", 3))
        if self.deployed_count < min_board and state.deploy_slots_free > 0:
            return False, f"board_thin({self.deployed_count}<{min_board})"

        return True, f"safe_upgrade(round={state.round_num},hp={state.hp},cost={state.upgrade_cost})"

    def choose_sell_candidate(self, bench: list[BenchUnit]) -> BenchUnit | None:
        if not bench:
            return None
        # In promote mode, sell promoted (elite) units first to recycle gold & free bench space
        if self.mode == "promote":
            elites = [u for u in bench if u.elite]
            if elites:
                return elites[0]
            return bench[-1]

        # In high_diff mode, avoid selling core operators or elites; sell lowest-tier filler
        cores = self.cfg.get("core_operators", [])
        non_core = [u for u in bench if not (u.name and any(c in u.name for c in cores))]
        pool = non_core or bench
        pool_sorted = sorted(pool, key=lambda u: (u.elite, u.tier))
        return pool_sorted[0]

    def decide(self, state: ShopState) -> Action:
        self.reload_if_changed()

        if state.scene == SceneType.CONFIRM_READY:
            return Action(ActionKind.CONFIRM_DIALOG, "confirm ready popup")
        if state.scene != SceneType.PREP_SHOP:
            return Action(ActionKind.WAIT, f"scene={state.scene.value}")

        if state.round_num != self.last_round:
            self.last_round = state.round_num
            self.refreshes_this_round = 0

        econ = self.cfg.get("economy", {})
        scoring = self.cfg.get("scoring", {})
        min_buy_score = float(scoring.get("min_buy_score", 20))

        # 1. Always pick best free promotion reward card first
        if state.is_reward_shop and state.cards:
            scored = [(self.score_card(c, state), c) for c in state.cards]
            scored.sort(key=lambda item: item[0][0], reverse=True)
            (best_score, reasons), best_card = scored[0]
            return Action(
                ActionKind.PICK_REWARD,
                f"free reward {best_card.name} score={best_score:.1f} ({','.join(reasons)})",
                {"x": best_card.x, "name": best_card.name, "slot_idx": best_card.slot_idx},
            )

        # 2. If bench has units and board still has free deploy slots, deploy first
        if state.deploy_slots_free > 0 and state.bench:
            tiles = self.cfg.get("tiles", {})
            all_tiles = list(tiles.get("path", [])) + list(tiles.get("safe", [])) + list(tiles.get("rear", []))
            if all_tiles:
                target_tile = all_tiles[self.deployed_count % len(all_tiles)]
                unit = state.bench[0]
                return Action(
                    ActionKind.DEPLOY,
                    f"fill free deploy slot ({state.deploy_slots_free} free)",
                    {"from_x": unit.x, "from_y": unit.y, "to_x": target_tile[0], "to_y": target_tile[1]},
                )

        # 3. Bench overflow / promote-mode recycling: sell before buying more
        sell_thresh = int(econ.get("sell_bench_when_occupied_ge", 7))
        if len(state.bench) >= sell_thresh:
            cand = self.choose_sell_candidate(state.bench)
            if cand is not None:
                return Action(
                    ActionKind.SELL,
                    f"recycle bench ({len(state.bench)}>={sell_thresh}, elite={cand.elite})",
                    {"from_x": cand.x, "from_y": cand.y, "slot_idx": cand.slot_idx},
                )

        # 4. Score all affordable shop cards
        affordable: list[tuple[float, list[str], ShopCard]] = []
        for c in state.cards:
            if c.cost <= state.gold:
                sc, rs = self.score_card(c, state)
                if sc >= min_buy_score:
                    affordable.append((sc, rs, c))
        affordable.sort(key=lambda item: item[0], reverse=True)

        # In high_diff mode, check if we should upgrade shop BEFORE spending on mediocre cards
        can_up, up_reason = self.should_upgrade_shop(state)
        best_is_triple = bool(affordable and any("TRIPLE" in r for r in affordable[0][1]))
        best_is_core = bool(affordable and any("core+" in r for r in affordable[0][1]))

        if can_up and not best_is_triple and not best_is_core:
            return Action(
                ActionKind.UPGRADE_SHOP,
                up_reason,
                {"x": state.upgrade_x, "cost": state.upgrade_cost},
            )

        # 5. Buy highest-scoring affordable card
        if affordable:
            best_score, reasons, best_card = affordable[0]
            return Action(
                ActionKind.BUY,
                f"buy {best_card.name or 'card'} score={best_score:.1f} ({','.join(reasons)})",
                {"x": best_card.x, "name": best_card.name, "cost": best_card.cost, "slot_idx": best_card.slot_idx},
            )

        # 6. Upgrade shop if allowed and nothing worth buying
        if can_up:
            return Action(
                ActionKind.UPGRADE_SHOP,
                up_reason,
                {"x": state.upgrade_x, "cost": state.upgrade_cost},
            )

        # 7. Refresh shop if gold & per-round refresh budget allow
        refresh_cost = int(econ.get("refresh_cost", 2))
        min_gold_refresh = int(econ.get("min_gold_to_refresh", 6))
        max_refreshes = int(econ.get("max_refreshes_per_round", 4))
        if (
            state.gold >= max(refresh_cost, min_gold_refresh)
            and self.refreshes_this_round < max_refreshes
        ):
            return Action(
                ActionKind.REFRESH,
                f"refresh ({self.refreshes_this_round + 1}/{max_refreshes}, gold={state.gold})",
                {},
            )

        # 8. Otherwise click Ready to start combat
        return Action(
            ActionKind.READY,
            f"prep complete (gold={state.gold}, round={state.round_num}, up={up_reason})",
            {},
        )

    def record_action_effect(self, action: Action) -> None:
        if action.kind in (ActionKind.BUY, ActionKind.PICK_REWARD):
            name = str(action.params.get("name", ""))
            if name:
                self.owned_counts[name] = self.owned_counts.get(name, 0) + 1
                if self.owned_counts[name] >= 3:
                    # Promoted to elite; reset base copy counter for next triple
                    self.owned_counts[name] = 0
        elif action.kind == ActionKind.DEPLOY:
            self.deployed_count += 1
        elif action.kind == ActionKind.REFRESH:
            self.refreshes_this_round += 1
