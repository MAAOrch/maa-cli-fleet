# -*- coding: utf-8 -*-
"""活动图逐关推：**MaaFw PI 导航 + copilot-picker 执行**。

架构（2026-11-22 用户指定，按 MaaFramework ProjectInterface V2 协议）：

    Python（本脚本）       只做决策：读台账/理智、选作业、重试顺序
    MaaFw PI（pi/arknights-event）  所有 UI 导航：主界面→终端→活动 hub→普通/EX/S 图、
                                    收结算页、任务失败页、保存阵容弹窗
    copilot-picker         执行作业：onMap 模式下由 MaaCore 的 find_stage 在当前活动图上
                           OCR 认出关卡、切突袭、开打（不 startup、不导航）

推关顺序（用户指定）：**本关普通 → 本关突袭 → 下一关普通 → 下一关突袭 → …**
某关突袭几份作业都打不过就记下跳过，继续下一关。

用法：
  python tools/picker_push/push_event.py --event act46side --stages OS-EX-7,OS-EX-8
  python tools/picker_push/push_event.py --event act46side --from OS-S-1 --to OS-S-5
  python tools/picker_push/push_event.py --event act46side --stages OS-EX-8 --mode normal
  python tools/picker_push/push_event.py --event act46side --stages OS-S-1 --dry-run

退出码：0 = 全部完成 / 1 = 有关卡失败 / 2 = 环境或参数问题。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from device import ensure_only_client, page_hint, read_sanity, resolve_device  # noqa: E402
from pi_runner import run_task, set_device  # noqa: E402

# 台账（progress.json）属于 copilot-picker 的**运行根**，不是代码仓：
# 双开时绝不能写死主运行根（D:\copilot-picker-local），否则 B 会话会改到实例 0 的台账。
# 优先级：--progress > 事件配置 "progress" > 环境变量 PICKER_PROGRESS；都没给就按空台账跑（只读不写）。
PROGRESS: Path | None = Path(os.environ["PICKER_PROGRESS"]) if os.environ.get("PICKER_PROGRESS") else None
TRIED_FILE = HERE / "_tried.json"  # 本轮已经试过、没达标的作业（跨多次运行保留，防反复撞同一份 0★/2★）

# 区域 → PI 任务名
AREA_TASK = {"main": "进入普通图", "ex": "进入EX图", "s": "进入S图"}


class OutOfSanity(RuntimeError):
    """MAA 起不了战斗（BattleStartAll），多半是理智不够——整批停手去补理智。"""


class NavFailed(RuntimeError):
    """PI 导航任务失败。"""


def load_event(name: str) -> dict:
    p = HERE / "events" / f"{name}.json"
    if not p.exists():
        raise SystemExit(f"[err] 没有活动配置 {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def ledger() -> dict:
    if PROGRESS is None or not PROGRESS.exists():
        return {}
    try:
        rows = json.loads(PROGRESS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return {(r.get("stageCode", ""), r.get("mode", "")): r.get("stars", 0) for r in rows}


def load_tried() -> dict[tuple[str, str], set[int]]:
    if not TRIED_FILE.exists():
        return {}
    try:
        raw = json.loads(TRIED_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return {(k.split("|")[0], k.split("|")[1]): set(v) for k, v in raw.items() if "|" in k}


def save_tried(tried: dict[tuple[str, str], set[int]]) -> None:
    raw = {f"{c}|{m}": sorted(ids) for (c, m), ids in tried.items() if ids}
    try:
        TRIED_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def api(cfg: dict):
    import urllib.request

    def post(path: str, body: dict) -> dict:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            cfg["api"] + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)

    def get(path: str) -> dict:
        with urllib.request.urlopen(cfg["api"] + path, timeout=30) as r:
            return json.load(r)

    return post, get


def search_jobs(post, code: str, stage_name: str, limit: int, mode: str = "") -> list[dict]:
    body = {"stageCode": code, "stageName": stage_name, "limit": limit}
    if mode:
        body["mode"] = mode  # picker 侧按标题筛掉「仅突袭/仅普通」等不对难度的作业
    out = post("/api/jobs/search", body)
    return [j for j in out.get("jobs", []) if j.get("mode") == "ready" and j.get("met")]


def start_run(post, cfg: dict, code: str, stage_name: str, job_id: int, mode: str,
              timeout_min: int, manual: bool = False) -> str:
    stage = {"stageCode": code, "stageName": stage_name, "jobIds": [job_id]}
    if mode == "raid":
        stage["raidJobIds"] = [job_id]
    body = {
        "accountId": cfg["account"], "client": cfg.get("client", ""),
        "stages": [stage],
        "manualMode": manual, "onMap": not manual,
        "formation": True, "addTrust": True, "ignoreRequirements": True,
        "maxAttempts": 1, "supportUnitUsage": 0, "timeoutMin": timeout_min,
    }
    return post("/api/run/start", body)["runId"]


def wait_run(get, run_id: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(6)
        try:
            snap = get(f"/api/run/{run_id}")
        except Exception:  # noqa: BLE001
            continue
        if snap.get("finishedAt"):
            return snap
    return {"status": "timeout"}


def stage_has_raid(st: dict, area: str) -> bool:
    if "raid" in st:
        return bool(st["raid"])
    return area in ("ex", "s")


def go_area(cfg: dict, area: str, log=print) -> None:
    """用 PI 任务把游戏导航到指定区域的活动图；失败先收结算页再试一次。"""
    task = AREA_TASK.get(area)
    if not task:
        raise NavFailed(f"未知区域 {area}")
    ok, out = run_task(task, timeout=300, log=log)
    if ok:
        return
    log("    [pi] 导航失败，先收结算页再重试一次")
    run_task("收结算页", timeout=180, log=log)
    ok, out = run_task(task, timeout=300, log=log)
    if not ok:
        raise NavFailed(f"PI 任务「{task}」失败：" + out.strip().splitlines()[-1][:200])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True)
    ap.add_argument("--stages", default="", help="逗号分隔；为空则用 --from/--to")
    ap.add_argument("--from", dest="from_stage", default="")
    ap.add_argument("--to", dest="to_stage", default="")
    ap.add_argument("--area", default="", help="覆盖区域（默认取配置）")
    ap.add_argument("--mode", default="", help="normal / raid（默认按 普通→突袭 循环）")
    ap.add_argument("--max-jobs", type=int, default=3)
    ap.add_argument("--timeout-min", type=int, default=15)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="不跳过台账里已 3 星的关卡")
    ap.add_argument("--max-potions", type=int, default=0,
                    help="最多自动用几个理智药补理智（默认 0 = 不嗑药，理智不足就停手）；"
                         "嗑药前必须 OCR 到「理智合剂」确认语，认不到直接停")
    # 双开隔离：事件配置里写的是某台实例的 api/账号/设备，命令行可整套覆盖。
    ap.add_argument("--api", default="", help="copilot-picker URL 覆盖（如 http://127.0.0.1:8791）")
    ap.add_argument("--account", default="", help="账号 id 覆盖（默认取事件配置）")
    ap.add_argument("--client", default="", help="客户端覆盖（Official/Bilibili/YoStarJP...）")
    ap.add_argument("--client-package", dest="client_package", default="", help="目标客户端包名覆盖")
    ap.add_argument("--device", default="", help="adb 地址覆盖，如 127.0.0.1:16416")
    ap.add_argument("--adb-name", dest="adb_name", default="",
                    help="MaaToolkit 探测名覆盖，如 2-MuMuPlayer v5+（PI 按名字选设备）")
    ap.add_argument("--emu", default="", help="MuMu 实例号覆盖（adb 端口漂移时用它问 MuMuManager）")
    ap.add_argument("--progress", default="", help="台账 progress.json 覆盖（默认事件配置/环境变量）")
    args = ap.parse_args()

    cfg = load_event(args.event)
    for key, val in (
        ("api", args.api), ("account", args.account), ("client", args.client),
        ("client_package", args.client_package), ("device", args.device),
        ("adb_name", args.adb_name), ("emu", args.emu),
    ):
        if val:
            cfg[key] = val
    global PROGRESS
    if args.progress:
        PROGRESS = Path(args.progress)
    elif cfg.get("progress"):
        PROGRESS = Path(cfg["progress"])
    if PROGRESS is None:
        print("[warn] 未配置台账（--progress / 事件配置 progress / PICKER_PROGRESS）：按空台账跑，不读写 progress.json")
    else:
        print(f"[cfg] api={cfg['api']} account={cfg.get('account')} device={cfg.get('device')} progress={PROGRESS}")
    set_device(address=cfg.get("device", ""), name=cfg.get("adb_name", ""))
    stages_cfg = cfg["stages"]
    if args.stages:
        codes = [s.strip() for s in args.stages.split(",") if s.strip()]
    elif args.from_stage:
        order = list(stages_cfg)
        i = order.index(args.from_stage)
        j = order.index(args.to_stage) + 1 if args.to_stage else len(order)
        codes = order[i:j]
    else:
        raise SystemExit("[err] 要么给 --stages，要么给 --from")
    unknown = [c for c in codes if c not in stages_cfg]
    if unknown:
        raise SystemExit(f"[err] 配置里没有这些关：{unknown}")

    post, get = api(cfg)

    # 客户端隔离：模拟器里其它服客户端一律 force-stop，目标客户端拉前台
    # （2026-11-22 05:32 官服在前台被 maa startup 误唤醒，白打 3 场）
    try:
        resolve_device(cfg)  # MuMu 重启后 adb 端口会漂，先校正再连
        # 端口漂移后把解析出的新地址同步给 PI（PI 按设备名匹配，地址也要对）
        set_device(address=cfg.get("device", ""), name=cfg.get("adb_name", ""))
        print(f"[cfg] 实际设备 {cfg.get('device')}（PI name={cfg.get('adb_name') or '沿用 setup 配置'}）")
        ensure_only_client(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[err] 客户端隔离失败：{e}")
        return 2

    # 开局先收一次（把游戏归位到主界面），同时验证 PI 运行时就绪
    if not run_task("收结算页", timeout=180, log=print):
        print("[err] PI 收结算页任务失败，先检查 pi/arknights-event 的 setup 与设备连接")
        return 2

    san = read_sanity(cfg)
    sanity_est = san[0] if san else None
    print(f"开推理智：{san[0] if san else '?'}/{san[1] if san else '?'}")

    done, skipped, failed, raid_skipped, stars_short = [], [], [], [], []
    potions_used = 0
    # 本轮内已经试过的作业（跨多次运行保留在 _tried.json，避免反复撞同一份 0★/2★ 作业）
    tried: dict[tuple[str, str], set[int]] = load_tried()

    def push_one(code: str, st: dict, area: str, mode: str, cost: int) -> tuple[bool, str]:
        """一关一个难度：导航 → 逐份作业执行。成功 True；失败给原因。"""
        nonlocal sanity_est
        if sanity_est is not None and sanity_est < cost:
            raise OutOfSanity(f"理智不足（估剩 {sanity_est} < {cost}）")
        jobs = search_jobs(post, code, st["stageName"], args.max_jobs, mode)
        if not jobs:
            return False, "没有可用作业（名册匹配全 blocked）"
        ids = [int(j["id"]) for j in jobs]
        key = (code, mode)
        fresh_ids = [i for i in ids if i not in tried.get(key, set())]
        if not fresh_ids:
            return False, "候选作业本轮都试过了（0★/未达 3★），换搜索条件或人工挑作业再试"
        ids = fresh_ids
        print(f"   {mode} 作业候选：{ids}  "
              + " | ".join(f"#{j['id']} {j['title'][:16]}" for j in jobs), flush=True)
        # S 关（喀兰之心）MAA 的 find_stage 会把标签 OCR 成 0S-S-1 找不到，只能走手动模式：
        # PI 一次调用「收上一场结算页 + 点开本关详情 + 校验右上角关卡代号」；要突袭再切一次。
        manual = area in cfg.get("manual_areas", [])
        last_err = ""
        for job_id in ids:
            if manual:
                # 可靠恢复顺序：不在详情页就先「收结算页」回主界面（这条链能处理结算/失败页），
                # 再导航到 S 图，再调「进入S-N详情」（自带收结算 + 详情代号校验）。
                # 不再单独用「收结算页留在地图」——它在 0 星结算页上实测会 30~120s 失败。
                hint = page_hint(cfg)
                if hint != "detail":
                    run_task("收结算页", timeout=180, log=print)
                    go_area(cfg, area)
                n = code.rsplit("-", 1)[-1]
                ok_detail = run_task(f"进入S-{n}详情", timeout=180, log=print)
                if not ok_detail:
                    run_task("收结算页", timeout=180, log=print)
                    go_area(cfg, area)
                    ok_detail = run_task(f"进入S-{n}详情", timeout=180, log=print)
                if not ok_detail:
                    last_err = "PI 没把 S 关详情页点开"
                    run_task("收结算页", timeout=180, log=print)
                    continue
                if page_hint(cfg) != "detail":
                    last_err = "PI 报成功但页面不在关卡详情"
                    run_task("收结算页", timeout=180, log=print)
                    continue
                if mode == "raid":
                    if not run_task("切突袭模式", timeout=90, log=print):
                        last_err = "PI 切突袭失败（突袭可能还没解锁）"
                        continue
            else:
                go_area(cfg, area)  # PI：导航到区域图（已在图上时秒回）
            run_id = start_run(post, cfg, code, st["stageName"], job_id, mode, args.timeout_min, manual=manual)
            print(f"   #{job_id} run={run_id} 等待结算…", flush=True)
            snap = wait_run(get, run_id, args.timeout_min * 60 + 240)
            stt = snap.get("status")
            print(f"   -> {stt} {snap.get('error', '')}".rstrip(), flush=True)
            # 手动模式打完游戏停在结算/失败页：下一次尝试的「收结算页/进入S-N详情」会处理它，
            # 这里不做动作（避免在 0 星页上用不稳定的收页链空转）。
            if stt == "success":
                if sanity_est is not None:
                    sanity_est -= cost
                return True, ""
            last_err = f"#{job_id} {stt} {snap.get('error', '')}".strip()
            tried.setdefault(key, set()).add(job_id)
            save_tried(tried)
            if "BattleStartAll" in last_err:
                raise OutOfSanity("MAA 起不了战斗（BattleStartAll），多半是理智不足——先补理智再继续")
            if not manual:
                run_task("收结算页", timeout=180, log=print)  # 失败后先把页面收干净再换作业
        return False, last_err

    def use_potion() -> bool:
        """理智不足时补一档药剂。返回是否补成功且验证到理智上涨。

        双保险：PI 任务「补充理智」必须 OCR 到弹层底部的「理智合剂」确认语才点 ✓；
        认不到就失败（绝不碰至纯源石）。补完再读一次理智，没涨也判失败。
        """
        nonlocal potions_used, sanity_est
        before = read_sanity(cfg)
        if not run_task("补充理智", timeout=90, log=print):
            print("[stop] 补充理智失败（没认出「理智合剂」等药剂确认语，可能是在源石页签/弹层没打开），不嗑药")
            return False
        after = read_sanity(cfg)
        if after is None or (before is not None and after[0] <= before[0]):
            print(f"[stop] 用药后理智没验证到上涨（before={before}, after={after}），停手检查")
            return False
        potions_used += 1  # 只有验证上涨才算真用了（防误把失败当已用）
        sanity_est = after[0]
        print(f"   已用药剂补理智：{before[0] if before else '?'} → {after[0]}")
        return True

    def push_mode(code: str, st: dict, area: str, mode: str, cost: int) -> tuple[str, str]:
        """跑一关一个难度。返回 (结果类型, 说明)：ok / stars / fail / sanity / nav。"""
        nonlocal sanity_est
        try:
            ok, err = push_one(code, st, area, mode, cost)
        except OutOfSanity as e:
            return "sanity", str(e)
        except NavFailed as e:
            return "nav", str(e)
        if ok:
            return "ok", ""
        # 没到 3 星（普通关 1/2 星）会被 picker 判失败并说明；和 0 星一样换过作业了
        if "星" in err and ("只拿到" in err or "三星" in err):
            return "stars", err
        return "fail", err

    def rest_of_stage(block_mode: list[str], code: str, reason: str) -> None:
        """本关后续难度不再尝试（普通没到 3★ / 突袭打不过）。——保留给以后批量调度用"""

    for code in codes:
        st = stages_cfg[code]
        area = args.area or st["area"]
        cost = int(st.get("cost", 10))
        raid = stage_has_raid(st, area) and not args.mode
        modes = [args.mode] if args.mode else (["normal", "raid"] if raid else ["normal"])
        print(f"== {code}（{st['stageName']}）area={area} 顺序={modes}")

        stop_batch = False
        for mode in modes:
            tag = "突袭" if mode == "raid" else "普通"
            if ledger().get((code, mode), 0) >= 3 and not args.force:
                print(f"   {tag}：台账已 3 星，跳过")
                skipped.append(f"{code}/{tag}")
                continue
            if args.dry_run:
                try:
                    go_area(cfg, area)
                    jobs = search_jobs(post, code, st["stageName"], args.max_jobs)
                    print(f"   {tag} 导航 ✓ 候选 {['#' + str(j['id']) for j in jobs]}")
                except NavFailed as e:
                    print(f"   [dry-run] 导航失败：{e}")
                continue

            while True:
                kind, err = push_mode(code, st, area, mode, cost)
                if kind == "sanity":
                    if potions_used < args.max_potions and use_potion():
                        continue  # 补完理智重试同一难度
                    print(f"[stop] {err}")
                    failed.append((code, err))
                    stop_batch = True
                    break
                if kind == "nav":
                    print(f"[stop] {err}")
                    failed.append((code, err))
                    stop_batch = True
                    break
                break

            if stop_batch:
                break
            if kind == "ok":
                done.append(f"{code}/{tag}")
                continue
            if kind == "stars":
                # 普通关 1★/2★：台账记最高星，换作业已试完仍不够 → 本关突袭解锁不了，记下跳过
                print(f"   普通关未达 3 星（{err}），台账已记最高星；本关突袭跳过，继续下一关")
                stars_short.append(f"{code}（{err}）")
                break
            if mode == "raid":
                print(f"   突袭打不过（{err}），先记下跳过，继续下一关")
                raid_skipped.append(f"{code}（{err}）")
                break
            print(f"   普通关失败（{err}），继续下一关")
            failed.append((code, err))
            break
        if stop_batch:
            break

    print("\n==== 汇总 ====")
    print("完成：", done)
    print("跳过（台账已 3 星）：", skipped)
    if stars_short:
        print("普通关没打出 3 星、突袭先跳过的关：", stars_short)
    if raid_skipped:
        print("突袭打不过、先跳过的关：", raid_skipped)
    if potions_used:
        print(f"本轮用了 {potions_used} 个理智药")
    if failed:
        print("失败/停手：", failed)
        run_task("收结算页", timeout=180, log=print)  # 收尾：把游戏放回主界面
        return 1
    # 全部完成也收尾一次，别把游戏留在结算页
    run_task("收结算页", timeout=180, log=print)
    return 0


if __name__ == "__main__":
    sys.exit(main())
