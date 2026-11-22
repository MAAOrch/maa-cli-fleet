# -*- coding: utf-8 -*-
"""Plan B：情报打包 —— 地图网格 / 名册摘要 / 失败作业 Seed / 提示词包。

产物（一个 bundle 目录）：
  context.json      机器可读的总情报（网格二维、可部署点、Seed 作业列表、名册路径）
  grid.txt          ASCII 网格（带坐标轴）
  grid_topdown.png  按游戏视角绘制、每格标 [x,y]（与作业 JSON 的 location 一致）
  grid_bottomup.png 同一张图但按数学坐标标注（左下为原点、y 向上），方便另一种读法
  roster.md         精二/高练度干员摘要
  seeds/*.json      参考作业（失败记录里挑的）
  prompt_pack.md    给 AI/人的改轴提示词包

用法：
  python tools/plan_b/context_builder.py --stage act46side_s02 --code OS-S-2 --raid \
      --out D:\\copilot-picker-local\\runs\\plan_b\\20261122-2300
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from datetime import datetime

import cv2
import numpy as np

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "arknights_agent"))

from tile_map import Level, ResourcePaths
from game_knowledge import inject_knowledge, roster_prompt  # noqa: E402

DEFAULT_RUNS = pathlib.Path(r"D:\copilot-picker-local\runs")
DEFAULT_PICKER = "http://127.0.0.1:8790"

# 格子字符表（ASCII 用）
SYM_FORBIDDEN = "#"
SYM_GROUND = "."
SYM_HIGH = "^"
SYM_START = "S"
SYM_END = "E"
SYM_WALK = ","
SYM_SPECIAL = "o"


def _cell_char(t) -> str:
    """格子符号。

    MAA 格点表语义（实测校准）：
      isStart = 敌人出生点（含飞行起点）；isEnd = 保护目标/蓝门
      buildableType: 1 = 地面可部署，2 = 高台可部署，0 = 不可部署
      tile_wall + bt2 = 高台（可以站人），不是“墙不能部署”
    """
    if t.is_end:
        return SYM_END
    if t.is_start:
        return SYM_START
    if t.key in ("tile_forbidden", "tile_hole", "tile_fence"):
        return SYM_FORBIDDEN
    if t.buildable == 2:
        return SYM_HIGH
    if t.buildable == 1:
        return SYM_GROUND
    if t.key == "tile_wall":
        return SYM_FORBIDDEN
    if t.height_type == 0:
        return SYM_WALK
    return SYM_SPECIAL


def load_level(tile_dir: pathlib.Path, stage_name: str, raid: bool) -> tuple[Level, pathlib.Path]:
    """按 stageName 找格点表；突袭优先用 `#f#` 变体（有额外禁部署格）。"""
    files = sorted(tile_dir.glob(f"{stage_name}*.json"))
    if not files:
        raise FileNotFoundError(f"{tile_dir} 里没有 {stage_name} 的格点表")
    pick = None
    for f in files:
        if raid and "#f#" in f.name:
            pick = f
            break
    if pick is None:
        pick = next((f for f in files if "#f#" not in f.name), files[0])
    data = json.loads(pick.read_text(encoding="utf-8"))
    lv = Level(data, pick.name)
    if not lv.width:
        lv.width = len(lv.tiles[0]) if lv.tiles else 0
    return lv, pick


def grid_ascii(lv: Level) -> str:
    """ASCII 网格：按游戏视角（row 0 = 最上排）。"""
    lines = ["坐标：x 向右，y 向下；y=0 是最上排（与作业 JSON location 一致）", ""]
    header = "    " + "".join(f"{x:>2}" for x in range(lv.width))
    lines.append(header)
    for y in range(lv.height):
        row = "".join(_cell_char(lv.at(x, y)) + " " for x in range(lv.width))
        lines.append(f"y={y:<2} {row}")
    lines.append("")
    lines.append("图例： . 地面可部署  ^ 高台可部署  , 可行走地面(不可部署)  # 不可通行/禁部署  S 敌人出生点  E 保护目标(蓝门)  o 特殊")
    return "\n".join(lines)


def _draw_grid(lv: Level, title: str, bottomup: bool) -> np.ndarray:
    """画网格图：每格写 [x,y]，高台/地面/禁部署用不同底色。"""
    cell, pad, top, bottom = 54, 46, 78, 34
    w = lv.width * cell + pad * 2
    h = lv.height * cell + pad + top + bottom
    img = np.full((h, w, 3), 245, dtype=np.uint8)
    colors = {
        SYM_FORBIDDEN: (70, 70, 70),
        SYM_GROUND: (235, 245, 235),
        SYM_HIGH: (235, 225, 190),
        SYM_START: (230, 200, 160),
        SYM_END: (180, 180, 240),
        SYM_WALK: (225, 235, 245),
        SYM_SPECIAL: (200, 200, 255),
    }
    # 只用 ASCII 画字：cv2 不带中文字体，中文会渲染成方块
    cv2.putText(img, title, (pad, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (30, 30, 30), 2, cv2.LINE_AA)
    note = ("y=0 = TOP row; [x,y] same as Deploy location in job JSON"
            if not bottomup else "y=0 = BOTTOM row; math coords, y up")
    cv2.putText(img, note, (pad, 54), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (90, 90, 90), 1, cv2.LINE_AA)
    cv2.putText(img, "legend: . ground  ^ highland  , walkable  # blocked  S spawn  E goal",
                (pad, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (60, 60, 60), 1, cv2.LINE_AA)

    for r in range(lv.height):
        for x in range(lv.width):
            y = r if not bottomup else lv.height - 1 - r
            t = lv.at(x, y)
            sym = _cell_char(t)
            x0 = pad + x * cell
            y0 = top + pad * 0 + r * cell
            cv2.rectangle(img, (x0, y0), (x0 + cell - 2, y0 + cell - 2), colors.get(sym, (235, 235, 235)), -1)
            cv2.rectangle(img, (x0, y0), (x0 + cell - 2, y0 + cell - 2), (120, 120, 120), 1)
            fg = (250, 250, 250) if sym == SYM_FORBIDDEN else (40, 40, 40)
            cv2.putText(img, sym, (x0 + 4, y0 + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.42, fg, 1, cv2.LINE_AA)
            cv2.putText(img, f"[{x},{y}]", (x0 + 3, y0 + cell - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (20, 20, 160), 1, cv2.LINE_AA)
    # 坐标轴
    for x in range(lv.width):
        cv2.putText(img, str(x), (pad + x * cell + cell // 2 - 6, top - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (20, 20, 20), 1)
    for r in range(lv.height):
        y = r if not bottomup else lv.height - 1 - r
        cv2.putText(img, str(y), (8, top + r * cell + cell // 2 + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (20, 20, 20), 1)
    return img


def fetch_roster(picker: str, maa_dir: str | None) -> list[dict]:
    """从 picker 的 /api/box/maa 读名册（MAA 的 OperBoxData.json）。"""
    import urllib.request

    body = json.dumps({"dir": maa_dir} if maa_dir else {}).encode()
    req = urllib.request.Request(picker.rstrip("/") + "/api/box/maa", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode())
    return data.get("entries") or []


def summarise_roster(entries: list[dict], min_elite: int = 2, min_level: int = 60) -> str:
    """精二高练度摘要（Markdown 表）。名册里没有职业/技能专精，如实标注。"""
    rows = []
    for e in entries:
        elite = int(e.get("elite") or 0)
        level = int(e.get("level") or 0)
        if elite < min_elite or level < min_level:
            continue
        rows.append((int(e.get("rarity") or 0), level, e.get("name", ""), elite, int(e.get("potential") or 0)))
    rows.sort(key=lambda r: (-r[0], -r[1], r[2]))
    out = [f"# 名册摘要（精英化 ≥{min_elite}、等级 ≥{min_level}，共 {len(rows)} 人）", "",
           "> 名册来自 MAA OperBoxData.json，只有稀有度/精英化/等级/潜能够；"
           "职业与技能专精请以游戏内为准（validator 只能校验干员是否存在与练度）。", "",
           "| 干员 | 稀有度 | 精英化 | 等级 | 潜能 |", "|---|---|---|---|---|"]
    for rar, lv, name, el, pot in rows:
        out.append(f"| {name} | {rar} | {el} | {lv} | {pot} |")
    return "\n".join(out)


def collect_seeds(stage_name: str, runs_dir: pathlib.Path, limit: int = 2,
                  seed_job: int | None = None) -> list[dict]:
    """从历史失败运行里挑参考作业（有战报用击杀数，没有就取最近的）。"""
    cands = []
    for d in sorted(runs_dir.glob("*"), reverse=True):
        if not d.is_dir():
            continue
        for jf in d.glob("job-*.json"):
            try:
                job = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if job.get("stage_name") != stage_name:
                continue
            m = re.match(r"job-(\d+)\.json", jf.name)
            jid = int(m.group(1)) if m else 0
            if seed_job is not None and jid != seed_job:
                continue
            cands.append({"jobId": jid, "path": str(jf), "title": (job.get("doc") or {}).get("title", ""),
                          "runDir": d.name, "job": job})
            break
    if seed_job is not None and not cands:
        raise SystemExit(f"runs 里找不到 #{seed_job} 的作业文件（stage_name={stage_name}）")
    # 同 id 去重，取最近的一次
    seen, out = set(), []
    for c in cands:
        if c["jobId"] in seen:
            continue
        seen.add(c["jobId"])
        out.append(c)
        if len(out) >= limit:
            break
    return out


PROMPT_RULES = """## 硬性约束（写轴时逐条遵守）

