import collections
import io
import json
import sys
import urllib.request

API = 'http://127.0.0.1:36163'
PLAN = r'E:\maa-cli-fleet\dist\reorch_plan.json'


def post(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def data(path, body):
    r = post(path, body)
    if r.get('code') not in (200, None) or r.get('status') == 'error':
        raise RuntimeError('%s -> %s' % (path, json.dumps(r, ensure_ascii=False)))
    return r.get('data') or {}


plan = json.load(io.open(PLAN, encoding='utf-8'))
checks = []
ALLOW_ENABLED = '--allow-enabled' in sys.argv
ALLOW_ACTIVE = '--allow-active' in sys.argv


def check(name, ok, detail=''):
    checks.append((name, bool(ok), detail))
    print(('PASS ' if ok else 'FAIL ') + name + (' :: ' + str(detail) if detail else ''))


script_raw = data('/api/scripts/get', {})
scripts = {sid: s for sid, s in script_raw.items() if sid != 'instances' and isinstance(s, dict)}
qraw = data('/api/queue/get', {})
queues = {qid: q for qid, q in qraw.items() if qid != 'instances' and isinstance(q, dict)}

off_q = {qid: q for qid, q in queues.items() if str((q.get('Info') or {}).get('Name') or '').startswith('并行')}
check('official queue count', len(off_q) == 7, len(off_q))
if not ALLOW_ENABLED:
    check('all queues paused', all(not (q.get('Info') or {}).get('TimeEnabled') for q in off_q.values()),
          [(q.get('Info') or {}).get('Name') for q in off_q.values() if (q.get('Info') or {}).get('TimeEnabled')])

items_by_queue = {}
for qid, q in off_q.items():
    qname = (q.get('Info') or {}).get('Name')
    cur = data('/api/queue/item/get', {'queueId': qid})
    sids = [str((it.get('Info') or {}).get('ScriptId') or '') for k, it in cur.items()
            if k != 'instances' and isinstance(it, dict)]
    items_by_queue[qname] = sids

for pq in plan['queues']:
    qname = pq['queueName']
    expect = [x['scriptId'] for x in pq['items']]
    check('queue order %s' % qname, items_by_queue.get(qname) == expect,
          'actual=%d expected=%d' % (len(items_by_queue.get(qname, [])), len(expect)))

all_sids = [sid for sids in items_by_queue.values() for sid in sids]
check('total queue items', len(all_sids) == 51, len(all_sids))
cnt = collections.Counter(all_sids)
check('script in one queue', not [s for s, n in cnt.items() if n > 1], [s for s, n in cnt.items() if n > 1])

# active identity uniqueness + canonical identity
ident = collections.defaultdict(list)
active_total = 0
for sid, s in scripts.items():
    users = data('/api/scripts/user/get', {'scriptId': sid, 'userId': None})
    for uid, u in users.items():
        if uid == 'instances' or not isinstance(u, dict):
            continue
        inf = u.get('Info') or {}
        if inf.get('Status') and inf.get('RemainedDay') != 0:
            active_total += 1
            key = ('id:' + str(inf.get('Id'))) if str(inf.get('Id') or '').strip() else (
                'name:' + str(inf.get('Name') or '') + '|' + str(inf.get('Server') or ''))
            ident[key].append((sid, str(inf.get('Name') or '')))
check('active identities == 51', len(ident) == 51, len(ident))
check('active user records == 51', active_total == 51, active_total)
dups = {k: v for k, v in ident.items() if len(v) > 1}
check('no duplicate active identity', not dups, list(dups.items())[:3])

identity_owner = {k: v[0][0] for k, v in ident.items()}
plan_owner = {c['identity']: c for c in plan['canonicals']}
check('canonical identity set', set(identity_owner) == set(plan_owner),
      'missing=%d extra=%d' % (len(set(plan_owner) - set(identity_owner)), len(set(identity_owner) - set(plan_owner))))
wrong = []
for k, c in plan_owner.items():
    owner = identity_owner.get(k)
    if owner != c['scriptId']:
        wrong.append((k, owner, c['scriptId']))
check('canonical owner correct', not wrong, wrong[:5])

# archive state
bad_arch = []
for a in plan['archives']:
    s = scripts.get(a['scriptId'])
    if not s:
        bad_arch.append((a['newName'], 'missing'))
        continue
    nm = str((s.get('Info') or {}).get('Name') or '')
    if not nm.startswith('归档-'):
        bad_arch.append((nm, 'not renamed'))
check('archive renamed', not bad_arch, bad_arch[:5])
arch_active = []
for a in plan['archives']:
    users = data('/api/scripts/user/get', {'scriptId': a['scriptId'], 'userId': None})
    for uid, u in users.items():
        if uid == 'instances' or not isinstance(u, dict):
            continue
        inf = u.get('Info') or {}
        if inf.get('Status'):
            arch_active.append((a['newName'], inf.get('Name')))
check('archive no active user', not arch_active, arch_active[:5])
check('old shared names gone', not [n for n in ('官-1', '官-2', '官-3', '官-4')
                                    if any(str((s.get('Info') or {}).get('Name')) == n for s in scripts.values())])

# MAA paths
bad_path = []
for c in plan['canonicals']:
    s = scripts.get(c['scriptId']) or {}
    actual = str((s.get('Info') or {}).get('Path') or '')
    expect = c.get('newMaa') or c['oldMaa']
    if actual != expect:
        bad_path.append((c['name'], actual, expect))
check('canonical maa path', not bad_path, bad_path[:5])

maa_queues = collections.defaultdict(set)
for qname, sids in items_by_queue.items():
    for sid in sids:
        maa = str((scripts.get(sid, {}).get('Info') or {}).get('Path') or '')
        maa_queues[maa].add(qname)
cross = {m: q for m, q in maa_queues.items() if len(q) > 1}
check('maa path not shared across queues', not cross, cross)

# runtime
snap = json.load(urllib.request.urlopen(API + '/api/dispatch/runtime-snapshot', timeout=60))
if not ALLOW_ACTIVE:
    check('no active dispatch task', not snap.get('tasks'), [t.get('taskId') for t in snap.get('tasks', [])])

failed = [c for c in checks if not c[1]]
print('')
print('RESULT', 'PASS' if not failed else 'FAIL %d/%d' % (len(failed), len(checks)))
