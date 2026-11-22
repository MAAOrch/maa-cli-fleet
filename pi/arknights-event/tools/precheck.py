# -*- coding: utf-8 -*-
"""PI 项目自检：ROI 边界 / 模板存在 / 节点引用 / 任务入口。

防的是「写 JSON 时把 [x,y,w,h] 写成 [x1,y1,x2,y2]」这类静默错误——越界的节点
不会报错，只在 maafw.log 里留一条 warn，然后永远识别失败（2026-11-22 踩过 12 个）。

用法：
    python pi/arknights-event/tools/precheck.py            # 检查整个项目
    python pi/arknights-event/tools/precheck.py --strict    # 有 warn 也返回 1

退出码：0 = 通过；1 = 有 error（或 --strict 下有 warn）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # pi/arknights-event
RESOURCE = HERE / "resource"
IMAGE = RESOURCE / "image"
PIPELINE = RESOURCE / "pipeline"

SCREEN_W, SCREEN_H = 1280, 720
VALID_RECOGNITION = {"DirectHit", "TemplateMatch", "OCR", "FeatureMatch", "ColorMatch", "NeuralNetworkClassify", "NeuralNetworkDetect", "Custom"}
VALID_ACTIONS = {"DoNothing", "Click", "Swipe", "ClickKey", "InputText", "StartApp", "StopApp", "StopTask", "Custom", "Scroll", "Command"}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warns: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)


def _check_roi(rep: Report, node: str, roi) -> None:
    if roi is None:
        return
    if not isinstance(roi, list) or len(roi) != 4 or not all(isinstance(v, int) for v in roi):
        rep.err(f"{node}: roi 必须是 [x,y,w,h] 四个整数，实际 {roi!r}")
        return
    x, y, w, h = roi
    if w <= 0 or h <= 0:
        rep.err(f"{node}: roi 宽高必须为正，实际 {roi!r}")
    if x < 0 or y < 0:
        rep.err(f"{node}: roi 坐标不能为负，实际 {roi!r}")
    if x + w > SCREEN_W or y + h > SCREEN_H:
        rep.err(
            f"{node}: roi 越界 {roi!r} → x+w={x + w}（须≤{SCREEN_W}）, y+h={y + h}（须≤{SCREEN_H}）；"
            f"注意 MaaFw 是 [x,y,w,h] 不是 [x1,y1,x2,y2]"
        )


def _check_templates(rep: Report, node: str, templates) -> None:
    if templates is None:
        return
    items = templates if isinstance(templates, list) else [templates]
    if not items:
        rep.err(f"{node}: template 列表为空")
        return
    for t in items:
        if not isinstance(t, str) or not t.strip():
            rep.err(f"{node}: template 不是合法路径：{t!r}")
            continue
        f = IMAGE / t.replace("\\", "/")
        if not f.exists():
            rep.err(f"{node}: 模板文件不存在 {f.relative_to(HERE)}")
        elif f.stat().st_size < 100:
            rep.err(f"{node}: 模板文件过小（疑似坏图）{f.relative_to(HERE)}")


def check_pipeline(path: Path, rep: Report) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        rep.err(f"{path.name}: 解析失败：{e}")
        return {}
    if not isinstance(data, dict) or not data:
        rep.err(f"{path.name}: 顶层必须是非空对象")
        return {}
    for name, node in data.items():
        if not isinstance(node, dict):
            rep.err(f"{name}: 节点必须是对象")
            continue
        rec = node.get("recognition") or {}
        rtype = rec.get("type", "DirectHit")
        if rtype not in VALID_RECOGNITION:
            rep.err(f"{name}: 未知 recognition.type={rtype!r}")
        param = rec.get("param") or {}
        _check_roi(rep, name, param.get("roi"))
        _check_templates(rep, name, param.get("template"))
        if rtype == "OCR":
            exp = param.get("expected")
            if not exp:
                rep.err(f"{name}: OCR 节点必须有 expected")
        act = node.get("action") or {}
        atype = act.get("type", "DoNothing")
        if atype not in VALID_ACTIONS:
            rep.err(f"{name}: 未知 action.type={atype!r}")
        tgt = (act.get("param") or {}).get("target")
        if tgt is not None:
            if not isinstance(tgt, list) or len(tgt) != 4:
                rep.err(f"{name}: click target 必须是 [x,y,w,h]，实际 {tgt!r}")
            elif tgt[0] < 0 or tgt[1] < 0 or tgt[0] + tgt[2] > SCREEN_W or tgt[1] + tgt[3] > SCREEN_H:
                rep.err(f"{name}: click target 越界 {tgt!r}")
        nxt = node.get("next")
        if nxt is not None and not isinstance(nxt, list):
            rep.err(f"{name}: next 必须是数组")
    # 节点引用
    for name, node in data.items():
        for ref in node.get("next") or []:
            if isinstance(ref, str) and ref not in data:
                rep.err(f"{name}: next 引用了不存在的节点 {ref!r}")
    return data


def check_interface(rep: Report, pipelines: dict) -> None:
    p = HERE / "interface.json"
    if not p.exists():
        rep.err("interface.json 不存在")
        return
    try:
        itf = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        rep.err(f"interface.json 解析失败：{e}")
        return
    if itf.get("interface_version") != 2:
        rep.err(f"interface_version 必须是 2，实际 {itf.get('interface_version')!r}")
    for field in ("name", "version", "controller", "resource", "task"):
        if not itf.get(field):
            rep.err(f"interface.json 缺字段 {field}")
    for ctrl in itf.get("controller") or []:
        if ctrl.get("type") != "Adb":
            continue
        if ctrl.get("display_short_side") != 720:
            rep.warn(f"controller {ctrl.get('name')}: display_short_side 不是 720（模板按 1280x720 裁）")
    for res in itf.get("resource") or []:
        for rp in res.get("path") or []:
            if not (HERE / rp).exists():
                rep.err(f"resource {res.get('name')}: path 不存在 {rp}")
    all_nodes = {k for d in pipelines.values() for k in d}
    for task in itf.get("task") or []:
        entry = task.get("entry")
        if not entry:
            rep.err(f"task {task.get('name')}: 缺 entry")
        elif entry not in all_nodes:
            rep.err(f"task {task.get('name')}: entry {entry!r} 不在任何 pipeline 里")
    # 可达性：从所有 task entry 出发 BFS，找不到的节点给 warn（可能是故意留的备用节点）
    reachable = set()
    for task in itf.get("task") or []:
        entry = task.get("entry")
        if not entry:
            continue
        stack = [entry]
        while stack:
            cur = stack.pop()
            if cur in reachable:
                continue
            reachable.add(cur)
            node = next((d[cur] for d in pipelines.values() if cur in d), None)
            if node:
                stack.extend(node.get("next") or [])
    for name in sorted(all_nodes - reachable):
        rep.warn(f"节点 {name} 从任何 task entry 都不可达（如非备用节点，检查 next 链）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="有 warn 也返回 1")
    args = ap.parse_args()

    rep = Report()
    pipelines = {}
    files = sorted(PIPELINE.glob("*.json"))
    if not files:
        rep.err(f"{PIPELINE} 下没有 pipeline json")
    for f in files:
        pipelines[f.name] = check_pipeline(f, rep)
    check_interface(rep, pipelines)

    for w in rep.warns:
        print(f"[warn] {w}")
    for e in rep.errors:
        print(f"[error] {e}")
    print(f"\n== precheck: {len(rep.errors)} error, {len(rep.warns)} warn "
          f"（{len(pipelines)} 个 pipeline 文件，{sum(len(d) for d in pipelines.values())} 个节点）")
    if rep.errors or (args.strict and rep.warns):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
