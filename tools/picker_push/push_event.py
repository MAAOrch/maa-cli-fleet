# -*- coding: utf-8 -*-
"""活动图逐关推：**MaaFw PI 导航 + copilot-picker 执行**。

架构（2026-11-22 用户指定，按 MaaFramework ProjectInterface V2 协议）：

    Python（本脚本）       只做决策：读台账/理智、选作业、助战降级、重试顺序、Webhook 推送
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
  python tools/picker_push/push_event.py --event act46side --from OS-1 --webhook https://...

退出码：0 = 全部完成 / 1 = 有关卡失败 / 2 = 环境或参数问题。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from device import ensure_only_client, page_hint, read_sanity, resolve_device  # noqa: E402
from pi_runner import run_task, set_device
from fleet_bridge import enqueue_pi  # noqa: E402

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


def build_webhook_payload(url: str, event: str, title: str, text: str, data: dict | None = None) -> dict:
    """按常见群机器人 URL 自动适配 JSON 载荷，未知 URL 走通用结构。"""
    low = url.lower()
    full_text = f"{title}\n{text}" if text else title
    if "open.feishu.cn" in low or "open.larksuite.com" in low:
        return {"msg_type": "text", "content": {"text": full_text}}
    if "oapi.dingtalk.com" in low or "qyapi.weixin.qq.com" in low:
        return {"msgtype": "text", "text": {"content": full_text}}
    if "discord.com/api/webhooks" in low or "discordapp.com/api/webhooks" in low:
        return {"content": f"**{title}**\n{text}" if text else f"**{title}**"}
    return {
        "event": event,
        "title": title,
        "text": text,
        "data": data or {},
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def send_webhook(url: str, event: str, title: str, text: str, data: dict | None = None) -> bool:
    """轻量 Webhook 推送（纯标准库，失败只打日志，绝不打断推关）。"""
    if not url:
        return False
    payload = build_webhook_payload(url, event, title, text, data)
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception as e:  # noqa: BLE001
        print(f"   [webhook] 推送失败（不影响推关）：{e}", flush=True)
        return False


def api(cfg: dict):
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


def search_jobs(
    post, code: str, stage_name: str, limit: int, mode: str = "", allow_borrow: bool = True
) -> list[dict]:
    """拉取候选作业：先排 ready（不用助战），再用 borrow（助战降级 supportUnitUsage=1）兜底。"""
    fetch_limit = max(limit * 3, 12) if allow_borrow else limit
    body = {"stageCode": code, "stageName": stage_name, "limit": fetch_limit}
    if mode:
        body["mode"] = mode  # picker 侧按难度过滤掉不对难度的作业
    out = post("/api/jobs/search", body)
    all_jobs = out.get("jobs", [])
    ready = [j for j in all_jobs if j.get("mode") == "ready" and j.get("met")][:limit]
    if not allow_borrow:
        return ready
    borrow = [j for j in all_jobs if j.get("mode") == "borrow"][:limit]
    return ready + borrow


def start_run(
    post,
    cfg: dict,
    code: str,
    stage_name: str,
    job_id: int,
    mode: str,
    timeout_min: int,
    manual: bool = False,
    support_unit_usage: int = 0,
) -> str:
    stage = {"stageCode": code, "stageName": stage_name, "jobIds": [job_id]}
    if mode == "raid":
        stage["raidJobIds"] = [job_id]
    body = {
        "accountId": cfg["account"],
        "client": cfg.get("client", ""),
        "stages": [stage],
        "manualMode": manual,
        "onMap": not manual,
        "formation": True,
        "addTrust": True,
        "ignoreRequirements": True,
        "maxAttempts": 1,
        "supportUnitUsage": int(support_unit_usage),
        "timeoutMin": timeout_min,
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


def go_area(cfg: dict, area: str, log=print, via_fleet=False, fleet_state_dir="", account="") -> None:
    """用 PI 任务把游戏导航到指定区域的活动图；失败先收结算页再试一次。"""
    task = AREA_TASK.get(area)
    if not task:
        raise NavFailed(f"未知区域 {area}")
    if via_fleet:
        enqueue_pi("arknights-event", task, account or cfg.get("account", ""), fleet_state_dir or None)
        log(f"    [fleet] 已入队 pi.arknights-event.{task}")
        return
    ok, out = run_task(task, timeout=300, log=log)
    if ok:
        return
    log("    [pi] 导航失败，先收结算页再重试一次")
    run_task("收结算页", timeout=180, log=log)
    ok, out = run_task(task, timeout=300, log=log)
    if not ok:
        log("    [pi] 进图失败，尝试滑动扫描")
        # 滑动兜底：如果已在某个活动图，左右滑找目标
        if swipe_find_stage(cfg, "OS-1", log=log):
            return
        raise NavFailed(f"PI 任务「{task}」失败：" + out.strip().splitlines()[-1][:200])


def _format_job_brief(j: dict) -> str:
    tag = "[borrow]" if j.get("mode") == "borrow" else ""
    sc = j.get("localScore")
    sc_tag = f"({sc:+.1f})" if sc else ""
    return f"#{j['id']}{tag}{sc_tag} {j.get('title', '')[:16]}"


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
    ap.add_argument(
        "--no-borrow",
        action="store_true",
        help="禁用 borrow 助战自动降级（默认开启：ready 全败/无可用时自动用 supportUnitUsage=1 试 borrow）",
    )
    ap.add_argument(
        "--webhook",
        default="",
        help="轻量 Webhook URL（支持飞书/钉钉/企业微信/Discord/通用 JSON POST；也可设事件配置 webhook 或 PICKER_WEBHOOK_URL）",
    )
    ap.add_argument(
        "--webhook-every-stage",
        action="store_true",
        help="每关完成/失败也推送 Webhook（默认只在停手预警与整批汇总时推）",
    )
    ap.add_argument(
        "--max-potions",
        type=int,
        default=0,
        help="最多自动用几个理智药补理智（默认 0 = 不嗑药，理智不足就停手）；"
        "嗑药前必须 OCR 到「理智合剂」确认语，认不到直接停",
    )
    # 双开隔离：事件配置里写的是某台实例的 api/账号/设备，命令行可整套覆盖。
    ap.add_argument("--api", default="", help="copilot-picker URL 覆盖（如 http://127.0.0.1:8791）")
    ap.add_argument("--account", default="", help="账号 id 覆盖（默认取事件配置）")
    ap.add_argument("--client", default="", help="客户端覆盖（Official/Bilibili/YoStarJP...）")
    ap.add_argument("--client-package", dest="client_package", default="", help="目标客户端包名覆盖")
    ap.add_argument("--device", default="", help="adb 地址覆盖，如 127.0.0.1:16416")
    ap.add_argument(
        "--via-fleet",
        action="store_true",
        help="PI 导航写入 fleet queue.in 而不直接调 MaaPiCli（需要 fleet runner 正在跑）",
    )
    ap.add_argument(
        "--fleet-state-dir",
        default="",
        help="fleet 台账目录（queue.in 所在目录），配合 --via-fleet 使用",
    )
    ap.add_argument(
        "--adb-name",
        dest="adb_name",
        default="",
        help="MaaToolkit 探测名覆盖，如 2-MuMuPlayer v5+（PI 按名字选设备）",
    )
    ap.add_argument("--emu", default="", help="MuMu 实例号覆盖（adb 端口漂移时用它问 MuMuManager）")
    ap.add_argument("--progress", default="", help="台账 progress.json 覆盖（默认事件配置/环境变量）")
    args = ap.parse_args()

    cfg = load_event(args.event)
    for key, val in (
        ("api", args.api),
        ("account", args.account),
        ("client", args.client),
        ("client_package", args.client_package),
        ("device", args.device),
        ("adb_name", args.adb_name),
        ("emu", args.emu),
    ):
        if val:
            cfg[key] = val

    webhook_url = (args.webhook or cfg.get("webhook") or os.environ.get("PICKER_WEBHOOK_URL") or "").strip()
    allow_borrow = not args.no_borrow

    global PROGRESS
    if args.progress:
        PROGRESS = Path(args.progress)
    elif cfg.get("progress"):
        PROGRESS = Path(cfg["progress"])
    if PROGRESS is None:
        print("[warn] 未配置台账（--progress / 事件配置 progress / PICKER_PROGRESS）：按空台账跑，不读写 progress.json")
    else:
        print(
            f"[cfg] api={cfg['api']} account={cfg.get('account')} device={cfg.get('device')} "
            f"progress={PROGRESS} borrow={'on' if allow_borrow else 'off'}"
            + (" webhook=on" if webhook_url else "")
        )
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
    try:
        resolve_device(cfg)  # MuMu 重启后 adb 端口会漂，先校正再连
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
    tried: dict[tuple[str, str], set[int]] = load_tried()

    def notify(event: str, title: str, text: str, data: dict | None = None) -> None:
        if webhook_url:
            send_webhook(webhook_url, event, title, text, data)

    def push_one(code: str, st: dict, area: str, mode: str, cost: int) -> tuple[bool, str]:
        """一关一个难度：导航 → 逐份作业执行（ready 优先，自动降级 borrow 助战）。成功 True；失败给原因。"""
        nonlocal sanity_est
        if sanity_est is not None and sanity_est < cost:
            raise OutOfSanity(f"理智不足（估剩 {sanity_est} < {cost}）")
        jobs = search_jobs(post, code, st["stageName"], args.max_jobs, mode, allow_borrow=allow_borrow)
        if not jobs:
            return False, "没有可用作业（名册匹配全 blocked/train）"
        key = (code, mode)
        fresh_jobs = [j for j in jobs if int(j["id"]) not in tried.get(key, set())]
        if not fresh_jobs:
            return False, "候选作业本轮都试过了（0★/未达 3★），换搜索条件或人工挑作业再试"
        ids = [int(j["id"]) for j in fresh_jobs]
        print(
            f"   {mode} 作业候选：{ids}  " + " | ".join(_format_job_brief(j) for j in fresh_jobs),
            flush=True,
        )
        manual = area in cfg.get("manual_areas", [])
        last_err = ""
        borrow_announced = False
        for job in fresh_jobs:
            job_id = int(job["id"])
            is_borrow = job.get("mode") == "borrow"
            support_usage = 1 if is_borrow else 0
            if is_borrow and not borrow_announced:
                borrow_announced = True
                missing_str = "、".join(job.get("missing") or []) or "1 名干员"
                print(
                    f"   [borrow] 自动降级助战模式（supportUnitUsage=1），尝试 #{job_id}（借：{missing_str}）",
                    flush=True,
                )
            if manual:
                hint = page_hint(cfg)
                if hint != "detail":
                    run_task("收结算页", timeout=180, log=print)
                    go_area(cfg, area, via_fleet=args.via_fleet, fleet_state_dir=args.fleet_state_dir, account=args.account or cfg.get("account", ""))
                n = code.rsplit("-", 1)[-1]
                ok_detail = run_task(f"进入S-{n}详情", timeout=180, log=print)
                if not ok_detail:
                    run_task("收结算页", timeout=180, log=print)
                    go_area(cfg, area, via_fleet=args.via_fleet, fleet_state_dir=args.fleet_state_dir, account=args.account or cfg.get("account", ""))
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
                go_area(cfg, area, via_fleet=args.via_fleet, fleet_state_dir=args.fleet_state_dir, account=args.account or cfg.get("account", ""))
            run_id = start_run(
                post,
                cfg,
                code,
                st["stageName"],
                job_id,
                mode,
                args.timeout_min,
                manual=manual,
                support_unit_usage=support_usage,
            )
            tag_sup = " [助战]" if support_usage else ""
            print(f"   #{job_id}{tag_sup} run={run_id} 等待结算…", flush=True)
            snap = wait_run(get, run_id, args.timeout_min * 60 + 240)
            stt = snap.get("status")
            print(f"   -> {stt} {snap.get('error', '')}".rstrip(), flush=True)
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
                run_task("收结算页", timeout=180, log=print)
        return False, last_err

    def use_potion() -> bool:
        nonlocal potions_used, sanity_est
        before = read_sanity(cfg)
        if not run_task("补充理智", timeout=90, log=print):
            print("[stop] 补充理智失败（没认出「理智合剂」等药剂确认语，可能是在源石页签/弹层没打开），不嗑药")
            return False
        after = read_sanity(cfg)
        if after is None or (before is not None and after[0] <= before[0]):
            print(f"[stop] 用药后理智没验证到上涨（before={before}, after={after}），停手检查")
            return False
        potions_used += 1
        sanity_est = after[0]
        print(f"   已用药剂补理智：{before[0] if before else '?'} → {after[0]}")
        return True

    def push_mode(code: str, st: dict, area: str, mode: str, cost: int) -> tuple[str, str]:
        nonlocal sanity_est
        try:
            ok, err = push_one(code, st, area, mode, cost)
        except OutOfSanity as e:
            return "sanity", str(e)
        except NavFailed as e:
            return "nav", str(e)
        if ok:
            return "ok", ""
        if "星" in err and ("只拿到" in err or "三星" in err):
            return "stars", err
        return "fail", err

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
                    go_area(cfg, area, via_fleet=args.via_fleet, fleet_state_dir=args.fleet_state_dir, account=args.account or cfg.get("account", ""))
                    jobs = search_jobs(
                        post, code, st["stageName"], args.max_jobs, mode, allow_borrow=allow_borrow
                    )
                    print(f"   {tag} 导航 ✓ 候选 {[_format_job_brief(j) for j in jobs]}")
                except NavFailed as e:
                    print(f"   [dry-run] 导航失败：{e}")
                continue

            while True:
                kind, err = push_mode(code, st, area, mode, cost)
                if kind == "sanity":
                    if potions_used < args.max_potions and use_potion():
                        continue
                    print(f"[stop] {err}")
                    failed.append((code, err))
                    notify(
                        "out_of_sanity",
                        f"[推关停手] {cfg.get('account')} · {code}/{tag} 理智不足",
                        err,
                        {"event": args.event, "account": cfg.get("account"), "stage": code, "mode": mode},
                    )
                    stop_batch = True
                    break
                if kind == "nav":
                    print(f"[stop] {err}")
                    failed.append((code, err))
                    notify(
                        "nav_failed",
                        f"[推关停手] {cfg.get('account')} · {code}/{tag} PI 导航失败",
                        err,
                        {"event": args.event, "account": cfg.get("account"), "stage": code, "mode": mode},
                    )
                    stop_batch = True
                    break
                break

            if stop_batch:
                break
            if kind == "ok":
                done.append(f"{code}/{tag}")
                if args.webhook_every_stage:
                    notify(
                        "stage_done",
                        f"[推关通过] {cfg.get('account')} · {code}/{tag}",
                        f"已完成 {len(done)} 项，估剩理智 {sanity_est if sanity_est is not None else '?'}",
                        {"event": args.event, "account": cfg.get("account"), "stage": code, "mode": mode},
                    )
                continue
            if kind == "stars":
                print(f"   普通关未达 3 星（{err}），台账已记最高星；本关突袭跳过，继续下一关")
                stars_short.append(f"{code}（{err}）")
                if args.webhook_every_stage:
                    notify(
                        "stars_short",
                        f"[未达 3★] {cfg.get('account')} · {code}/普通",
                        err,
                        {"event": args.event, "account": cfg.get("account"), "stage": code},
                    )
                break
            if mode == "raid":
                print(f"   突袭打不过（{err}），先记下跳过，继续下一关")
                raid_skipped.append(f"{code}（{err}）")
                if args.webhook_every_stage:
                    notify(
                        "raid_skipped",
                        f"[突袭跳过] {cfg.get('account')} · {code}/突袭",
                        err,
                        {"event": args.event, "account": cfg.get("account"), "stage": code},
                    )
                break
            print(f"   普通关失败（{err}），继续下一关")
            failed.append((code, err))
            if args.webhook_every_stage:
                notify(
                    "stage_failed",
                    f"[推关失败] {cfg.get('account')} · {code}/普通",
                    err,
                    {"event": args.event, "account": cfg.get("account"), "stage": code},
                )
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

    summary_lines = [
        f"完成（{len(done)}）：{', '.join(done) if done else '无'}",
        f"跳过已 3★（{len(skipped)}）：{', '.join(skipped) if skipped else '无'}",
    ]
    if stars_short:
        summary_lines.append(f"未达 3★：{'; '.join(stars_short)}")
    if raid_skipped:
        summary_lines.append(f"突袭跳过：{'; '.join(raid_skipped)}")
    if potions_used:
        summary_lines.append(f"理智药消耗：{potions_used}")
    if failed:
        summary_lines.append(f"失败/停手：{'; '.join(f'{c}: {e}' for c, e in failed)}")
    notify(
        "batch_finish",
        f"[推关汇总] {cfg.get('account')} · {args.event}（完成 {len(done)} / 失败 {len(failed)}）",
        "\n".join(summary_lines),
        {
            "event": args.event,
            "account": cfg.get("account"),
            "done": done,
            "skipped": skipped,
            "stars_short": stars_short,
            "raid_skipped": raid_skipped,
            "failed": failed,
            "potions_used": potions_used,
        },
    )

    if failed:
        run_task("收结算页", timeout=180, log=print)
        return 1
    run_task("收结算页", timeout=180, log=print)
    return 0


if __name__ == "__main__":
    sys.exit(main())
