#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重排后队列定时（3 并发波次）与启用。

默认 dry-run；--apply 写 MAS；有 --enable 才打开 TimeEnabled。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mas_inventory import safe_print  # noqa: E402

API = os.environ.get('MAS_API', 'http://127.0.0.1:36163')
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
SCHEDULE = {
    '并行1': ['05:00', '19:00'],
    '并行2': ['05:10', '19:10'],
    '并行3': ['05:20', '19:20'],
    '并行4': ['08:00', '21:30'],
    '并行5': ['08:10', '21:40'],
    '并行6': ['08:20', '21:50'],
    '并行7': ['10:30', '00:15'],
}


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
    ap = argparse.ArgumentParser(description='MAS 队列定时波次（默认 dry-run）')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--enable', action='store_true', help='同时打开队列 TimeEnabled')
    args = ap.parse_args()

    qs = ok('/api/queue/get', {}).get('data') or {}
    for qid, q in qs.items():
        if qid == 'instances' or not isinstance(q, dict):
            continue
        name = str((q.get('Info') or {}).get('Name') or '')
        if not name.startswith('并行'):
            continue
        key = name.split('-')[0]
        times = SCHEDULE.get(key)
        if not times:
            continue
        sets = ok('/api/queue/time/get', {'queueId': qid, 'timeSetId': None}).get('data') or {}
        ids = [k for k in sets if k != 'instances' and isinstance(sets[k], dict)]
        safe_print('%s timeEnabled=%s sets=%d -> %s' % (
            name, (q.get('Info') or {}).get('TimeEnabled'), len(ids), times))
        if not args.apply:
            continue
        # 保证每个队列有两组时间
        while len(ids) < 2:
            r = ok('/api/queue/time/add', {'queueId': qid})
            ids.append(str(r.get('timeSetId') or ''))
        for i, tid in enumerate(ids[:2]):
            data = {'Info': {'Enabled': True, 'Days': DAYS, 'Time': times[i]}}
            ok('/api/queue/time/update', {'queueId': qid, 'timeSetId': tid, 'data': data})
            safe_print('  set %s' % times[i])
        if args.enable:
            q['Info']['TimeEnabled'] = True
            ok('/api/queue/update', {'queueId': qid, 'data': q})
            safe_print('  enabled')
    if not args.apply:
        safe_print('dry-run：未写 MAS。加 --apply 才执行。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
