#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监视单个 MAS dispatch 任务，任务从 runtime-snapshot 消失即退出。"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.request

API = 'http://127.0.0.1:36163'


def snapshot() -> dict:
    with urllib.request.urlopen(API + '/api/dispatch/runtime-snapshot', timeout=30) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--task-id', required=True)
    ap.add_argument('--timeout-min', type=int, default=180)
    ap.add_argument('--interval', type=int, default=60)
    args = ap.parse_args()
    deadline = time.time() + args.timeout_min * 60
    while time.time() < deadline:
        stamp = dt.datetime.now().strftime('%H:%M:%S')
        try:
            tasks = snapshot().get('tasks') or []
        except Exception as e:  # noqa: BLE001
            print('%s API_ERROR %s' % (stamp, e), flush=True)
            time.sleep(args.interval)
            continue
        target = next((t for t in tasks if t.get('taskId') == args.task_id), None)
        if target is None:
            print('%s TASK_DONE %s' % (stamp, args.task_id), flush=True)
            return 0
        rows = []
        for s in target.get('task_info') or []:
            users = ','.join('%s:%s' % (u.get('name'), u.get('status'))
                             for u in (s.get('userList') or []))
            rows.append('%s[%s]%s' % (s.get('name'), s.get('status'), users))
        print('%s %s' % (stamp, ' | '.join(rows)), flush=True)
        time.sleep(args.interval)
    print('WATCH_TIMEOUT', flush=True)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
