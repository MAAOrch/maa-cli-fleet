#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把重排后的单账号脚本切到 independent，并物化独立 ConfigFile。

顺序（每个脚本）：
  1. data/<scriptId>/Default/ConfigFile -> data/<scriptId>/<userId>/ConfigFile
     （只复制非 .bak 文件，已有独立目录则不动）
  2. 用户 Info.Base = independent
默认 dry-run；--apply 才写 MAS。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mas_inventory import safe_print  # noqa: E402

API = os.environ.get('MAS_API', 'http://127.0.0.1:36163')
MAS_ROOT = r'E:\AUTO-MAS'
PLAN = r'E:\maa-cli-fleet\dist\reorch_plan.json'


def post(path: str, body: dict, retries: int = 4) -> dict:
    last = None
    for i in range(retries):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1 + i)
    raise RuntimeError('%s: %s' % (path, last))


def ok(path: str, body: dict) -> dict:
    r = post(path, body)
    if r.get('code') not in (200, None) or r.get('status') == 'error':
        raise RuntimeError('%s -> %s' % (path, json.dumps(r, ensure_ascii=False)))
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description='切换正式脚本到 independent（默认 dry-run）')
    ap.add_argument('--plan', default=PLAN)
    ap.add_argument('--only', default='', help='只处理一个脚本名')
    ap.add_argument('--exclude-queue', default='', help='跳过某个队列里的脚本')
    ap.add_argument('--skip-active', action='store_true', help='跳过当前正在运行的脚本')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    plan = json.load(io.open(args.plan, encoding='utf-8'))
    targets = [c for c in plan['canonicals'] if c['base'] != 'independent']
    if args.only:
        targets = [c for c in targets if c['name'] == args.only]
    if args.exclude_queue:
        skip = {it['scriptId'] for q in plan['queues']
                if q['queueName'].startswith(args.exclude_queue) for it in q['items']}
        targets = [c for c in targets if c['scriptId'] not in skip]
        safe_print('skip queue %s: %d scripts' % (args.exclude_queue, len(skip)))
    if args.skip_active:
        snap = json.load(urllib.request.urlopen(API + '/api/dispatch/runtime-snapshot', timeout=60))
        active = set()
        for t in snap.get('tasks') or []:
            for s in t.get('task_info') or []:
                if s.get('status') == '运行' and s.get('script_id'):
                    active.add(str(s['script_id']))
        targets = [c for c in targets if c['scriptId'] not in active]
        safe_print('skip active: %d scripts' % len(active))
    safe_print('BASE PLAN  targets=%d' % len(targets))

    for c in targets:
        sid, uid = c['scriptId'], c['userId']
        try:
            users = ok('/api/scripts/user/get', {'scriptId': sid, 'userId': None}).get('data') or {}
            current = str(((users.get(uid) or {}).get('Info') or {}).get('Base') or '')
        except Exception as e:  # noqa: BLE001
            safe_print('  %-28s API ERROR %s' % (c['name'], e))
            return 2
        if current == 'independent':
            continue
        src = os.path.join(MAS_ROOT, 'data', sid, 'Default', 'ConfigFile')
        dst = os.path.join(MAS_ROOT, 'data', sid, uid, 'ConfigFile')
        need_copy = os.path.isdir(src) and not os.path.isdir(dst)
        safe_print('  %-28s %-24s %s -> independent  copy=%s' % (
            c['name'], c['userName'], current or '?', need_copy))
        if not args.apply:
            continue
        try:
            if need_copy:
                os.makedirs(dst, exist_ok=True)
                shutil.copytree(src, dst, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns('*.bak', '*.bak_*', '*.bak-*'))
                safe_print('    copied ConfigFile')
            users = ok('/api/scripts/user/get', {'scriptId': sid, 'userId': None}).get('data') or {}
            u = users.get(uid)
            if not isinstance(u, dict):
                raise RuntimeError('user missing')
            u.setdefault('Info', {})['Base'] = 'independent'
            ok('/api/scripts/user/update', {'scriptId': sid, 'userId': uid, 'data': u})
            safe_print('    base=independent')
        except Exception as e:  # noqa: BLE001
            safe_print('    ERROR %s' % e)
            return 2

    if not args.apply:
        safe_print('dry-run：未写 MAS。加 --apply 才执行。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
