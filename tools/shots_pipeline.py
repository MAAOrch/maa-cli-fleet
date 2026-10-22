# -*- coding: utf-8 -*-
"""shots_pipeline.py —— 大厅截图 → 二次处理 → 归档（回填 / 常驻看门）。

锚定原则（2026-10-03 踩过坑后定的）：
  · **账号**以 MAS 的 history 为准：`E:\\AUTO-MAS\\history\\<日期>\\<账号名>\\<脚本>-<时间>.log`
    该文件 mtime ≈ 这一轮结束时刻，而截图发生在「领取奖励」（结束前 1~2 分钟）→ 用 mtime 锚定。
  · **槽位约束**：账号所属脚本的 MAA 槽位必须等于截图所在槽位，否则不认（防串台/串号）。
  · 所有配置来自**在线** MAS 接口（`/api/scripts/get` + `/api/scripts/user/get`），不读本地旧快照。

用法：
  python shots_pipeline.py --backfill      # 把已有 raw 图补齐成品 + 清黑帧
  python shots_pipeline.py --watch --minutes 480
"""
import argparse
import glob
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

API = os.environ.get('MAS_API', 'http://127.0.0.1:36163')
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
BASE = os.environ.get('MAS_BASE', os.path.join(os.path.dirname(ROOT), 'MAA-Multi'))
HISTORY = os.environ.get('MAS_HISTORY', os.path.join(os.path.dirname(ROOT), 'AUTO-MAS', 'history'))
PY = os.environ.get('PYTHON', sys.executable)
ANNO = os.path.join(ROOT, 'tools', 'shot_annotate.py')
OUT_DIR = os.environ.get('SHOTS_OUT', os.path.join(ROOT, 'shots'))
DESK = os.environ.get('SHOTS_DESK', os.path.join(os.path.expanduser('~'), 'Desktop', 'maa-shots'))
NAMES = {'StartUp': '开始唤醒', 'Fight': '刷理智', 'Infrast': '基建换班',
         'Recruit': '公开招募', 'Mall': '信用购物', 'Award': '领取奖励'}
JUNK_BYTES = 600 * 1024          # 成品小于这个 → 黑帧版
BLACK_RAW = 900 * 1024           # raw 小于这个 → 转场黑帧（大厅帧一般 ≥1.4MB）

BY_NAME = {}     # 账号名 -> dict(phone, script, emu, slot, stage, stage_rem, ann)
ROUNDS = []      # [(mtime, 账号名, 文件名)]
SLOTS = {}       # 槽位 -> 脚本名（仅用于日志）


def _p(*a):
    try:
        print(*a)
    except UnicodeEncodeError:
        print(' '.join(str(x).encode('ascii', 'replace').decode('ascii') for x in a))


def post(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=60))


def load_config():
    """在线拉脚本 + 用户 → BY_NAME（账号名是唯一可靠键：B服 ID 是用户名、台服是邮箱）。"""
    global BY_NAME, SLOTS
    BY_NAME, SLOTS = {}, {}
    for sid, v in (post('/api/scripts/get', {}).get('data') or {}).items():
        sname = v['Info']['Name']
        emu = v['Emulator']['Index']
        slot = (v['Info'].get('Path') or '').replace('/', '\\').split('\\')[-1]
        SLOTS.setdefault(slot, []).append(sname)
        us = post('/api/scripts/user/get', {'scriptId': sid, 'userId': None}).get('data') or {}
        for uid, u in us.items():
            if uid == 'instances':
                continue
            i = (u or {}).get('Info') or {}
            nm = i.get('Name')
            if not nm:
                continue
            BY_NAME[nm] = dict(phone=i.get('Id') or '', script=sname, emu=emu, slot=slot,
                               stage=i.get('Stage') or '', stage_rem=i.get('Stage_Remain') or '',
                               ann=i.get('Annihilation') or '', status=bool(i.get('Status')))
    _p('config: %d accounts / %d slots' % (len(BY_NAME), len(SLOTS)))


