#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MAS 全量脚本去重 + 队列重排（Plan / Apply）。

规则：
  * 每个启用账号（Id 优先，否则 Name+Server）只保留一个正式脚本；
  * 旧共享脚本、补跑重复脚本、无启用用户的脚本统一改名 归档-*；
  * 归档脚本里的启用用户改为 Status=false，配置保留可回滚；
  * 正式队列 并行1..7 重建，队列顺序优先保留原顺序；
  * 并行3/4 按队列项单双分配 MAA_3/MAA_8、MAA_4/MAA_9；
  * 默认只生成 Plan，不写 MAS；--apply 才落地，且队列保持暂停。

用法：
  python tools/mas_reorch_apply.py --config-dir C:\\path\\to\\config
  python tools/mas_reorch_apply.py --apply --mas-api http://127.0.0.1:36163
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import io
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mas_inventory import safe_print  # noqa: E402

BASE = r'E:\AUTO-MAS'
POOLS = {
    '并行1': [r'E:\MAA-Multi\MAA_1'],
    '并行2': [r'E:\MAA-Multi\MAA_2'],
    '并行3': [r'E:\MAA-Multi\MAA_3', r'E:\MAA-Multi\MAA_8'],
    '并行4': [r'E:\MAA-Multi\MAA_4', r'E:\MAA-Multi\MAA_9'],
    '并行5': [r'E:\MAA-Multi\MAA_5'],
    '并行6': [r'E:\MAA-Multi\MAA_6'],
    '并行7': [r'E:\MAA-Multi\MAA_7'],
}
OLD_SHARED_NAMES = ('官-1', '官-2', '官-3', '官-4')


def load_json(path: str) -> dict:
    with io.open(path, 'r', encoding='utf-8-sig') as f:
        return json.load(f)


def api_post(api: str, path: str, body: dict, retries: int = 3) -> dict:
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            api.rstrip('/') + path,
            data=json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 + attempt)
    raise RuntimeError('MAS API %s 失败：%s' % (path, last))


def api_ok(api: str, path: str, body: dict) -> dict:
    resp = api_post(api, path, body)
    if resp.get('code') not in (200, None) or resp.get('status') == 'error':
        raise RuntimeError('%s 返回异常：%s' % (path, json.dumps(resp, ensure_ascii=False)))
    return resp


def active(user: dict) -> bool:
    info = user.get('Info') or {}
    return bool(info.get('Status')) and info.get('RemainedDay') != 0


def ident_key(user: dict) -> str:
    uid = str(user.get('id') or '').strip()
    if uid:
        return 'id:' + uid
    return 'name:' + str(user.get('name') or '').strip() + '|' + str(user.get('server') or '')


def server_label(server: str) -> str:
    return {
        'Official': '官服',
        'Bilibili': 'B服',
        'YoStarJP': '日服',
        'YoStarEN': '国际服',
        'YoStarKR': '韩服',
        'txwy': '腾讯',
    }.get(server, server or '未知')


