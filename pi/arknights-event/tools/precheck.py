# -*- coding: utf-8 -*-
"""PI 项目自检：多层资源叠加 / ROI 边界 / 模板存在 / 节点引用 / 任务入口与可达性。

防的是「写 JSON 时把 [x,y,w,h] 写成 [x1,y1,x2,y2]」这类静默错误——越界的节点
不会报错，只在 maafw.log 里留一条 warn，然后永远识别失败（2026-11-22 踩过 12 个）。

支持 interface.json 的 resource[].path 多层叠加（如 resource + resource/common +
resource/events/<event>）：按顺序加载各层 pipeline/*.json 合并节点（后层可覆写前层），
跨层校验 next / on_error 引用（兼容 [JumpBack] / [Anchor] 等节点前缀），并在多层
image/ 目录中查找模板图。

用法：
    python pi/arknights-event/tools/precheck.py            # 检查整个项目
    python pi/arknights-event/tools/precheck.py --strict    # 有 warn 也返回 1

退出码：0 = 通过；1 = 有 error（或 --strict 下有 warn）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # pi/arknights-event

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
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warns: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)


def _rel(path: Path) -> str:
    try:
        return path.relative_to(HERE).as_posix()
    except ValueError:
        return str(path)


def _parse_node_ref(ref) -> str | None:
    """解析 next / on_error 中的节点引用，兼容 [JumpBack]NodeName 与对象格式。"""
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
            f"{node}: roi 越界 {roi!r} → x+w={x + w}（须≤{SCREEN_W}）, y+h={y + h}（须≤{SCREEN_H}）；"
            f"注意 MaaFw 是 [x,y,w,h] 不是 [x1,y1,x2,y2]"
        )


def _find_template(image_dirs: list[Path], rel_tpl: str) -> Path | None:
    norm = rel_tpl.replace("\\", "/")
    # 后加载层优先覆盖前层
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
            rep.err(f"{node}: 模板文件在所有资源层的 image/ 中都不存在：{t}")
        elif f.stat().st_size < 100:
            rep.err(f"{node}: 模板文件过小（疑似坏图）{_rel(f)}")


def _check_node_schema(rep: Report, name: str, node: dict, image_dirs: list[Path]) -> None:
    rec = node.get("recognition") or {}
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
    if rtype == "OCR":
        exp = param.get("expected")
        if not exp:
            rep.err(f"{name}: OCR 节点必须有 expected")

    act = node.get("action") or {}
    if not isinstance(act, dict):
        rep.err(f"{name}: action 必须是对象")
        return
    atype = act.get("type", "DoNothing")
    if atype not in VALID_ACTIONS:
        rep.err(f"{name}: 未知 action.type={atype!r}")
    act_param = act.get("param") or {}
    if not isinstance(act_param, dict):
        rep.err(f"{name}: action.param 必须是对象")
        act_param = {}
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


def load_pipeline_file(path: Path, rep: Report) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        rep.err(f"{_rel(path)}: 解析失败：{e}")
        return {}
    if not isinstance(data, dict):
        rep.err(f"{_rel(path)}: 顶层必须是对象")
        return {}
    return data


def check_resource_stack(
    rep: Report, res_name: str, layer_rel_paths: list[str]
) -> tuple[dict[str, dict], int, int]:
    """按 resource[].path 顺序叠加各层 pipeline 与 image，校验所有节点与跨层引用。"""
    layer_dirs: list[Path] = []
    image_dirs: list[Path] = []
    for rp in layer_rel_paths:
        layer_dir = HERE / rp
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
        layer_seen: dict[str, str] = {}
        for f in sorted(pipe_dir.glob("*.json")):
            file_count += 1
            data = load_pipeline_file(f, rep)
            for name, node in data.items():
                if not isinstance(node, dict):
                    rep.err(f"{_rel(f)}: 节点 {name} 必须是对象")
                    continue
                if name in layer_seen:
                    rep.warn(
                        f"{_rel(f)}: 同一资源层内重复定义节点 {name}（前定义在 {layer_seen[name]}）"
                    )
                layer_seen[name] = _rel(f)
                merged[name] = node

    if not merged:
        rep.err(f"resource {res_name}: 叠加所有层后没有任何 pipeline 节点")
        return merged, file_count, len(layer_dirs)

    for name, node in merged.items():
        _check_node_schema(rep, name, node, image_dirs)

    # 跨层节点引用校验（合并后再检查，支持 common ↔ event_override 双向引用）
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

    return merged, file_count, len(layer_dirs)


def check_project(rep: Report) -> tuple[int, int, int]:
    p = HERE / "interface.json"
    if not p.exists():
        rep.err("interface.json 不存在")
        return 0, 0, 0
    try:
        itf = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        rep.err(f"interface.json 解析失败：{e}")
        return 0, 0, 0

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

    resources = itf.get("resource") or []
    tasks = itf.get("task") or []
    total_files = 0
    total_layers = 0
    total_nodes = 0

    for res in resources:
        res_name = res.get("name") or "<unnamed>"
        layer_paths = res.get("path") or []
        merged, f_cnt, l_cnt = check_resource_stack(rep, res_name, layer_paths)
        total_files += f_cnt
        total_layers += l_cnt
        total_nodes += len(merged)

        for task in tasks:
            entry = task.get("entry")
            if not entry:
                rep.err(f"task {task.get('name')}: 缺 entry")
            elif entry not in merged:
                rep.err(f"task {task.get('name')}: entry {entry!r} 不在 resource {res_name} 合并后的 pipeline 里")

        # 可达性：从所有 task entry 出发 BFS（沿 next 和 on_error）
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
            rep.warn(
                f"resource {res_name}: 节点 {name} 从任何 task entry 都不可达（如非备用节点，检查 next 链）"
            )

    return total_layers, total_files, total_nodes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="有 warn 也返回 1")
    args = ap.parse_args()

    rep = Report()
    layers, files, nodes = check_project(rep)

    for w in rep.warns:
        print(f"[warn] {w}")
    for e in rep.errors:
        print(f"[error] {e}")
    print(
        f"\n== precheck: {len(rep.errors)} error, {len(rep.warns)} warn "
        f"（{layers} 个资源层，{files} 个 pipeline 文件，{nodes} 个合并节点）"
    )
    if rep.errors or (args.strict and rep.warns):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
