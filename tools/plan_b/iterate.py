# -*- coding: utf-8 -*-
"""Plan B：迭代驱动（Phase 1 = manual 模式）。

流程：
  bundle 情报 → 候选作业 candidate.json → validator 静态校验 → PI 进关卡详情并「演习」
  → copilot-picker 跑单文件作业（不记台账）→ BattleObserver 采战报 → battle_report.json
  → 未满 3★ 就生成下一轮 prompt；满 3★ 提示切真理智正式打。

用法：
  # 首轮（没改轴时可用 seed 作业跑通链路）
  python tools/plan_b/iterate.py --bundle D:\\copilot-picker-local\\runs\\plan_b\\<ts> \
      --event act46side [--candidate candidate.json] [--seed-job 75481] [--rounds 1]

  # 人工改轴模式：把 AI 改好的 JSON 覆盖 bundle/candidate.json，然后重跑同一条命令
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
import time
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "picker_push"))
sys.path.insert(0, str(REPO / "tools" / "plan_b"))

import battle_observer as obs
from game_knowledge import inject_knowledge  # noqa: E402
import validator as val  # noqa: E402
from pi_runner import run_task  # noqa: E402

DEFAULT_PICKER = "http://127.0.0.1:8790"
EVENTS_DIR = REPO / "tools" / "picker_push" / "events"


def _post(picker: str, path: str, body: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(picker.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get(picker: str, path: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(picker.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def load_event(event: str) -> dict:
    p = EVENTS_DIR / f"{event}.json"
    if not p.exists():
        raise SystemExit(f"找不到活动配置 {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def enter_drill(code: str, raid: bool) -> bool:
    """PI 把游戏从任意界面带到「演习编队页」。返回是否成功。

    S 关走专用详情链；普通关（OS-1 等）先进图，详情页由后续 MAA find_stage 处理。
    """
    steps = ["收结算页"]
    if "-S-" in code:
        n = code.rsplit("-", 1)[-1]
        steps += ["进入S图", f"进入S-{n}详情"]
    elif "-EX-" in code:
        steps.append("进入EX图")
    else:
        # 普通关：进图后由 MAA find_stage 定位
        steps.append("进入普通图")
    if raid:
        steps.append("切突袭模式")
    # 演习按钮在详情页；普通关先进图，详情由后续流程处理
    if "-S-" in code:
        steps.append("进入演习")
    for step in steps:
        ok, _ = run_task(step, timeout=200)
        if not ok:
            print(f"[planb] PI 步骤失败：{step}")
            return False
    return True


def run_round(bundle: pathlib.Path, ctx: dict, candidate: pathlib.Path, args) -> dict:
    """跑一轮演习，返回 battle_report。"""
    round_dir = bundle / "rounds" / f"round-{args._round:02d}"
    round_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(candidate, round_dir / "candidate.json")

    if not enter_drill(ctx["stage"]["code"], bool(ctx["stage"]["raid"])):
        raise SystemExit("进演习失败（PI 导航）")

    event = load_event(args.event)
    device = event.get("device", "127.0.0.1:16384")
    observer = obs.BattleObserver(device, round_dir, interval=args.poll_interval)
    observer.start()
    try:
        is_s = "-S-" in ctx["stage"]["code"]
        r = _post(args.picker, "/api/run/start", {
            "accountId": event["account"],
            "client": event.get("client", ""),
            "stageCode": ctx["stage"]["code"],
            "stageName": ctx["stage"]["stageName"],
            "jobIds": [0],
            "manualMode": is_s,
            "onMap": not is_s,
            "formation": True,
            "maxAttempts": 1,
            "timeoutMin": args.timeout_min,
            "copilotFile": str(candidate.resolve()),
            "noProgress": True,
        })
        run_id = r["runId"]
        print(f"[planb] 演习 run={run_id}（观察中…）")
        deadline = time.time() + args.timeout_min * 60 + 300
        status = "?"
        while time.time() < deadline:
            time.sleep(10)
            snap = _get(args.picker, f"/api/run/{run_id}")
            status = snap.get("status")
            if status not in ("running", "idle"):
                break
    finally:
        observer.stop()
        observer.join(timeout=20)

    run_dir = pathlib.Path(event.get("runsDir") or r"D:\copilot-picker-local\runs") / run_id
    report = obs.build_report(run_dir, observer.timeline)
    report["candidate"] = str(round_dir / "candidate.json")
    report["round"] = args._round
    (round_dir / "battle_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[planb] 结算：stars={report['stars']}（{report['starsReason']}）"
          f" result={report['result']} maxKills={report['maxKills']}")
    return report


def write_next_prompt(bundle: pathlib.Path, report: dict) -> None:
    rd = pathlib.Path(report["candidate"]).parent
    lines = ["# Plan B 下一轮改轴提示", "", inject_knowledge(), "",
             f"- 上一轮：stars={report['stars']}（{report['starsReason']}），result={report['result']}",
             f"- 最终击杀：{report['maxKills']}", f"- 战报：`{rd / 'battle_report.json'}`",
             f"- 关键帧目录：`{rd / 'frames'}`", "",
             "## 改轴要点", "1. 打开 `prompt_pack.md` 的硬性约束逐条核对。",
             "2. 看 `frames/` 里击杀数最高那帧和蓝门掉血帧，找崩盘点（漏怪/费用不够/阻挡不足）。",
             "3. 优先换高练度主力、把 `skill_usage` 改 2、用 `costs`/`kills` 触发。",
             "4. 改完写回 `candidate.json`，重跑 iterate.py（会再走 validator → 演习）。"]
    (rd / "next_round_prompt.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Plan B 演习迭代（manual 模式）")
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--event", default="act46side")
    ap.add_argument("--candidate", default="", help="候选作业 JSON（默认 <bundle>/candidate.json）")
    ap.add_argument("--seed-job", type=int, default=None, help="没有 candidate 时用这个 seed 作业跑通链路")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--picker", default=DEFAULT_PICKER)
    ap.add_argument("--timeout-min", type=int, default=15)
    ap.add_argument("--poll-interval", type=float, default=12.0)
    ap.add_argument("--skip-validate", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只校验+打印流程，不进游戏")
    args = ap.parse_args(argv)

    bundle = pathlib.Path(args.bundle).resolve()
    if not (bundle / "context.json").exists():
        raise SystemExit(f"{bundle} 不是 bundle 目录（缺 context.json），先跑 context_builder.py")
    ctx = json.loads((bundle / "context.json").read_text(encoding="utf-8"))

    cand = pathlib.Path(args.candidate) if args.candidate else bundle / "candidate.json"
    if not cand.exists():
        seeds = sorted((bundle / "seeds").glob("job-*.json"))
        pick = None
        if args.seed_job is not None:
            pick = bundle / "seeds" / f"job-{args.seed_job}.json"
        elif seeds:
            pick = seeds[0]
        if pick is None or not pick.exists():
            raise SystemExit("没有 candidate.json 也没有可用 seed；先放一份候选作业")
        shutil.copy2(pick, cand)
        print(f"[planb] 没有 candidate.json，先用 seed {pick.name} 跑通链路")

    lv, _ = val.load_level(bundle)
    if not args.skip_validate:
        roster, src = val.load_roster(bundle, "", args.picker, r"D:\Xiach\M-A\MAA\MAA-v6.12.2-win-x64")
        job = json.loads(cand.read_text(encoding="utf-8"))
        errs, warns = val.validate(job, lv, ctx, roster, bool(ctx["stage"]["raid"]))
        for w in warns:
            print(f"[validator][warn] {w}")
        for e in errs:
            print(f"[validator][error] {e}")
        if errs:
            print(f"\n校验不通过（{len(errs)} error）——改好 candidate.json 再跑")
            return 1
        print(f"[validator] 通过（0 error, {len(warns)} warn）")

    if args.dry_run:
        print("[planb][dry-run] 校验通过；流程：PI 收结算→进S图→进S-N详情→切突袭→演习 → "
              "picker 演练 candidate → 观察 → 战报")
        return 0

    last = None
    for i in range(1, args.rounds + 1):
        args._round = i
        last = run_round(bundle, ctx, cand, args)
        if last.get("stars") == 3:
            print("[planb] 演习 3★！可以切真理智正式打（Phase 2 会接自动正式打）")
            run_task("退出演习", timeout=60)
            run_task("收结算页", timeout=180)
            return 0
        write_next_prompt(bundle, last)
        print(f"[planb] 未满 3★，下一轮提示：{pathlib.Path(last['candidate']).parent / 'next_round_prompt.md'}")

    # 收尾：退出演习页（可能已在失败页，失败可忽略），再统一回主界面
    run_task("退出演习", timeout=60)
    if not run_task("收结算页", timeout=180):
        print("[planb] 收尾收结算页失败（游戏可能停在失败页/结算页，下次跑前先手动收一下）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
