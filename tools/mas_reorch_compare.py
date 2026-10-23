#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重排前后账号配置对账：除 Base 和运行数据外，业务字段不应变化。

对比：
  * Info.Name / Id / Server / Notes / Status / RemainedDay
  * Stage / Task / Notify 整段
  * 可选：Password 解密后的明文是否一致（需要 MAS venv + pywin32）
脚本级只允许 Info.Name / Info.Path 变化。
"""
from __future__ import annotations

import argparse
import io
import json
import sys


def load(path: str) -> dict:
    with io.open(path, encoding='utf-8-sig') as f:
        return json.load(f)


def user_of(cfg: dict, sid: str, uid: str) -> dict:
    return ((cfg.get(sid) or {}).get('SubConfigsInfo', {}).get('UserData', {}) or {}).get(uid) or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--old-config', required=True)
    ap.add_argument('--cur-config', default=r'E:\AUTO-MAS\config\ScriptConfig.json')
    ap.add_argument('--plan', default=r'E:\maa-cli-fleet\dist\reorch_plan.json')
    ap.add_argument('--check-password', action='store_true')
    args = ap.parse_args()

    old, cur, plan = load(args.old_config), load(args.cur_config), load(args.plan)
    decrypt = None
    if args.check_password:
        sys.path.insert(0, r'E:\AUTO-MAS\repo')
        from app.utils.platform.windows.secret import dpapi_decrypt  # noqa: PLC0415
        decrypt = dpapi_decrypt

    issues = []
    checked = 0
    for c in plan['canonicals']:
        sid, uid = c['scriptId'], c['userId']
        o, n = user_of(old, sid, uid), user_of(cur, sid, uid)
        if not o or not n:
            issues.append((c['name'], 'missing old/new user'))
            continue
        checked += 1
        oi, ni = o.get('Info') or {}, n.get('Info') or {}
        for k in ('Name', 'Id', 'Server', 'Notes'):
            if oi.get(k) != ni.get(k):
                issues.append((c['name'], 'Info.%s changed' % k))
        for k in ('Status', 'RemainedDay'):
            if oi.get(k) != ni.get(k):
                issues.append((c['name'], 'Info.%s %s -> %s' % (k, oi.get(k), ni.get(k))))
        for section in ('Stage', 'Task', 'Notify'):
            if (o.get(section) or {}) != (n.get(section) or {}):
                issues.append((c['name'], '%s changed' % section))
        if decrypt:
            op = oi.get('Password') or ''
            np = ni.get('Password') or ''
            if op != np:
                try:
                    same = decrypt(op) == decrypt(np)
                except Exception as e:  # noqa: BLE001
                    issues.append((c['name'], 'password decrypt error %s' % type(e).__name__))
                else:
                    if not same:
                        issues.append((c['name'], 'password plaintext changed'))

    script_issues = []
    for c in plan['canonicals']:
        sid = c['scriptId']
        oi = (old.get(sid) or {}).get('Info') or {}
        ni = (cur.get(sid) or {}).get('Info') or {}
        for k in oi:
            if k in ('Name', 'Path'):
                continue
            if oi.get(k) != ni.get(k):
                script_issues.append((c['name'], k))

    print('checked=%d user_issues=%d script_issues=%d' % (
        checked, len(issues), len(script_issues)))
    for x in issues[:50]:
        print(' !', x)
    for x in script_issues[:20]:
        print(' ! script', x)
    return 0 if not issues and not script_issues else 2


if __name__ == '__main__':
    raise SystemExit(main())
