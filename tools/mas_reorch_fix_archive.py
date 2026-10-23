import json, io, time, urllib.request

API = 'http://127.0.0.1:36163'
PLAN = r'E:\maa-cli-fleet\dist\reorch_plan.json'


def post(path, body, retries=5):
    last = None
    for i in range(retries):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(1 + i)
    raise RuntimeError('%s: %s' % (path, last))


def ok(path, body):
    r = post(path, body)
    if r.get('code') not in (200, None) or r.get('status') == 'error':
        raise RuntimeError('%s -> %s' % (path, json.dumps(r, ensure_ascii=False)))
    return r


plan = json.load(io.open(PLAN, encoding='utf-8'))
targets = [a for a in plan['archives'] if a['oldName'] in ('官-1', '官-3', '官-4')]
print('targets', [(a['oldName'], a['scriptId']) for a in targets])

for a in targets:
    sid = a['scriptId']
    users = ok('/api/scripts/user/get', {'scriptId': sid, 'userId': None}).get('data') or {}
    for uid in a['activeUsers']:
        u = users.get(uid)
        if not isinstance(u, dict):
            print('missing user', a['oldName'], uid)
            continue
        info = u.get('Info') or {}
        if not info.get('Status'):
            continue
        info['Status'] = False
        u['Info'] = info
        ok('/api/scripts/user/update', {'scriptId': sid, 'userId': uid, 'data': u})
        print('disabled', a['oldName'], info.get('Name'))
    sd = ok('/api/scripts/get', {'scriptId': sid}).get('data') or {}
    sd = sd.get(sid) or {}
    info = sd.get('Info') or {}
    if not str(info.get('Name') or '').startswith('归档-'):
        info['Name'] = a['newName']
        sd['Info'] = info
        ok('/api/scripts/update', {'scriptId': sid, 'data': sd})
        print('renamed', a['oldName'], '->', a['newName'])

# verify
scripts = ok('/api/scripts/get', {}).get('data') or {}
bad = []
for sid, s in scripts.items():
    if sid == 'instances' or not isinstance(s, dict):
        continue
    nm = (s.get('Info') or {}).get('Name') or ''
    if nm in ('官-1', '官-2', '官-3', '官-4'):
        bad.append(nm)
print('remaining old shared names', bad)
for a in targets:
    users = ok('/api/scripts/user/get', {'scriptId': a['scriptId'], 'userId': None}).get('data') or {}
    act = [(u.get('Info') or {}).get('Name') for k, u in users.items()
           if k != 'instances' and isinstance(u, dict) and (u.get('Info') or {}).get('Status')]
    print('active in archive', a['newName'], act)
