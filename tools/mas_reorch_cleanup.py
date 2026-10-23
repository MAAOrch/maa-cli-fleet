import json
import urllib.request

API = 'http://127.0.0.1:36163'
PLAN = r'E:\maa-cli-fleet\dist\reorch_plan.json'


def post(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def ok(path, body):
    r = post(path, body)
    if r.get('code') not in (200, None) or r.get('status') == 'error':
        raise RuntimeError('%s -> %s' % (path, json.dumps(r, ensure_ascii=False)))
    return r


plan = json.load(open(PLAN, encoding='utf-8'))
canon = {c['scriptId'] for c in plan['canonicals']}

# 1) 清空归档验收队列里指向正式脚本的项
qs = ok('/api/queue/get', {}).get('data') or {}
for qid, q in qs.items():
    if qid == 'instances' or not isinstance(q, dict):
        continue
    name = str((q.get('Info') or {}).get('Name') or '')
    if not name.startswith('归档-'):
        continue
    items = ok('/api/queue/item/get', {'queueId': qid}).get('data') or {}
    removed = 0
    for iid, it in items.items():
        if iid == 'instances' or not isinstance(it, dict):
            continue
        sid = str((it.get('Info') or {}).get('ScriptId') or '')
        if sid in canon:
            ok('/api/queue/item/delete', {'queueId': qid, 'queueItemId': iid})
            removed += 1
    if removed:
        print('cleared %d canonical items from %s' % (removed, name))

# 2) 正式脚本排前，归档脚本排后（保持各自相对顺序）
data = ok('/api/scripts/get', {}).get('data') or {}
order = [sid for sid in data if sid != 'instances' and isinstance(data[sid], dict)]
new_order = [sid for sid in order if sid in canon] + [sid for sid in order if sid not in canon]
ok('/api/scripts/order', {'indexList': new_order})
print('reordered scripts: canonical=%d archive=%d' % (len(canon), len(new_order) - len(canon)))

# 3) 校验
data = ok('/api/scripts/get', {}).get('data') or {}
now = [sid for sid in data if sid != 'instances' and isinstance(data[sid], dict)]
names = [str((data[sid].get('Info') or {}).get('Name') or '') for sid in now]
print('first 5:', names[:5])
print('last 5:', names[-5:])
print('canonical all before archive:', all(('归档-' not in n) for n in names[:len(canon)]))
