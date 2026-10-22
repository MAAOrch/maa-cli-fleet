# -*- coding: utf-8 -*-
"""给某个 MAA 槽位的 resource/tasks/tasks.json 打补丁：
把指定任务名加进 ScreenshotTaskPlugin-Config.next（= 该任务开始时自动截图）。
用法:
  python patch_screenshot_plugin.py <tasks.json 路径> [要加的任务名...]
  python patch_screenshot_plugin.py <tasks.json 路径> --remove [要移除的任务名...]
ASCII-only output so PowerShell/console encoding never breaks it.
每次写入前自动留 tasks.json.bak-pi-<时间戳> 备份。
"""
import json
import sys
import shutil
import datetime
import os

if len(sys.argv) < 3:
    print("usage: patch_screenshot_plugin.py <tasks.json> <task...> | --remove [task...]")
    raise SystemExit(2)

path = sys.argv[1]
rest = sys.argv[2:]
remove = False
if rest and rest[0] == '--remove':
    remove = True
    rest = rest[1:]
add = rest

if not os.path.exists(path):
    print("NOT FOUND: " + path)
    raise SystemExit(3)

bak = path + ".bak-pi-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy2(path, bak)
print("backup: " + os.path.basename(bak))

with open(path, encoding="utf-8") as f:
    data = json.load(f)

key = "ScreenshotTaskPlugin-Config"
if key not in data:
    print("no such task: " + key)
    raise SystemExit(4)

nxt = data[key].setdefault("next", [])
before = list(nxt)
if remove:
    targets = add or ["Award"]
    for t in targets:
        while t in nxt:
            nxt.remove(t)
    print("mode      : remove")
else:
    print("mode      : add")
    for t in add:
        if t not in nxt:
            nxt.append(t)
print("next before: " + json.dumps(before, ensure_ascii=False))
print("next after : " + json.dumps(nxt, ensure_ascii=False))

with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=4)

# read back
with open(path, encoding="utf-8") as f:
    back = json.load(f)
print("verify     : " + json.dumps(back[key].get("next"), ensure_ascii=False))
print("tasks total: %d" % len(back))
