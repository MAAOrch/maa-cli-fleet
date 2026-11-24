#!/usr/bin/env python3
"""CLI runner for the standalone Stronghold Protocol external agent (Route B).

Supports:
- Offline single-frame perception & decision dry-run:
    python -m tools.spa_agent.runner --dry-run-image path/to/frame.png --policy config/spa_policies/regulation_12.json
- Live ADB loop with hot-reloadable policy, JSONL trace logging, and AUTO-MAS fleet-pause yield:
    python -m tools.spa_agent.runner --addr 127.0.0.1:16512 --policy config/spa_policies/promote_200.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .controller import AdbController, has_active_mas_lock
from .models import Action, ActionKind
from .perception import SpaPerception, load_image
from .policy import SpaPolicyEngine

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def execute_action(ctrl: AdbController, action: Action) -> None:
    p = action.params
    if action.kind in (ActionKind.BUY, ActionKind.PICK_REWARD):
        ctrl.buy_card(int(p["x"]))
    elif action.kind == ActionKind.UPGRADE_SHOP:
        ctrl.upgrade_shop(int(p["x"]))
    elif action.kind == ActionKind.REFRESH:
        ctrl.refresh_shop()
    elif action.kind == ActionKind.DEPLOY:
        ctrl.deploy_from_bench(int(p["from_x"]), int(p["from_y"]), int(p["to_x"]), int(p["to_y"]))
    elif action.kind == ActionKind.SELL:
        ctrl.sell_from_bench(int(p["from_x"]), int(p["from_y"]))
    elif action.kind == ActionKind.READY:
        ctrl.click_ready()
    elif action.kind == ActionKind.CONFIRM_DIALOG:
        ctrl.click_confirm_dialog()
    else:
        time.sleep(1.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="Standalone Stronghold Protocol external agent (Route B)")
    ap.add_argument("--policy", required=True, help="Path to hot-reloadable JSON policy file")
    ap.add_argument("--dry-run-image", default="", help="Run perception + policy once on a local 1280x720 screenshot")
    ap.add_argument("--addr", default="", help="ADB address (e.g. 127.0.0.1:16512)")
    ap.add_argument("--adb", default=r"E:\MuMu Player 12\nx_main\adb.exe", help="Path to adb.exe")
    ap.add_argument("--pause-locks-dir", default=r"E:\fleet-pause\locks", help="AUTO-MAS fleet-pause lock dir")
    ap.add_argument("--trace-log", default="", help="Optional JSONL path to record state & decisions")
    ap.add_argument("--max-steps", type=int, default=500, help="Max decision steps before stopping")
    args = ap.parse_args()

    perception = SpaPerception()
    policy = SpaPolicyEngine(args.policy)

    if args.dry_run_image:
        img = load_image(args.dry_run_image)
        state = perception.parse_shop(img)
        action = policy.decide(state)
        out = {
            "policy": policy.cfg.get("name"),
            "mode": policy.mode,
            "scene": state.scene.value,
            "round": state.round_num,
            "gold": state.gold,
            "deploy_slots_free": state.deploy_slots_free,
            "shop_maxed": state.shop_maxed,
            "upgrade_cost": state.upgrade_cost,
            "is_reward_shop": state.is_reward_shop,
            "cards": [c.__dict__ for c in state.cards],
            "bench_count": len(state.bench),
            "action": action.to_dict(),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if not args.addr:
        print("ERROR: --addr is required when not using --dry-run-image", file=sys.stderr)
        return 2

    ctrl = AdbController(args.adb, args.addr)
    if not ctrl.connect():
        print(f"ERROR: failed to connect ADB {args.addr}", file=sys.stderr)
        return 5

    trace_fp = open(args.trace_log, "a", encoding="utf-8") if args.trace_log else None
    try:
        for step in range(1, args.max_steps + 1):
            lock = has_active_mas_lock(args.pause_locks_dir)
            if lock:
                print(f"[spa_agent] MAS lock detected ({lock}), yielding cleanly (rc=15).", flush=True)
                return 15

            img = ctrl.screencap()
            state = perception.parse_shop(img)
            action = policy.decide(state)
            print(
                f"[step={step}] scene={state.scene.value} r={state.round_num} g={state.gold} "
                f"bench={len(state.bench)} -> {action.kind.value} ({action.reason})",
                flush=True,
            )
            if trace_fp:
                trace_fp.write(
                    json.dumps(
                        {
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "step": step,
                            "scene": state.scene.value,
                            "round": state.round_num,
                            "gold": state.gold,
                            "bench": len(state.bench),
                            "cards": [c.__dict__ for c in state.cards],
                            "action": action.to_dict(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                trace_fp.flush()

            execute_action(ctrl, action)
            policy.record_action_effect(action)
    finally:
        if trace_fp:
            trace_fp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