def load_rounds():
    """扫 MAS history：一轮一个 log 文件，父目录名 = 账号名。"""
    global ROUNDS
    out = []
    days = [time.strftime('%Y-%m-%d'), time.strftime('%Y-%m-%d', time.localtime(time.time() - 86400))]
    for day in days:
        d = os.path.join(HISTORY, day)
        if not os.path.isdir(d):
            continue
        for acct in os.listdir(d):
            ap = os.path.join(d, acct)
            if not os.path.isdir(ap):
                continue
            for f in glob.glob(os.path.join(ap, '*.log')):
                try:
                    out.append((os.path.getmtime(f), acct, os.path.basename(f)))
                except OSError:
                    pass
    out.sort()
    ROUNDS = out
    _p('history rounds: %d' % len(out))
    return out


def find_account(shot_slot, epoch):
    """给截图找账号：history 轮次 mtime 在 [t-2min, t+15min] 且**槽位一致**。"""
    best = None
    for mt, acct, _f in ROUNDS:
        if not (epoch - 120 <= mt <= epoch + 900):
            continue
        rec = BY_NAME.get(acct)
        if not rec:
            continue
        if rec['slot'] != shot_slot:          # 槽位不符 → 不认（防串台）
            continue
        best = acct                            # 取窗口内最后一个
    if best:
        return best
    # 放宽到 30 分钟，同样要求槽位一致
    for mt, acct, _f in ROUNDS:
        if not (epoch - 300 <= mt <= epoch + 1800):
            continue
        rec = BY_NAME.get(acct)
        if rec and rec['slot'] == shot_slot:
            best = acct
    return best