def build_plan(config_dir: str) -> dict:
    sc = load_json(os.path.join(config_dir, 'ScriptConfig.json'))
    qc = load_json(os.path.join(config_dir, 'QueueConfig.json'))

    scripts = {}
    for sid, s in sc.items():
        if sid == 'instances' or not isinstance(s, dict):
            continue
        info = s.get('Info') or {}
        users = {}
        for uid, u in ((s.get('SubConfigsInfo') or {}).get('UserData') or {}).items():
            if uid == 'instances' or not isinstance(u, dict):
                continue
            ui = u.get('Info') or {}
            users[uid] = {
                'uid': uid,
                'name': str(ui.get('Name') or '').strip(),
                'id': str(ui.get('Id') or '').strip(),
                'server': str(ui.get('Server') or ''),
                'status': bool(ui.get('Status')),
                'remained': ui.get('RemainedDay'),
                'base': str(ui.get('Base') or ''),
                'active': active(u),
            }
        scripts[sid] = {
            'sid': sid,
            'name': str(info.get('Name') or '').strip(),
            'maa': str(info.get('Path') or ''),
            'emu': str((s.get('Emulator') or {}).get('Index') or ''),
            'users': users,
            'queues': [],
        }

    queues = {}
    for qid, q in qc.items():
        if qid == 'instances' or not isinstance(q, dict):
            continue
        qi = q.get('Info') or {}
        items = []
        schedule = None
        for iid, it in ((q.get('SubConfigsInfo') or {}).get('QueueItem') or {}).items():
            if iid == 'instances' or not isinstance(it, dict):
                continue
            sid = str((it.get('Info') or {}).get('ScriptId') or '')
            items.append((iid, sid))
            if schedule is None and isinstance(it.get('Schedule'), dict):
                schedule = it['Schedule']
            if sid in scripts:
                scripts[sid]['queues'].append(str(qi.get('Name') or ''))
        queues[qid] = {
            'qid': qid,
            'name': str(qi.get('Name') or ''),
            'enabled': bool(qi.get('TimeEnabled')),
            'items': items,
            'schedule': schedule,
        }

    official = {qid: q for qid, q in queues.items() if q['name'].startswith('并行')}
    official_names = {q['name'] for q in official.values()}

    # 账号身份 -> 候选脚本/用户
    identities = collections.defaultdict(list)
    for s in scripts.values():
        for u in s['users'].values():
            if u['active']:
                identities[ident_key(u)].append((s, u))

    def score(item):
        s, u = item
        active_n = sum(1 for x in s['users'].values() if x['active'])
        return (
            0 if active_n == 1 else 1,
            0 if s['name'] in OLD_SHARED_NAMES else 1,
            0 if '补跑' not in s['name'] else 1,
            0 if s['name'] in official_names else 1,
            s['name'],
        )

    canonicals = []
    canonical_sids = set()
    warnings = []
    for key, items in identities.items():
        items = sorted(items, key=score)
        s, u = items[0]
        active_n = sum(1 for x in s['users'].values() if x['active'])
        canonical_sids.add(s['sid'])
        canonicals.append({
            'identity': key,
            'scriptId': s['sid'],
            'name': s['name'],
            'userId': u['uid'],
            'userName': u['name'],
            'server': u['server'],
            'base': u['base'],
            'oldMaa': s['maa'],
            'oldEmu': s['emu'],
            'oldQueues': sorted(set(s['queues'])),
        })
        if active_n != 1 or s['name'] in OLD_SHARED_NAMES or '补跑' in s['name']:
            warnings.append('canonical 不是单用户正式脚本：%s (%s)' % (s['name'], key))

    # 目标队列
    for c in canonicals:
        cur = [q for q in c['oldQueues'] if q in official_names]
        if len(cur) == 1:
            c['targetQueue'] = cur[0]
            continue
        # 旧共享脚本所在队列
        groups = []
        for old_name in OLD_SHARED_NAMES:
            old = next((s for s in scripts.values() if s['name'] == old_name), None)
            if not old:
                continue
            for u in old['users'].values():
                if not u['active']:
                    continue
                if ident_key(u) == c['identity']:
                    groups.extend([q for q in old['queues'] if q in official_names])
        groups = sorted(set(groups))
        if len(groups) == 1:
            c['targetQueue'] = groups[0]
        else:
            c['targetQueue'] = 'UNQUEUED'
            warnings.append('无法确定队列：%s -> %s' % (c['name'], c['userName']))

    by_q = collections.defaultdict(list)
    for c in canonicals:
        by_q[c['targetQueue']].append(c)

    # 队列内顺序：保留原有队列项顺序，新脚本追加并排序
    ordered = {}
    for qname, members in by_q.items():
        q = next((x for x in official.values() if x['name'] == qname), None)
        if not q:
            continue
        cur_index = {sid: i for i, (_, sid) in enumerate(q['items'])}
        members.sort(key=lambda c: (0 if c['scriptId'] in cur_index else 1,
                                    cur_index.get(c['scriptId'], 9999),
                                    c['name']))
        ordered[qname] = members

    # MAA 池按队列项单双分配
    for qname, members in ordered.items():
        pool = next((v for k, v in POOLS.items() if qname.startswith(k)), [])
        if not pool:
            warnings.append('队列没有 MAA 池：%s' % qname)
            continue
        for i, c in enumerate(members):
            c['newMaa'] = pool[i % len(pool)]

    # 归档脚本 = 非 canonical
    archives = []
    for s in scripts.values():
        if s['sid'] in canonical_sids:
            continue
        active_users = [u for u in s['users'].values() if u['active']]
        new_name = s['name'] if s['name'].startswith('归档-') else '归档-' + s['name']
        archives.append({
            'scriptId': s['sid'],
            'oldName': s['name'],
            'newName': new_name,
            'maa': s['maa'],
            'activeUsers': [u['uid'] for u in active_users],
            'activeNames': [u['name'] for u in active_users],
            'queues': sorted(set(s['queues'])),
        })

    # 冲突检查
    by_maa = collections.defaultdict(list)
    for c in canonicals:
        if c.get('newMaa'):
            by_maa[c['newMaa']].append(c['name'])
    for maa, names in by_maa.items():
        if len(names) > 1:
            qs = {c['targetQueue'] for c in canonicals if c.get('newMaa') == maa}
            if len(qs) > 1:
                warnings.append('MAA 路径跨队列复用：%s -> %s / %s' % (maa, sorted(qs), names))

    plan = {
        'generatedAt': dt.datetime.now().isoformat(timespec='seconds'),
        'source': config_dir,
        'summary': {
            'scripts': len(scripts),
            'activeIdentities': len(identities),
            'canonicalScripts': len(canonicals),
            'archiveScripts': len(archives),
            'queues': len(official),
        },
        'canonicals': sorted(canonicals, key=lambda c: (c['targetQueue'], c['name'])),
        'archives': sorted(archives, key=lambda a: a['oldName']),
        'queues': [
            {
                'queueId': q['qid'],
                'queueName': q['name'],
                'previousTimeEnabled': q['enabled'],
                'schedule': q['schedule'],
                'items': [
                    {
                        'scriptId': c['scriptId'],
                        'scriptName': c['name'],
                        'userId': c['userId'],
                        'userName': c['userName'],
                        'maa': c.get('newMaa', c['oldMaa']),
                    }
                    for c in ordered.get(q['name'], [])
                ],
            }
            for q in sorted(official.values(), key=lambda x: x['name'])
        ],
        'baseChanges': [
            {'scriptId': c['scriptId'], 'userId': c['userId'], 'from': c['base']}
            for c in canonicals if c['base'] != 'independent'
        ],
        'warnings': warnings,
    }
    return plan


