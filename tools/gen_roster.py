# -*- coding: utf-8 -*-
"""gen_roster.py —— 从机端 state.json + accounts.json 生成班表 runner/roster.json。

两种排法（见 internal/runner/roster.go）：
  chain（默认，连续班）：所有账号按顺序均匀铺在一个 cycleHours 轮盘上，
      间隔 = cycleHours*60/n 分钟（52 台/16h ≈ 18.5 分钟一位），
      合起来就是一条"每 ~18 分钟起一台、永不空转"的连续流水线。
  shifts：几班倒（每班一个锚点时刻，班内多台一起上）。

用法（机端）：
  python gen_roster.py                          # 连续班：全部启用日常号，间隔自动算
  python gen_roster.py --base 00:15 --reseed    # 起点 00:15 + 立即对齐（清掉旧排班）
  python gen_roster.py --mode shifts --anchors 04:00,12:00,20:00
  python gen_roster.py --dry                    # 只看排位，不写文件

只读 state.json / accounts.json，不改任何运行状态。
"""
import argparse
import io
import json
import os
import re
import sys
import datetime as dt
from collections import OrderedDict

FMT = "%Y-%m-%d %H:%M:%S"


def load(path):
    with io.open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def natkey(s):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)]


def main():
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=os.path.join(root, "runner", "state.json"))
    ap.add_argument("--accounts", default=os.path.join(root, "runner", "accounts.json"))
    ap.add_argument("--out", default=os.path.join(root, "runner", "roster.json"))
    ap.add_argument("--mode", choices=["chain", "shifts"], default="chain")
    ap.add_argument("--base", default="", help="连续模式起点 HH:MM（默认下一个整 5 分钟）")
    ap.add_argument("--anchors", default="00:00,08:00,16:00", help="shifts 模式的班次锚点")
    ap.add_argument("--cycle", type=int, default=16, help="每号多少小时一轮（默认 16）")
    ap.add_argument("--min-gap", type=int, default=12, help="两次之间最少间隔小时（默认 12）")
    ap.add_argument("--reseed", action="store_true", help="立即对齐（清掉旧排班；否则下次跑完才对齐）")
    ap.add_argument("--exclude", default="a37", help="不排班的号（逗号分隔；默认 a37 已停用）")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    st, acc = load(a.state), load(a.accounts)
    tasks = st.get("tasks", {})
    excl = {x.strip() for x in a.exclude.split(",") if x.strip()}

    rows = []
    for ac in acc.get("accounts", []):
        if not ac.get("enabled", True) or not ac.get("daily"):
            continue
        if ac["id"] in excl:
            continue
        t = tasks.get("daily:" + ac["id"], {})
        try:
            due = dt.datetime.strptime(t.get("nextDueAt", ""), FMT)
        except Exception:
            due = None
        rows.append({"id": ac["id"], "emu": str(ac.get("emu", "")), "client": ac.get("client", ""),
                     "due": due, "state": t.get("state")})
    rows.sort(key=lambda r: natkey(r["id"]))

    base = a.base
    if not base:
        now = dt.datetime.now()
        nxt = (now + dt.timedelta(minutes=5)).replace(second=0, microsecond=0)
        nxt = nxt.replace(minute=(nxt.minute // 5) * 5)
        base = nxt.strftime("%H:%M")

    out = OrderedDict()
    out["enabled"] = True
    out["cycleHours"] = a.cycle
    out["minGapHours"] = a.min_gap
    if a.reseed:
        out["reseed"] = True
    out["note"] = "由 tools/gen_roster.py 生成（机端）：%s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    if a.mode == "chain":
        out["base"] = base
        out["order"] = [r["id"] for r in rows]
        step = a.cycle * 60.0 / max(len(rows), 1)
        out["note"] += "；连续班：%d 台，间隔 %.1f 分钟" % (len(rows), step)
    else:
        anchors = [x.strip() for x in a.anchors.split(",") if x.strip()]
        names = ["零点班", "八点班", "十六点班", "凌晨班", "中午班", "晚间班"][: len(anchors)]
        groups = [[] for _ in anchors]
        for i, r in enumerate(rows):
            cand = sorted(range(len(anchors)), key=lambda k: (len(groups[k]), k))
            for k in cand:
                other = {"a13": "a51", "a51": "a13"}.get(r["id"])
                if other and any(x["id"] == other for x in groups[k]):
                    continue
                groups[k].append(r)
                break
            else:
                groups[cand[0]].append(r)
        out["shifts"] = [OrderedDict([("name", names[k]), ("at", anchors[k]),
                                      ("accounts", [x["id"] for x in groups[k]])])
                         for k in range(len(anchors))]

    if a.dry:
        h, m = [int(x) for x in base.split(":")]
        if a.mode == "chain":
            step = a.cycle * 60.0 / max(len(rows), 1)
            for i, r in enumerate(rows):
                tot = (h * 60 + m + round(i * step)) % 1440
                print("  %s(%d)  %-4s emu=%-3s %s" % ("%02d:%02d" % (tot // 60, tot % 60), i,
                                                      r["id"], r["emu"], r["due"].strftime("%m-%d %H:%M") if r["due"] else "—"))
        else:
            for s in out["shifts"]:
                print("  %s %s：%s" % (s["name"], s["at"], ",".join(s["accounts"])))
        return 0

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    print("已写 %s" % a.out)
    if a.mode == "chain":
        print("  连续班：%d 台，%s 起，每 %.1f 分钟一位，%dh 一轮" % (
            len(rows), base, a.cycle * 60.0 / max(len(rows), 1), a.cycle))
    else:
        for s in out["shifts"]:
            print("  %-6s %s  %d 台" % (s["name"], s["at"], len(s["accounts"])))
    print("  排除：%s｜reseed=%s" % (",".join(sorted(excl)) or "无", bool(a.reseed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