def tasks_of(slot, cutoff):
    """截图时刻之前的那一轮任务段（LIFO 配对 + 只取最后一个 StartUp）。"""
    p = os.path.join(BASE, slot, 'debug', 'gui.log')
    if not os.path.exists(p):
        return []
    txt = io.open(p, encoding='utf-8', errors='replace').read()
    rows = re.findall(r'\[(\d{4}-\d\d-\d\d) ([\d:]{8})[^\]]*\][^\]]*\][^\]]*\]\s*<\d+>\s*'
                      r'(?:Start Task Chain: (\w+)|Completed Task Chain: (\w+))', txt)
    ot, seq = {}, []
    for day, ts, s, d in rows:
        try:
            t = time.mktime(time.strptime('%s %s' % (day, ts), '%Y-%m-%d %H:%M:%S'))
        except Exception:
            continue
        if t > cutoff + 120:
            break
        if s:
            ot.setdefault(s, []).append((t, ts))
        elif d:
            q = ot.get(d) or []
            if q:
                _st, sts = q.pop()
                seq.append((d, sts, ts))
    starts = [i for i, x in enumerate(seq) if x[0] == 'StartUp']
    seq = seq[starts[-1]:] if starts else seq[-7:]

    def sec(x):
        h, mm, ss = x.split(':')
        return int(h) * 3600 + int(mm) * 60 + int(ss)
    out = []
    for nm, s, e in seq:
        d = sec(e) - sec(s)
        if d < 0:
            d += 86400
        out.append({'name': NAMES.get(nm, nm), 'start': s, 'end': e,
                    'dur': '%dm%02ds' % (d // 60, d % 60), 'status': 'Completed'})
    return out


def groups(slot):
    """按时间聚类 raw 图（相邻≤120s 同组），每组取最大那张；整组都是黑帧则跳过。"""
    d = os.path.join(BASE, slot, 'debug', 'Award')
    if not os.path.isdir(d):
        return []
    files = sorted((os.path.getmtime(p), p) for p in glob.glob(os.path.join(d, '*.png')))
    out, cur = [], []
    for t, p in files:
        if cur and t - cur[-1][0] > 120:
            out.append(cur)
            cur = []
        cur.append((t, p))
    if cur:
        out.append(cur)
    res = []
    for g in out:
        best = max(g, key=lambda x: os.path.getsize(x[1]))
        if os.path.getsize(best[1]) < BLACK_RAW:
            continue
        res.append((best[0], best[1]))
    return res


def annotate(slot, epoch, raw):
    acct = find_account(slot, epoch)
    if not acct:
        _p('SKIP %s %s：history 里找不到匹配的轮次（槽位 %s）' % (
            slot, time.strftime('%H:%M:%S', time.localtime(epoch)), slot))
        return
    rec = BY_NAME[acct]
    tasks = tasks_of(slot, epoch)
    info = {'ts': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(epoch)),
            'account': acct, 'phone': rec['phone'], 'server': _server_of(rec),
            'emu': rec['emu'], 'source': 'AUTO-MAS %s（emu %s / %s）' % (rec['script'], rec['emu'], slot),
            'tasks': tasks, 'stage': rec['stage'], 'stage_remain': rec['stage_rem'], 'ann': rec['ann']}
    tag = re.sub(r'[\\/:*?"<>|]', '_', '%s_%s_%s' % (rec['script'], acct,
                                                     time.strftime('%Y%m%d-%H%M%S', time.localtime(epoch))))
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(DESK, exist_ok=True)
    tmp = os.path.join(OUT_DIR, '_info.json')
    io.open(tmp, 'w', encoding='utf-8').write(json.dumps(info, ensure_ascii=False, indent=1))
    out = os.path.join(OUT_DIR, tag + '.png')
    r = subprocess.run([PY, ANNO, '--shot', raw, '--json', tmp, '--out', out],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < JUNK_BYTES:
        _p('SKIP %s：成品不合格（rc=%s）—— 保留旧图' % (tag, r.returncode))
        return
    prefix = tag.rsplit('_', 1)[0]
    for old in glob.glob(os.path.join(OUT_DIR, prefix + '-*.png')):
        if os.path.basename(old) == tag + '.png':
            continue
        for d in (OUT_DIR, DESK):
            f = os.path.join(d, os.path.basename(old))
            if os.path.exists(f):
                os.remove(f)
    subprocess.run(['cmd', '/c', 'copy', '/Y', out, os.path.join(DESK, os.path.basename(out))],
                   capture_output=True, text=True)
    _p('OK  %-34s %s  tasks=%d  %dKB' % (tag, rec['phone'][-4:] if rec['phone'] else '-',
                                         len(tasks), os.path.getsize(out) // 1024))


def _server_of(rec):
    s = rec['script']
    if s.startswith('明日方舟-日') or s.startswith('l-'):
        return 'YoStarJP'
    if s.startswith('明日方舟-台'):
        return 'txwy'
    if s.startswith('明日方舟-b') or s.endswith('-b-11'):
        return 'Bilibili'
    return 'Official'


def sweep_junk():
    n = 0
    for d in (OUT_DIR, DESK):
        for p in glob.glob(os.path.join(d, '*.png')):
            if os.path.getsize(p) < JUNK_BYTES and not os.path.basename(p).startswith('_'):
                os.remove(p)
                n += 1
    if n:
        _p('清理黑帧成品 %d 个' % n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--backfill', action='store_true')
    ap.add_argument('--watch', action='store_true')
    ap.add_argument('--minutes', type=int, default=480)
    a = ap.parse_args()
    load_config()
    load_rounds()
    if a.backfill:
        slots = [s for s in SLOTS if os.path.isdir(os.path.join(BASE, s, 'debug', 'Award'))]
        for slot in sorted(slots):
            for epoch, raw in groups(slot):
                annotate(slot, epoch, raw)
        sweep_junk()
        return 0
    if a.watch:
        seen = set()
        end = time.time() + a.minutes * 60
        while time.time() < end:
            load_rounds()                       # 每轮刷新 history（有新轮次才能标对号）
            for slot in sorted(SLOTS):
                for epoch, raw in groups(slot):
                    key = (slot, os.path.basename(raw))
                    if key in seen or time.time() - epoch < 5:
                        continue
                    seen.add(key)
                    annotate(slot, epoch, raw)
            sweep_junk()
            time.sleep(30)
        return 0
    _p('用法：--backfill | --watch [--minutes N]')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