def print_plan(plan: dict) -> None:
    s = plan['summary']
    safe_print('REORCH PLAN  scripts=%d  identities=%d  canonical=%d  archive=%d  queues=%d' % (
        s['scripts'], s['activeIdentities'], s['canonicalScripts'], s['archiveScripts'], s['queues']))
    safe_print('')
    for q in plan['queues']:
        safe_print('%s  items=%d  previousTimeEnabled=%s' % (
            q['queueName'], len(q['items']), q['previousTimeEnabled']))
        for i, it in enumerate(q['items'], 1):
            safe_print('  %2d. %-28s %-24s -> %s' % (
                i, it['scriptName'], it['userName'], it['maa']))
    safe_print('')
    safe_print('ARCHIVE %d' % len(plan['archives']))
    for a in plan['archives']:
        safe_print('  %-38s -> %-38s active=%s' % (
            a['oldName'], a['newName'], ','.join(a['activeNames']) or '-'))
    safe_print('')
    safe_print('BASE 待切换 independent: %d' % len(plan['baseChanges']))
    if plan['warnings']:
        safe_print('WARNINGS %d' % len(plan['warnings']))
        for w in plan['warnings']:
            safe_print('  ! ' + w)


def backup_config(config_dir: str) -> str:
    ts = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    dest = os.path.join(BASE, 'backups', 'reorch_' + ts)
    os.makedirs(dest, exist_ok=True)
    for fn in ('ScriptConfig.json', 'QueueConfig.json'):
        src = os.path.join(config_dir, fn)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, fn))
    return dest


