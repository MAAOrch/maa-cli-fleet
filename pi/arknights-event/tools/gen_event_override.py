# -*- coding: utf-8 -*-
"""生成活动层 `resource/events/<event>/pipeline/event_override.json`。

换活动时只改「关卡代号前缀 / S 关数量」，不再手工翻 2000 行 JSON 改 `OS-`：

    python pi/arknights-event/tools/gen_event_override.py --event act52side --prefix XX --s-count 5

生成内容 = 以 `act46side` 的 `event_override.json` 为结构模板，做三件事：

1. 把模板里所有 `OS-` 代号替换成 `--prefix`（默认把 `OS-` 换成 `XX-`）；
2. 按 `--s-count` 保留/裁剪 `NavStageS* / NavSDetail*Start / NavDetailCodeS* /
   NavMapThenS* / NavDetailToMapNextS*` 五组节点；
3. 重写所有包含关卡代号的 `expected` 列表（到位锚点、地图锚点、S 关详情），
   保证 `NavMapAnyBack` / `NavMapStop` 等大列表里不会残留旧前缀。

它只生成 pipeline 差异节点；模板图仍放在 `resource/image/EventNav/`
（固定文件名，换活动时覆盖同名 PNG），OCR 模型走 `resource/model`。

生成后记得：

    pwsh -File pi/arknights-event/setup_pi.ps1 -Event <event>
    python pi/arknights-event/tools/precheck.py

注意：脚本假定活动结构为 EX-1..8、主线 1..10、ST-1..4、TR-1，且双码到位校验用
EX-5 / 主线 3；若新活动结构不同，改下面的 EX_COUNT / MAIN_COUNT / ST_COUNT /
EX_ANCHOR / MAIN_ANCHOR 常量，或直接在生成结果上补节点。
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # pi/arknights-event
EVENTS_DIR = HERE / "resource" / "events"
DEFAULT_SOURCE = "act46side"

EX_COUNT = 8
MAIN_COUNT = 10
ST_COUNT = 4
TR_COUNT = 1
EX_ANCHOR = 5
MAIN_ANCHOR = 3

S_NODE_RE = re.compile(r"^(NavStageS|NavSDetail|NavDetailCodeS|NavMapThenS|NavDetailToMapNextS)(\d+)(Start)?$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="生成活动层 event_override.json（前缀/S 关数量自动替换）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python pi/arknights-event/tools/gen_event_override.py "
            "--event act52side --prefix XX --s-count 5\n"
            "  python pi/arknights-event/tools/gen_event_override.py "
            "--event act53side --prefix YY --s-count 4 --from-event act52side --from-prefix XX"
        ),
    )
    ap.add_argument("--event", required=True, help="活动 id，如 act52side（生成到 resource/events/<event>/）")
    ap.add_argument("--prefix", required=True, help="新活动关卡代号前缀，如 XX（生成 XX-1 / XX-EX-1 / XX-S-1）")
    ap.add_argument("--s-count", type=int, default=5, help="S 关数量，默认 5")
    ap.add_argument("--from-event", default=DEFAULT_SOURCE, help=f"结构模板活动，默认 {DEFAULT_SOURCE}")
    ap.add_argument("--from-prefix", default="OS", help="模板活动的关卡前缀，默认 OS")
    ap.add_argument("--event-name", default="", help="可选：把模板 doc 里的「雪山降临」替换成这个活动名")
    return ap.parse_args(argv)


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"找不到模板活动层：{path}\n先确认 --from-event 是否正确，或从仓库恢复 {DEFAULT_SOURCE}。")
    except json.JSONDecodeError as e:
        raise SystemExit(f"模板活动层 JSON 解析失败：{path}: {e}")
    if not isinstance(data, dict) or not data:
        raise SystemExit(f"模板活动层顶层必须是非空对象：{path}")
    return data


def replace_strings(obj, old_prefix: str, new_prefix: str, event_name: str):
    """递归替换所有字符串里的旧代号前缀；可选替换活动名。"""
    if isinstance(obj, str):
        text = obj.replace(f"{old_prefix}-", f"{new_prefix}-")
        if event_name:
            text = text.replace("雪山降临", event_name)
        return text
    if isinstance(obj, list):
        return [replace_strings(v, old_prefix, new_prefix, event_name) for v in obj]
    if isinstance(obj, dict):
        return {k: replace_strings(v, old_prefix, new_prefix, event_name) for k, v in obj.items()}
    return obj


def map_codes(prefix: str, s_count: int) -> list[str]:
    codes = [f"{prefix}-EX-{i}" for i in range(1, EX_COUNT + 1)]
    codes += [f"{prefix}-S-{i}" for i in range(1, s_count + 1)]
    codes += [f"{prefix}-{i}" for i in range(1, MAIN_COUNT + 1)]
    codes += [f"{prefix}-ST-{i}" for i in range(1, ST_COUNT + 1)]
    codes += [f"{prefix}-TR-{i}" for i in range(1, TR_COUNT + 1)]
    return codes


def set_expected(data: dict, node_name: str, expected: list[str]) -> None:
    node = data.get(node_name)
    if not isinstance(node, dict):
        raise SystemExit(f"模板里缺少节点 {node_name}，无法重写 expected")
    rec = node.get("recognition")
    if not isinstance(rec, dict) or not isinstance(rec.get("param"), dict):
        raise SystemExit(f"{node_name}: 不是 v2 recognition 结构，无法重写 expected")
    rec["param"]["expected"] = expected


def drop_extra_s_nodes(data: dict, s_count: int) -> list[str]:
    removed = []
    for name in list(data):
        m = S_NODE_RE.match(name)
        if not m:
            continue
        index = int(m.group(2))
        if index > s_count:
            removed.append(name)
            del data[name]
    return removed


def drop_refs(data: dict, removed: set[str]) -> None:
    if not removed:
        return
    for node in data.values():
        if not isinstance(node, dict):
            continue
        for key in ("next", "on_error"):
            refs = node.get(key)
            if not isinstance(refs, list):
                continue
            node[key] = [ref for ref in refs if not (isinstance(ref, str) and ref in removed)]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    event = args.event.strip()
    if not event or any(part in event for part in ("/", "\\", "..")):
        raise SystemExit(f"--event 不合法：{args.event!r}")

    prefix = args.prefix.strip().rstrip("-")
    from_prefix = args.from_prefix.strip().rstrip("-")
    if not prefix or not re.fullmatch(r"[A-Za-z0-9_]+", prefix):
        raise SystemExit(f"--prefix 只允许字母/数字/下划线（不带末尾连字符）：{args.prefix!r}")
    if args.s_count < 1 or args.s_count > 20:
        raise SystemExit("--s-count 必须在 1..20 之间")

    source_path = EVENTS_DIR / args.from_event / "pipeline" / "event_override.json"
    out_dir = EVENTS_DIR / event / "pipeline"
    out_path = out_dir / "event_override.json"

    data = replace_strings(copy.deepcopy(load_json(source_path)), from_prefix, prefix, args.event_name)

    # 先按 s_count 裁掉多余的五组 S 关节点，再重写所有 expected。
    removed = set(drop_extra_s_nodes(data, args.s_count))
    drop_refs(data, removed)

    codes = map_codes(prefix, args.s_count)
    set_expected(data, "NavArrivedEX", [f"{prefix}-EX-1"])
    set_expected(data, "NavArrivedEX2", [f"{prefix}-EX-{EX_ANCHOR}"])
    set_expected(data, "NavArrivedMain", [f"{prefix}-1"])
    set_expected(data, "NavArrivedMain2", [f"{prefix}-{MAIN_ANCHOR}"])
    for name in ("NavMapAnyBack", "NavMapStop") + tuple(f"NavMapThenS{i}" for i in range(1, args.s_count + 1)):
        set_expected(data, name, list(codes))
    for i in range(1, args.s_count + 1):
        set_expected(data, f"NavStageS{i}", [f"{prefix}-S-{i}"])
        set_expected(data, f"NavDetailCodeS{i}", [f"{prefix}-S-{i}"])

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")

    print(f"[ok] {out_path.relative_to(HERE)}")
    print(f"     event={event} prefix={prefix}- s_count={args.s_count} nodes={len(data)}")
    if removed:
        print(f"     裁掉 S 节点：{', '.join(sorted(removed))}")
    if args.s_count != 5:
        print(
            "[warn] interface.json 仍有 5 个「进入S-N详情」任务入口；"
            f"s_count={args.s_count} 时需同步删减，否则 precheck 会报 entry 缺失。",
            file=sys.stderr,
        )
    print("下一步：")
    print(f"  pwsh -File pi/arknights-event/setup_pi.ps1 -Event {event}")
    print("  python pi/arknights-event/tools/precheck.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