1. 输出必须是**单个** copilot JSON 文件（MaaCore 格式，`version: 3`），顶层字段：
   `stage_name` / `difficulty`（1 普通，3 突袭）/ `opers` / `groups` / `actions` / `doc`。
2. 部署动作：`{{"type":"Deploy","name":"干员名","location":[x,y],"direction":"Left|Right|Up|Down|None","skill_usage":0|1|2,"kills":N|"costs":N}}`
   其中 `location` 必须落在网格里**可部署**的格子上（`.` 地面 / `^` 高台），
   高台职业（狙击/术师/辅助/医疗）只能上 `^`，地面职业（先锋/近卫/重装/部分特种）只能上 `.`。
3. **优先塞满我方高练度主力**：用名册摘要里的精二 90 干员平推，别写极限低配轴。
4. 技能：非关键轴干员一律 `skill_usage: 2`（好了就开）；只有需要精确时机才用 `kills` / `costs` 触发。
5. 触发：优先 `costs`（费用到 N 部署）/ `kills`（击杀到 N 放技能/撤退），**少用死等延迟**。
6. 干员名必须是名册里真实存在的名字；装置（如「落雪防护装置」）按作业站原名照抄。
7. 不要改 `stage_name`；不要输出注释以外的多余字段。

## 上一轮战报怎么用