def apply_plan(plan: dict, api: str, config_dir: str) -> dict:
    backup = backup_config(config_dir)
    report = {
        'appliedAt': dt.datetime.now().isoformat(timespec='seconds'),
        'backup': backup,
        'archived': [],
        'renamed': [],
        'pathChanges': [],
        'queueItems': {},
        'baseChanges': [],
        'errors': [],
    }
    with io.open(os.path.join(backup, 'reorch_plan.json'), 'w', encoding='utf-8') as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    safe_print('backup: %s' % backup)

    # 1) 暂停所有正式队列
    qs = api_ok(api, '/api/queue/get', {}).get('data') or {}
    for qid, q in qs.items():
        if qid == 'instances' or not isinstance(q, dict):
            continue
        info = q.get('Info') or {}
        if not str(info.get('Name') or '').startswith('并行'):
            continue
        if info.get('TimeEnabled'):
            info['TimeEnabled'] = False
            q['Info'] = info
            api_ok(api, '/api/queue/update', {'queueId': qid, 'data': q})
            safe_print('paused %s' % info.get('Name'))

    # 2) 归档脚本：禁用启用用户 + 改名
    for a in plan['archives']:
        sid = a['scriptId']
        try:
            sd = api_ok(api, '/api/scripts/get', {'scriptId': sid}).get('data') or {}
            sd = sd.get(sid) or {}
            if not sd:
                report['errors'].append('script missing %s' % a['oldName'])
                continue
            users = api_ok(api, '/api/scripts/user/get', {'scriptId': sid, 'userId': None}).get('data') or {}
            for uid in a['activeUsers']:
                u = users.get(uid)
                if not isinstance(u, dict):
                    report['errors'].append('user missing %s/%s' % (a['oldName'], uid))
                    continue
                info = u.get('Info') or {}
                info['Status'] = False
                u['Info'] = info
                api_ok(api, '/api/scripts/user/update', {'scriptId': sid, 'userId': uid, 'data': u})
                report['archived'].append({'scriptId': sid, 'userId': uid, 'name': info.get('Name')})
            info = sd.get('Info') or {}
            if not str(info.get('Name') or '').startswith('归档-'):
                info['Name'] = a['newName']
                sd['Info'] = info
                api_ok(api, '/api/scripts/update', {'scriptId': sid, 'data': sd})
                report['renamed'].append({'scriptId': sid, 'name': a['newName']})
                safe_print('archived %s -> %s' % (a['oldName'], a['newName']))
        except Exception as e:  # noqa: BLE001
            report['errors'].append('archive %s: %s' % (a['oldName'], e))
            safe_print('ERROR archive %s: %s' % (a['oldName'], e))

    # 3) 脚本 MAA 路径
    for c in plan['canonicals']:
        new_maa = c.get('newMaa') or c['oldMaa']
        if new_maa == c['oldMaa']:
            continue
        try:
            sid = c['scriptId']
            sd = api_ok(api, '/api/scripts/get', {'scriptId': sid}).get('data') or {}
            sd = sd.get(sid) or {}
            info = sd.get('Info') or {}
            info['Path'] = new_maa
            sd['Info'] = info
            api_ok(api, '/api/scripts/update', {'scriptId': sid, 'data': sd})
            report['pathChanges'].append({'scriptId': sid, 'name': c['name'],
                                          'from': c['oldMaa'], 'to': new_maa})
            safe_print('maa %-28s %s -> %s' % (c['name'], c['oldMaa'].replace(r'E:\MAA-Multi\\', ''),
                                               new_maa.replace(r'E:\MAA-Multi\\', '')))
        except Exception as e:  # noqa: BLE001
            report['errors'].append('path %s: %s' % (c['name'], e))
            safe_print('ERROR path %s: %s' % (c['name'], e))

    # 4) 重建正式队列
    for q in plan['queues']:
        qid = q['queueId']
        try:
            cur = api_ok(api, '/api/queue/item/get', {'queueId': qid}).get('data') or {}
            schedule = q.get('schedule')
            for iid, it in cur.items():
                if iid == 'instances' or not isinstance(it, dict):
                    continue
                if schedule is None and isinstance(it.get('Schedule'), dict):
                    schedule = it['Schedule']
                api_ok(api, '/api/queue/item/delete', {'queueId': qid, 'queueItemId': iid})
            added = []
            for it in q['items']:
                r = api_ok(api, '/api/queue/item/add', {'queueId': qid})
                iid = str(r.get('queueItemId') or '')
                if not iid:
                    raise RuntimeError('queue/item/add 没有返回 queueItemId')
                payload = {'Info': {'ScriptId': it['scriptId']}}
                if schedule is not None:
                    payload['Schedule'] = schedule
                api_ok(api, '/api/queue/item/update',
                       {'queueId': qid, 'queueItemId': iid, 'data': payload})
                added.append({'itemId': iid, 'scriptId': it['scriptId'], 'name': it['scriptName']})
            report['queueItems'][q['queueName']] = added
            safe_print('queue %-24s items=%d' % (q['queueName'], len(added)))
        except Exception as e:  # noqa: BLE001
            report['errors'].append('queue %s: %s' % (q['queueName'], e))
            safe_print('ERROR queue %s: %s' % (q['queueName'], e))

    # 5) Base 切换由 mas_reorch_base.py 单独执行（需先物化 ConfigFile）
    return report


