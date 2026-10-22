# -*- coding: utf-8 -*-
"""切号对账（ASCII 输出）：配置里的账号顺序 vs 实际切号日志 vs 大厅图时刻。"""
import glob
import io
import json
import os
import re
import time
import urllib.request

API = os.environ.get('MAS_API', 'http://127.0.0.1:36163')
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
BASE = os.environ.get('MAS_BASE', os.path.join(os.path.dirname(ROOT), 'MAA-Multi'))
SLOTS = {'MAA_6': '官-1', 'MAA_2': '官-2', 'MAA_10': '官-3', 'MAA_1': '官-4'}


def A(x):
    return str(x).encode('ascii', 'replace').decode('ascii')


def post(p, b):
    r = urllib.request.Request(API + p, data=json.dumps(b).encode(), headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(r, timeout=60))


scripts = post('/api/scripts/get', {}).get('data') or {}
sid_of = {v['Info']['Name']: k for k, v in scripts.items()}


def mask_of(phone):
    return phone[:3] + '****' + phone[-4:] if len(phone or '') == 11 else ''


def users_in_order(script):
    us = post('/api/scripts/user/get', {'scriptId': sid_of[script], 'userId': None}).get('data') or {}
    order = [x['uid'] for x in (us.get('instances') or [])] or [k for k in us if k != 'instances']
    out = []
    for uid in order:
        i = (us.get(uid) or {}).get('Info') or {}
        out.append((i.get('Name'), mask_of(i.get('Id') or ''), bool(i.get('Status'))))
    return out


def switches(slot):
    p = os.path.join(BASE, slot, 'debug', 'gui.log')
    out = []
    if os.path.exists(p):
        for ln in io.open(p, encoding='utf-8', errors='replace'):
            m = re.search(r'\[\d{4}-\d{2}-\d{2} (\d\d:\d\d:\d\d)[^\]]*\].*?-->> (\d{3}\*{4}\d{4})', ln)
            if m:
                out.append((m.group(1), m.group(2)))
    return out


def awards(slot):
    d = os.path.join(BASE, slot, 'debug', 'Award')
    if not os.path.isdir(d):
        return []
    files = sorted((os.path.getmtime(p), p) for p in glob.glob(os.path.join(d, '*.png')))
    out, cur = [], []
    for t, p in files:
        if cur and t - cur[-1][0] > 120:
            out.append(max(cur, key=lambda x: os.path.getsize(x[1])))
            cur = []
        cur.append((t, p))
    if cur:
        out.append(max(cur, key=lambda x: os.path.getsize(x[1])))
    return [time.strftime('%H:%M:%S', time.localtime(t)) for t, _ in out]


for slot, script in SLOTS.items():
    users = users_in_order(script)
    enabled = [u for u in users if u[2]]
    disabled = [n for n, _, e in users if not e]
    print('=== %s (%s)' % (slot, A(script)))
    print('   config order : ' + ' -> '.join('%s[%s]' % (A(n), m) for n, m, _ in enabled))
    if disabled:
        print('   disabled     : ' + ','.join(A(d) for d in disabled))
    sw = switches(slot)
    aw = awards(slot)
    print('   real switches: ' + (' '.join('%s->%s' % x for x in sw) or '(none)'))
    print('   lobby shots  : ' + (' '.join(aw) or '(none)'))
    exp = [m for _, m, _ in enabled]
    got = [m for _, m in sw]
    miss = [m for m in exp if m not in got]
    extra = [m for m in got if m not in exp]
    print('   >> %d enabled accounts / %d switches' % (len(exp), len(got)))
    if miss:
        print('   >> NEVER SWITCHED TO (suspect): ' + ','.join(miss))
    if extra:
        print('   >> SWITCHED TO SOMETHING NOT IN LIST: ' + ','.join(extra))
    print()
