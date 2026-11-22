# -*- coding: utf-8 -*-
"""全仓库 MaaFramework PI 项目统一自检工具。

支持单个项目（--project pi/event-shop）或全量扫描（--all，自动扫描 pi/*/interface.json）：
  - 按 interface.json 的 resource[].path 顺序叠加各层 pipeline/*.json 与 image/；
  - 校验 ROI / click target 边界（1280x720，防 [x1,y1,x2,y2] 误写导致运行时静默失败）；
  - 校验模板文件存在且非坏图、OCR 节点必填 expected、recognition/action 类型合法；
  - 合并后跨层校验 next / on_error 引用（兼容 [JumpBack] / [Anchor] 前缀）；
  - 从全部 task entry 做 BFS 可达性分析（不可达节点报 warn）。

用法：
  python tools/pi_precheck.py --all
  python tools/pi_precheck.py --project pi/ra-enter --strict
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PI_ROOT = REPO / "pi"

SCREEN_W, SCREEN_H = 1280, 720
VALID_RECOGNITION = {
    "DirectHit",
    "TemplateMatch",
    "OCR",
    "FeatureMatch",
    "ColorMatch",
    "NeuralNetworkClassify",
    "NeuralNetworkDetect",
    "Custom",
}
VALID_ACTIONS = {
    "DoNothing",
    "Click",
    "Swipe",
    "ClickKey",
    "InputText",
    "StartApp",
    "StopApp",
    "StopTask",
    "Custom",
    "Scroll",
    "Command",
}
NODE_ATTR_PREFIX_RE = re.compile(r"^(?:\[[^\]]+\])+")


class Report:
    def __init__(self, project: Path) -> None:
        self.project = project
        self.errors: list[str] = []
        self.warns: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _parse_node_ref(ref) -> str | None:
    if isinstance(ref, str):
        name = NODE_ATTR_PREFIX_RE.sub("", ref).strip()
        return name or None
    if isinstance(ref, dict):
        name = ref.get("name")
        if isinstance(name, str) and name.strip():
            return NODE_ATTR_PREFIX_RE.sub("", name).strip() or None
    return None


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
            f"{node}: roi 越界 {roi!r} → x+w={x + w}（须≤{SCREEN_W}）, y+h={y + h}（须≤{SCREEN_H}）"
        )


def _find_template(image_dirs: list[Path], rel_tpl: str) -> Path | None:
    norm = rel_tpl.replace("\\", "/")
    for img_dir in reversed(image_dirs):
        cand = img_dir / norm
        if cand.exists():
            return cand
    return None


def _check_templates(rep: Report, node: str, templates, image_dirs: list[Path]) -> None:
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
        f = _find_template(image_dirs, t)
        if f is None:
            rep.err(f"{node}: 模板文件不存在：{t}")
        elif f.stat().st_size < 100:
            rep.err(f"{node}: 模板文件过小（疑似坏图）{_rel(rep.project, f)}")


def _check_node_schema(rep: Report, name: str, node: dict, image_dirs: list[Path]) -> None:
    rec = node.get("recognition") or {}
    if isinstance(rec, str):
        # 兼容 v1 写法（recognition 为字符串）
        if rec not in VALID_RECOGNITION:
            rep.err(f"{name}: 未知 recognition={rec!r}")
        _check_roi(rep, name, node.get("roi"))
        _check_templates(rep, name, node.get("template"), image_dirs)
        return
    if not isinstance(rec, dict):
        rep.err(f"{name}: recognition 必须是对象")
        return
    rtype = rec.get("type", "DirectHit")
    if rtype not in VALID_RECOGNITION:
        rep.err(f"{name}: 未知 recognition.type={rtype!r}")
    param = rec.get("param") or {}
    if not isinstance(param, dict):
        rep.err(f"{name}: recognition.param 必须是对象")
        param = {}
    _check_roi(rep, name, param.get("roi"))
    _check_templates(rep, name, param.get("template"), image_dirs)
    if rtype == "OCR" and not param.get("expected"):
        rep.err(f"{name}: OCR 节点必须有 expected")

    act = node.get("action") or {}
    if isinstance(act, dict):
        atype = act.get("type", "DoNothing")
        if atype not in VALID_ACTIONS:
            rep.err(f"{name}: 未知 action.type={atype!r}")
        act_param = act.get("param") or {}
        if isinstance(act_param, dict):
            tgt = act_param.get("target")
            if tgt is not None:
                if not isinstance(tgt, list) or len(tgt) != 4 or not all(isinstance(v, int) for v in tgt):
                    rep.err(f"{name}: click target 必须是 [x,y,w,h] 四个整数，实际 {tgt!r}")
                elif tgt[0] < 0 or tgt[1] < 0 or tgt[0] + tgt[2] > SCREEN_W or tgt[1] + tgt[3] > SCREEN_H:
                    rep.err(f"{name}: click target 越界 {tgt!r}")

    for key in ("next", "on_error"):
        refs = node.get(key)
        if refs is not None and not isinstance(refs, list):
            rep.err(f"{name}: {key} 必须是数组")


def check_project(project_dir: Path) -> tuple[Report, int, int, int]:
    rep = Report(project_dir)
    itf_path = project_dir / "interface.json"
    if not itf_path.exists():
        rep.err("interface.json 不存在")
        return rep, 0, 0, 0
    try:
        itf = json.loads(itf_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        rep.err(f"interface.json 解析失败：{e}")
        return rep, 0, 0, 0

    if itf.get("interface_version") != 2:
        rep.err(f"interface_version 必须是 2，实际 {itf.get('interface_version')!r}")
    for field in ("name", "version", "controller", "resource", "task"):
        if not itf.get(field):
            rep.err(f"interface.json 缺字段 {field}")

    for ctrl in itf.get("controller") or []:
        if ctrl.get("type") == "Adb" and ctrl.get("display_short_side") != 720:
            rep.warn(f"controller {ctrl.get('name')}: display_short_side 不是 720")

    resources = itf.get("resource") or []
    tasks = itf.get("task") or []
    total_layers = 0
    total_files = 0
    total_nodes = 0

    for res in resources:
        res_name = res.get("name") or "<unnamed>"
        layer_dirs: list[Path] = []
        image_dirs: list[Path] = []
        for rp in res.get("path") or []:
            # 兼容 {PROJECT_DIR}/resource 占位符写法
            norm_rp = rp.replace("{PROJECT_DIR}/", "").replace("{PROJECT_DIR}\\", "")
            layer_dir = project_dir / norm_rp
            if not layer_dir.exists():
                rep.err(f"resource {res_name}: path 不存在 {rp}")
                continue
            layer_dirs.append(layer_dir)
            img_dir = layer_dir / "image"
            if img_dir.is_dir():
                image_dirs.append(img_dir)

        merged: dict[str, dict] = {}
        file_count = 0
        for layer_dir in layer_dirs:
            pipe_dir = layer_dir / "pipeline"
            if not pipe_dir.is_dir():
                continue
            for f in sorted(pipe_dir.glob("*.json")):
                file_count += 1
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except Exception as e:  # noqa: BLE001
                    rep.err(f"{_rel(project_dir, f)}: 解析失败：{e}")
                    continue
                if not isinstance(data, dict):
                    rep.err(f"{_rel(project_dir, f)}: 顶层必须是对象")
                    continue
                for name, node in data.items():
                    if not isinstance(node, dict):
                        rep.err(f"{_rel(project_dir, f)}: 节点 {name} 必须是对象")
                        continue
                    merged[name] = node

        total_layers += len(layer_dirs)
        total_files += file_count
        total_nodes += len(merged)

        if not merged:
            rep.err(f"resource {res_name}: 叠加所有层后没有任何 pipeline 节点")
            continue

        for name, node in merged.items():
            _check_node_schema(rep, name, node, image_dirs)

        for name, node in merged.items():
            for key in ("next", "on_error"):
                refs = node.get(key)
                if not isinstance(refs, list):
                    continue
                for ref in refs:
                    target = _parse_node_ref(ref)
                    if target is None:
                        rep.err(f"{name}: {key} 包含非法引用 {ref!r}")
                    elif target not in merged:
                        rep.err(f"{name}: {key} 引用了不存在的节点 {ref!r}")

        for task in tasks:
            entry = task.get("entry")
            if not entry:
                rep.err(f"task {task.get('name')}: 缺 entry")
            elif entry not in merged:
                rep.err(f"task {task.get('name')}: entry {entry!r} 不在 resource {res_name} 中")

        reachable: set[str] = set()
        for task in tasks:
            entry = task.get("entry")
            if not entry or entry not in merged:
                continue
            stack = [entry]
            while stack:
                cur = stack.pop()
                if cur in reachable:
                    continue
                reachable.add(cur)
                node = merged.get(cur)
                if not node:
                    continue
                for key in ("next", "on_error"):
                    for ref in node.get(key) or []:
                        target = _parse_node_ref(ref)
                        if target and target in merged and target not in reachable:
                            stack.append(target)

        for name in sorted(set(merged) - reachable):
            rep.warn(f"节点 {name} 从任何 task entry 都不可达（若为脚本显式入口，请补到 interface.json）")

    return rep, total_layers, total_files, total_nodes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="全仓库 MaaFramework PI 项目统一自检")
    ap.add_argument("--project", action="append", default=[], help="指定单个或多个 PI 项目目录")
    ap.add_argument("--all", action="store_true", help="扫描 pi/ 下所有包含 interface.json 的项目")
    ap.add_argument("--strict", action="store_true", help="有 warn 也返回 1")
    args = ap.parse_args(argv)

    targets: list[Path] = []
    if args.all or not args.project:
        for itf in sorted(PI_ROOT.glob("*/interface.json")):
            targets.append(itf.parent)
    else:
        for p in args.project:
            cand = Path(p)
            if not cand.is_absolute():
                cand = REPO / cand
            targets.append(cand)

    total_err = 0
    total_warn = 0
    for proj in targets:
        rep, layers, files, nodes = check_project(proj)
        rel_name = _rel(REPO, proj)
        print(f"=== {rel_name} ({layers} 层, {files} 文件, {nodes} 节点) ===")
        for w in rep.warns:
            print(f"  [warn]  {w}")
        for e in rep.errors:
            print(f"  [error] {e}")
        if not rep.errors and not rep.warns:
            print("  [ok]    0 error, 0 warn")
        total_err += len(rep.errors)
        total_warn += len(rep.warns)

    print(f"\n== pi_precheck 汇总 ({len(targets)} 个项目): {total_err} error, {total_warn} warn ==")
    if total_err or (args.strict and total_warn):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
