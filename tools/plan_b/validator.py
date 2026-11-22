# -*- coding: utf-8 -*-
"""Plan B：作业静态校验器 —— 把 AI 写的 copilot JSON 在交给 MaaCore 之前先预检。

校验项（error 直接打回修，不浪费演习券）：
  必填字段 / 动作类型 / 干员是否在名册 / skill 序号 / Deploy 坐标在边界内且是可部署格 /
  方向合法 / kills·costs 类型 / 高台-地面粗校验（格子必须可部署；有职业信息时再精确）

用法：
  python tools/plan_b/validator.py --job candidate.json --bundle <bundle目录>
  # 名册来源优先级：--roster > bundle/roster_entries.json > picker API（--picker）
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "arknights_agent"))

from tile_map import Level  # noqa: E402

DIRECTIONS = {"Left", "Right", "Up", "Down", "None"}
# 常见动作类型（MaaCore copilot v3）
ACTION_TYPES = {"Deploy", "Skill", "UseSkill", "Retreat", "SkillDaemon", "SpeedUp",
                "BulletTime", "MoveCamera", "WaitTimes", "OutputText", "Output", "Move"}
GROUND_CLASSES = {"先锋", "近卫", "重装", "特种"}          # 地面
HIGH_CLASSES = {"狙击", "术师", "辅助", "医疗"}            # 高台


def load_level(bundle: pathlib.Path) -> tuple[Level, dict]:
    ctx = json.loads((bundle / "context.json").read_text(encoding="utf-8"))
    tile_file = pathlib.Path(ctx["stage"]["tileFile"])
    lv = Level(json.loads(tile_file.read_text(encoding="utf-8")), tile_file.name)
    if not lv.width:
        lv.width = len(lv.tiles[0]) if lv.tiles else 0
    return lv, ctx


def load_roster(bundle: pathlib.Path, roster_file: str, picker: str, maa_dir: str) -> tuple[dict, str]:
    """返回 {name: entry} + 来源说明。拿不到就返回空表（后续跳过干员名校验并给 warning）。"""
    if roster_file:
        data = json.loads(pathlib.Path(roster_file).read_text(encoding="utf-8"))
        entries = data.get("own_opers") or data.get("entries") or []
    elif (bundle / "roster_entries.json").exists():
        entries = json.loads((bundle / "roster_entries.json").read_text(encoding="utf-8"))
    else:
        try:
            body = json.dumps({"dir": maa_dir}).encode()
            req = urllib.request.Request(picker.rstrip("/") + "/api/box/maa", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                entries = json.loads(r.read().decode()).get("entries") or []
        except Exception as e:  # noqa: BLE001
            return {}, f"名册读取失败：{e}"
    out = {}
    for e in entries:
        name = e.get("name") or ""
        if not name:
            continue
        out[name] = {
            "elite": int(e.get("elite", 0) or 0),
            "level": int(e.get("level", 0) or 0),
            "rarity": int(e.get("rarity", 0) or 0),
            "class": e.get("class") or e.get("profession") or "",
        }
    return out, f"{len(out)} 人（{roster_file or 'picker/boxAPI'}）"


def validate(job: dict, lv: Level, ctx: dict, roster: dict, raid: bool) -> tuple[list[str], list[str]]:
    errs: list[str] = []
    warns: list[str] = []

    if not isinstance(job, dict):
        return ["作业顶层必须是对象"], []
    for f in ("stage_name", "opers", "actions"):
        if f not in job:
            errs.append(f"缺必填字段 {f}")
    if errs:
        return errs, warns

    expect_stage = ctx["stage"]["stageName"]
    if job.get("stage_name") != expect_stage:
        errs.append(f"stage_name={job.get('stage_name')!r} 与目标关卡 {expect_stage!r} 不一致")
    diff = job.get("difficulty", 1)
    if raid and diff != 3:
        errs.append(f"目标是突袭，但 difficulty={diff}（突袭必须写 3）")
    if not raid and diff == 3:
        warns.append("目标是普通关但 difficulty=3（会被当成突袭）")

    title = ((job.get("doc") or {}) or {}).get("details", "") or ""
    if raid and ("不兼容突袭" in title or "仅普通" in title):
        warns.append("作业 doc 里写着不兼容突袭/仅普通，突袭大概率打不过，仅作参考")

    # 干员
    opers = job.get("opers") or []
    if not isinstance(opers, list) or not opers:
        errs.append("opers 必须是非空数组")
        opers = []
    seen = set()
    for i, o in enumerate(opers):
        if not isinstance(o, dict) or not o.get("name"):
            errs.append(f"opers[{i}] 缺 name")
            continue
        name = o["name"]
        if name in seen:
            warns.append(f"opers 里 {name} 重复")
        seen.add(name)
        skill = o.get("skill", 1)
        if not isinstance(skill, int) or skill not in (1, 2, 3):
            errs.append(f"{name}: skill={skill!r} 非法（1/2/3）")
        su = o.get("skill_usage", 0)
        if su not in (0, 1, 2):
            errs.append(f"{name}: skill_usage={su!r} 非法（0/1/2）")
        if roster and name not in roster:
            warns.append(f"{name} 不在本机名册里（可能是装置或名字写错）")
        if roster and name in roster:
            req = o.get("requirements") or {}
            r_el = req.get("elite")
            r_lv = req.get("level")
            owned = roster[name]
            if isinstance(r_el, int) and r_el > owned["elite"]:
                errs.append(f"{name}: 作业要精英 {r_el}，号里只有 {owned['elite']}")
            if isinstance(r_lv, int) and r_lv > owned["level"] and owned["elite"] <= (r_el or 0):
                warns.append(f"{name}: 作业要等级 {r_lv}，号里 {owned['level']}")

    # 动作
    actions = job.get("actions")
    if not isinstance(actions, list) or not actions:
        errs.append("actions 必须是非空数组")
        actions = []
    deploy_count = 0
    for i, a in enumerate(actions):
        if not isinstance(a, dict):
            errs.append(f"actions[{i}] 必须是对象")
            continue
        at = a.get("type")
        if at not in ACTION_TYPES:
            errs.append(f"actions[{i}] 未知动作类型 {at!r}")
            continue
        for k in ("kills", "costs"):
            if k in a and not isinstance(a[k], int):
                errs.append(f"actions[{i}].{k} 必须是整数")
        if at != "Deploy":
            continue
        deploy_count += 1
        name = a.get("name") or a.get("operator")
        if not name:
            errs.append(f"actions[{i}] Deploy 缺 name")
        loc = a.get("location")
        if not isinstance(loc, list) or len(loc) != 2 or not all(isinstance(v, int) for v in loc):
            errs.append(f"actions[{i}] Deploy location 必须是 [x,y] 整数")
            continue
        x, y = loc
        t = lv.at(x, y)
        if t is None:
            errs.append(f"{name or '?'} 部署点 [{x},{y}] 越界（地图 {lv.width}x{lv.height}）")
            continue
        if not t.deployable:
            errs.append(f"{name or '?'} 部署点 [{x},{y}] 不可部署（buildable={t.buildable}, key={t.key}）")
        d = a.get("direction")
        if d is not None and d not in DIRECTIONS:
            errs.append(f"{name or '?'} direction={d!r} 非法")
        if name and roster and name in roster and roster[name].get("class"):
            cls = roster[name]["class"]
            if cls in HIGH_CLASSES and t.ground_deployable:
                warns.append(f"{name}({cls}) 是远程职业却放在地面格 [{x},{y}]")
            if cls in GROUND_CLASSES and t.high_deployable:
                warns.append(f"{name}({cls}) 是近战职业却放在高台格 [{x},{y}]")
    if deploy_count == 0:
        warns.append("整份作业没有 Deploy 动作（纯技能轴？确认一下）")
    return errs, warns


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Plan B 作业静态校验")
    ap.add_argument("--job", required=True, help="candidate.json 路径")
    ap.add_argument("--bundle", required=True, help="context_builder 生成的 bundle 目录")
    ap.add_argument("--roster", default="", help="OperBoxData.json（默认从 picker API 读）")
    ap.add_argument("--picker", default="http://127.0.0.1:8790")
    ap.add_argument("--maa-dir", default=r"D:\Xiach\M-A\MAA\MAA-v6.12.2-win-x64")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    args = ap.parse_args(argv)

    bundle = pathlib.Path(args.bundle)
    job = json.loads(pathlib.Path(args.job).read_text(encoding="utf-8"))
    lv, ctx = load_level(bundle)
    roster, roster_src = load_roster(bundle, args.roster, args.picker, args.maa_dir)
    if not roster:
        print(f"[warn] {roster_src}（跳过干员名校验）", file=sys.stderr)
    errs, warns = validate(job, lv, ctx, roster, bool(ctx["stage"].get("raid")))
    result = {"job": str(args.job), "rosterSource": roster_src, "errors": errs, "warnings": warns,
              "ok": not errs}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for w in warns:
            print(f"[warn] {w}")
        for e in errs:
            print(f"[error] {e}")
        print(f"\n== validator: {len(errs)} error, {len(warns)} warn（名册：{roster_src}）")
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