`battle_report.json` 里：
- `stars`：结算星数（0/1/2/3）。目标 3★。
- `kills`：最终击杀数（`27/57` 这类），击杀数高说明输出够、多半是漏怪/阻挡不够。
- `frames`：关键帧（蓝门掉血 / 费用卡满 / 结束），对照画面找崩盘点。
- `asst_errors`：MAA 最后报错的子任务（如 `Deploy` 动作失败 = 坐标或费用不对）。
"""


def build_bundle(args) -> pathlib.Path:
    out = pathlib.Path(args.out).resolve()
    (out / "seeds").mkdir(parents=True, exist_ok=True)
    rp = ResourcePaths(args.resource_dir) if args.resource_dir else ResourcePaths()
    tile_dir = rp.tile_pos_dir
    lv, tile_file = load_level(tile_dir, args.stage, args.raid)

    (out / "grid.txt").write_text(grid_ascii(lv), encoding="utf-8")
    title = f"{args.code} {lv.name} ({args.stage}{' 突袭' if args.raid else ''}) {lv.width}x{lv.height}"
    cv2.imwrite(str(out / "grid_topdown.png"), _draw_grid(lv, title, bottomup=False))
    cv2.imwrite(str(out / "grid_bottomup.png"), _draw_grid(lv, title, bottomup=True))

    roster_entries: list[dict] = []
    roster_err = ""
    try:
        roster_entries = fetch_roster(args.picker, args.maa_dir)
        (out / "roster.md").write_text(summarise_roster(roster_entries), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        roster_err = str(e)
        (out / "roster.md").write_text(f"# 名册读取失败\n\n{e}\n", encoding="utf-8")

    seeds = collect_seeds(args.stage, pathlib.Path(args.runs_dir), args.seed_limit, args.seed_job)
    seed_meta = []
    for s in seeds:
        dst = out / "seeds" / f"job-{s['jobId']}.json"
        dst.write_text(json.dumps(s["job"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        seed_meta.append({k: s[k] for k in ("jobId", "title", "runDir", "path")} | {"bundlePath": str(dst)})

    deploy_ground = sorted(lv.ground_spots)
    deploy_high = sorted(lv.high_spots)
    ctx = {
        "stage": {"code": args.code, "stageName": args.stage, "raid": args.raid,
                  "name": lv.name, "width": lv.width, "height": lv.height,
                  "tileFile": str(tile_file)},
        "map": {"groundDeploy": [list(p) for p in deploy_ground], "highDeploy": [list(p) for p in deploy_high],
                "starts": [list(p) for p in lv.starts], "ends": [list(p) for p in lv.ends]},
        "seeds": seed_meta,
        "rosterCount": len(roster_entries),
        "rosterError": roster_err,
    }
    (out / "context.json").write_text(json.dumps(ctx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    prompt = ["# Plan B 改轴情报包", "", inject_knowledge(args.code, args.raid), "", roster_prompt(roster_entries), "",
              f"- 关卡：`{args.code}` / `{args.stage}`（{lv.name}，{lv.width}x{lv.height}"
              f"{'，突袭' if args.raid else '，普通'}）",
              f"- 汇总情报：`context.json`；网格：`grid.txt`、`grid_topdown.png`、`grid_bottomup.png`",
              "- 名册：`roster.md`；参考作业：`seeds/`", "",
              "把改好的作业写成 `candidate.json`（放本目录），再跑：",
              "```",
              f"python tools/plan_b/iterate.py --bundle {out} --candidate {out / 'candidate.json'}",
              "```", "", PROMPT_RULES]
    (out / "prompt_pack.md").write_text("\n".join(prompt), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Plan B 情报打包")
    ap.add_argument("--stage", required=True, help="作业站关卡代码（如 act46side_s02）")
    ap.add_argument("--code", default="", help="游戏内代号（如 OS-S-2）")
    ap.add_argument("--raid", action="store_true", help="按突袭取格点表（#f# 变体）")
    ap.add_argument("--out", required=True, help="bundle 输出目录")
    ap.add_argument("--resource-dir", default="", help="MAA resource 目录（默认自动找）")
    ap.add_argument("--runs-dir", default=str(DEFAULT_RUNS))
    ap.add_argument("--picker", default=DEFAULT_PICKER)
    ap.add_argument("--maa-dir", default=r"D:\Xiach\M-A\MAA\MAA-v6.12.2-win-x64")
    ap.add_argument("--seed-limit", type=int, default=2)
    ap.add_argument("--seed-job", type=int, default=None)
    args = ap.parse_args(argv)
    out = build_bundle(args)
    print(f"bundle 已生成：{out}")
    print(f"  grid.txt / grid_topdown.png / grid_bottomup.png / roster.md / seeds/ / prompt_pack.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