def verify_plan(plan: dict, api: str) -> dict:
    result = {'ok': True, 'checks': [], 'errors': []}

    def check(name, ok, detail=''):
        result['checks'].append({'check': name, 'ok': bool(ok), 'detail': detail})
        if not ok:
            result['ok'] = False
            result['errors'].append('%s: %s' % (name, detail))

    qs = api_ok(api, '/api/queue/get', {}).get('data') or {}
    archive_sids = {a['scriptId'] for a in plan['archives']}
    all_items = {}
    for q in plan['queues']:
        cur = api_ok(api, '/api/queue/item/get', {'queueId': q['queueId']}).get('data') or {}
        sids = []
        for iid, it in cur.items():
            if iid == 'instances' or not isinstance(it, dict):
                continue
            sids.append(str((it.get('Info') or {}).get('ScriptId') or ''))
        all_items[q['queueName']] = sids
        expected = [x['scriptId'] for x in q['items']]
        check('queue %s items' % q['queueName'], sids == expected,
              'actual=%d expected=%d' % (len(sids), len(expected)))
        check('queue %s no archive' % q['queueName'], not (set(sids) & archive_sids),
              'archive=%s' % sorted(set(sids) & archive_sids))

    # 每个正式脚本只在一个队列
    seen = collections.Counter()
    for name, sids in all_items.items():
        for sid in sids:
            seen[sid] += 1
    dup = [sid for sid, n in seen.items() if n > 1]
    check('script in exactly one queue', not dup, 'dup=%s' % dup[:5])

    # 归档脚本没有启用用户
    bad_archive = []
    for a in plan['archives']:
        users = api_ok(api, '/api/scripts/user/get', {'scriptId': a['scriptId'], 'userId': None}).get('data') or {}
        for uid, u in users.items():
            if uid == 'instances' or not isinstance(u, dict):
                continue
            if active(u):
                bad_archive.append('%s/%s' % (a['newName'], (u.get('Info') or {}).get('Name')))
    check('archive has no active user', not bad_archive, ','.join(bad_archive[:5]))

    # 正式脚本 MAA 路径
    bad_path = []
    for c in plan['canonicals']:
        sd = api_ok(api, '/api/scripts/get', {'scriptId': c['scriptId']}).get('data') or {}
        sd = sd.get(c['scriptId']) or {}
        actual = str((sd.get('Info') or {}).get('Path') or '')
        expect = c.get('newMaa') or c['oldMaa']
        if actual != expect:
            bad_path.append('%s %s!=%s' % (c['name'], actual, expect))
    check('canonical maa path', not bad_path, '; '.join(bad_path[:5]))

    return result


def main() -> int:
    ap = argparse.ArgumentParser(description='MAS 全量脚本去重 + 队列重排（默认 dry-run）')
    ap.add_argument('--config-dir', default=os.path.join(BASE, 'config'))
    ap.add_argument('--mas-api', default=os.environ.get('MAS_API', 'http://127.0.0.1:36163'))
    ap.add_argument('--out', default='dist/reorch_plan.json')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--verify', action='store_true')
    args = ap.parse_args()

    plan = build_plan(args.config_dir)
    print_plan(plan)
    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with io.open(args.out, 'w', encoding='utf-8') as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
        f.write('\n')
    safe_print('')
    safe_print('plan: %s' % args.out)
    if not args.apply:
        safe_print('dry-run：未写 MAS。加 --apply 才执行。')
        return 0
    if plan['warnings']:
        safe_print('存在 WARNING，拒绝 apply；先修 Plan。')
        return 2

    report = apply_plan(plan, args.mas_api, args.config_dir)
    report['verify'] = verify_plan(plan, args.mas_api)
    report_path = os.path.join(os.path.dirname(args.out) or '.', 'reorch_apply_report.json')
    with io.open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write('\n')
    safe_print('')
    safe_print('report: %s' % report_path)
    if report['errors']:
        safe_print('APPLY ERRORS %d' % len(report['errors']))
        for e in report['errors']:
            safe_print('  ! ' + e)
        return 2
    safe_print('verify=%s' % ('PASS' if report['verify']['ok'] else 'FAIL'))
    safe_print('所有正式队列保持暂停；验证后再启用。')
    return 0 if report['verify']['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
