# -*- coding: utf-8 -*-
r"""shot_annotate.py —— 给「日常完成截图」做二次处理：在图上叠一层信息面板。

面板内容（用户口径 2026-10-03）：
  1. 截图时间
  2. 该账号的用户名
  3. 该账号的手机号 + 各任务的完成情况与时长

两种输入：
  A) --json <info.json>   结构化信息（推荐；由调度器/看护进程生成）
  B) --from-out <x.out>   maa-cli 任务输出（机队 daily_*.out），自动解析 Summary 段落

用法：
  python shot_annotate.py --shot in.png --json a49.json --out out.png
  python shot_annotate.py --shot in.png --from-out daily_a49_1002_080010.out ^
         --name 示例账号 --phone <PHONE> --server Official --emu 49 --out out.png
  python shot_annotate.py --json x.json --demo --out demo.png     # 没图时生成占位底图

红线：手机号默认**完整显示**；`--mask-phone` 可改 3+****+4 掩码。
隐私：手机号属于个人信息，对外分享截图请务必加 `--mask-phone`。
本脚本不联网、不改别的文件；只读输入、只写 --out。
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception as e:  # pragma: no cover
    print('需要 Pillow：python -m pip install pillow（%s）' % e, file=sys.stderr)
    raise SystemExit(2)

FONTS = [
    r'C:\Windows\Fonts\msyh.ttc',
    r'C:\Windows\Fonts\msyhbd.ttc',
    r'C:\Windows\Fonts\simhei.ttf',
    r'C:\Windows\Fonts\Deng.ttf',
    '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
]
MONO = [r'C:\Windows\Fonts\consola.ttf', r'C:\Windows\Fonts\lucon.ttf']

ANN_NAME = {'LungmenDowntown@Annihilation': '龙门市区', 'LungmenOutskirts@Annihilation': '龙门外环',
            'Chernobog@Annihilation': '切尔诺伯格', 'Annihilation': '剿灭（默认）'}

STATUS_OK = ('completed', '完成', '成功')
STATUS_BAD = ('error', 'failed', '失败', '异常')


def _font(size: int, mono: bool = False) -> ImageFont.FreeTypeFont:
    for p in (MONO if mono else []) + FONTS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def mask_phone(p: str) -> str:
    return re.sub(r'(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)', r'\1****\2', p or '')


# ---------------------------------------------------------------- 解析 maa-cli .out
LINE_RE = re.compile(r'^\[(?P<name>[^\]]+)\]\s*(?P<start>\d\d:\d\d:\d\d)\s*-\s*(?P<end>\d\d:\d\d:\d\d)\s*\((?P<dur>[^)]+)\)\s*(?P<status>\S+)')


def trim_round(tasks: list[dict]) -> list[dict]:
    """只留最后一轮：从最后一个「开始唤醒」到末尾（一轮 = StartUp 开头）。"""
    for i in range(len(tasks) - 1, -1, -1):
        if '唤醒' in tasks[i].get('name', '') or tasks[i].get('name') == 'StartUp':
            return tasks[i:]
    return tasks


def parse_out(path: str) -> list[dict]:
    tasks = []
    with io.open(path, encoding='utf-8', errors='replace') as f:
        for ln in f:
            m = LINE_RE.match(ln.strip())
            if m:
                d = m.groupdict()
                tasks.append(dict(name=d['name'], start=d['start'], end=d['end'],
                                  dur=d['dur'], status=d['status']))
    return tasks


def build_info(args) -> dict:
    if args.json:
        with io.open(args.json, encoding='utf-8-sig') as f:
            info = json.load(f)
    else:
        info = {}
    tasks = info.get('tasks') or []
    if args.from_out:
        tasks = parse_out(args.from_out) or tasks
    info['tasks'] = tasks
    if args.shot and os.path.exists(args.shot) and 'shot_mtime' not in info:
        info['shot_mtime'] = dt.datetime.fromtimestamp(os.path.getmtime(args.shot)).strftime('%Y-%m-%d %H:%M:%S')
    for k, v in (('account', args.name), ('phone', args.phone), ('server', args.server), ('emu', args.emu)):
        if v:
            info[k] = v
    if args.ts:
        info['ts'] = args.ts
    info.setdefault('ts', dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    return info


def _status_icon(status: str) -> tuple[str, tuple[int, int, int]]:
    s = (status or '').lower()
    if any(k in s for k in STATUS_OK):
        return '✔', (120, 230, 120)
    if any(k in s for k in STATUS_BAD):
        return '✘', (255, 140, 120)
    return '•', (230, 230, 230)


def render(shot: str, info: dict, out: str, args) -> dict:
    base = Image.open(shot).convert('RGB') if shot else Image.new('RGB', (900, 1600), (28, 30, 36))
    W, H = base.size
    # 竖屏（手机 900x1600）放大；横屏（MAA 窗口图 1280x720）要收小，否则面板挤满画面
    landscape = W > H * 1.4
    scale = 0.62 if landscape else max(W / 900.0, 1.0)
    two_col = landscape                      # 横屏（1280x720）用两栏，不然面板压住大厅
    f_title = _font(int(34 * scale), mono=False)
    f_text = _font(int(28 * scale))
    f_mono = _font(int(26 * scale), mono=True)

    tasks = info.get('tasks') or []
    acct = '%s%s' % (info.get('account', ''), ('（%s）' % info['account_id']) if info.get('account_id') else '')
    phone = mask_phone(info['phone']) if (args.mask_phone and info.get('phone')) else (info.get('phone') or '')
    srv = '%s%s' % (info.get('server', ''), ('｜模拟器 idx %s' % info['emu']) if info.get('emu') else '')
    # 关卡信息（用户口径：要能看出刷的哪个关）
    st = info.get('stage') or ''
    st_rem = info.get('stage_remain') or ''
    ann_raw = info.get('ann') or ''
    ann = ANN_NAME.get(ann_raw, ann_raw)
    if ann_raw in ('', 'Close', 'close'):
        ann = '关'
    stage_line = '关卡：%s%s%s' % (st or '-', ('（剩余理智走 %s）' % st_rem) if st_rem else '',
                                ('｜剿灭：%s' % ann) if ann else '')
    # Fight 行标关卡：第 1 个刷主关卡，第 2 个刷剩余理智
    fi = 0
    for t in tasks:
        if '理智' in (t.get('name') or ''):
            t['name'] = '%s(%s)' % (t['name'], st if fi == 0 else (st_rem or '?'))
            fi += 1
    if landscape:
        lines_meta = []
        head1 = '账号：%s   手机号：%s   截图时间：%s' % (acct, phone, info.get('ts', ''))
        head2 = '服务器：%s   %s' % (srv, stage_line)
        head3 = ('来源：' + info['source']) if info.get('source') else ''
        lines_meta = []
    else:
        lines_meta = [('截图时间', info.get('ts', '')), ('账号', acct), ('手机号', phone),
                      ('服务器', srv), ('关卡', stage_line.replace('关卡：', ''))]
        if info.get('source'):
            lines_meta.append(('来源', info['source']))

    ok = sum(1 for t in tasks if any(k in (t.get('status') or '').lower() for k in STATUS_OK))
    bad = sum(1 for t in tasks if any(k in (t.get('status') or '').lower() for k in STATUS_BAD))

    def total_minutes():
        tot = 0.0
        for t in tasks:
            d = (t.get('dur') or '').strip()
            m = re.match(r'(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?$', d)
            if m and (m.group(1) or m.group(2)):
                tot += int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
        return tot

    tot_sec = int(total_minutes())
    summary = '合计 %d/%d 完成%s｜总时长 %dm%02ds' % (
        ok, len(tasks), ('，%d 项异常' % bad) if bad else '', tot_sec // 60, tot_sec % 60)
    if info.get('sanity_cur') is not None:
        summary += '｜理智 %s/%s' % (info['sanity_cur'], info.get('sanity_max', ''))

    pad = int(28 * scale)
    lh = int(40 * scale)
    title_h = int(52 * scale)
    n_head = 3 if landscape else len(lines_meta)
    n_task_rows = ((len(tasks) + 1) // 2) if two_col else len(tasks)
    n_lines = n_head + (1 if tasks else 0) + n_task_rows + 2
    panel_h = pad * 2 + title_h + lh * (n_lines + 1) + int(10 * scale)
    panel_h = min(panel_h, int(H * (0.62 if landscape else 0.8)))

    panel = Image.new('RGBA', (W, panel_h), (12, 14, 18, 214))
    base.paste(panel, (0, H - panel_h), panel)
    d = ImageDraw.Draw(base)

    y = H - panel_h + pad
    d.text((pad, y), '明日方舟 · 日常完成凭证', font=f_title, fill=(255, 214, 102))
    y += title_h

    if landscape:
        d.text((pad, y), head1, font=f_text, fill=(238, 240, 245)); y += lh
        d.text((pad, y), head2, font=f_text, fill=(255, 214, 102)); y += lh
        if head3:
            d.text((pad, y), head3, font=f_text, fill=(190, 198, 210))
        y += lh
    else:
        for k, v in lines_meta:
            if not v:
                continue
            d.text((pad, y), '%s：' % k, font=f_text, fill=(160, 170, 185))
            d.text((pad + int(150 * scale), y), str(v), font=f_text, fill=(238, 240, 245))
            y += lh

    if tasks:
        y += int(6 * scale)
        d.text((pad, y), '任务', font=f_text, fill=(160, 170, 185))
        d.text((pad + int(150 * scale), y), '时段', font=f_text, fill=(160, 170, 185))
        d.text((pad + int(360 * scale), y), '耗时', font=f_text, fill=(160, 170, 185))
        d.text((pad + int(500 * scale), y), '状态', font=f_text, fill=(160, 170, 185))
        y += lh
        colw = int(W / 2) if two_col else 0
        for i, t in enumerate(tasks):
            icon, color = _status_icon(t.get('status', ''))
            cx = pad + (colw if (two_col and i % 2 == 1) else 0)
            cy = y + (lh * (i // 2) if two_col else 0)
            d.text((cx, cy), str(t.get('name', ''))[:10], font=f_text, fill=(238, 240, 245))
            d.text((cx + int(110 * scale * 1.6), cy), '%s-%s' % (t.get('start', ''), t.get('end', '')),
                   font=f_mono, fill=(190, 198, 210))
            d.text((cx + int(300 * scale * 1.6), cy), str(t.get('dur', '')), font=f_mono, fill=(190, 198, 210))
            d.text((cx + int(400 * scale * 1.6), cy), '%s %s' % (icon, t.get('status', '')), font=f_text, fill=color)
            if not two_col:
                y += lh
        if two_col:
            y += lh * ((len(tasks) + 1) // 2)

    y += int(6 * scale)
    d.text((pad, y), summary, font=f_text, fill=(255, 214, 102))

    if not shot:
        d.text((pad, int(pad)), '（演示底图：真实截图会盖在这张图上）', font=f_text, fill=(120, 128, 140))

    os.makedirs(os.path.dirname(os.path.abspath(out)) or '.', exist_ok=True)
    base.save(out, 'PNG', optimize=True)
    return dict(out=out, size=[W, H], panel_h=panel_h, tasks=len(tasks), ok=ok, bad=bad,
                total_sec=tot_sec, bytes=os.path.getsize(out))


def main() -> int:
    ap = argparse.ArgumentParser(description='日常完成截图的二次处理（叠信息面板）')
    ap.add_argument('--shot', help='原始截图 PNG（缺省则用占位底图，配 --demo 用）')
    ap.add_argument('--json', help='结构化信息 JSON')
    ap.add_argument('--from-out', dest='from_out', help='maa-cli 的 daily_*.out（自动解析任务段）')
    ap.add_argument('--out', required=True, help='输出 PNG')
    ap.add_argument('--ts', help='截图时间（默认 now）')
    ap.add_argument('--name', help='账号用户名')
    ap.add_argument('--phone', help='手机号')
    ap.add_argument('--server', help='服务器（Official/Bilibili/YoStarJP/txwy）')
    ap.add_argument('--emu', help='模拟器 idx')
    ap.add_argument('--mask-phone', action='store_true', help='手机号打码 3+****+4')
    ap.add_argument('--demo', action='store_true', help='没图时生成占位底图')
    args = ap.parse_args()
    if not args.shot and not args.demo:
        ap.error('要么给 --shot，要么给 --demo')
    info = build_info(args)
    res = render(args.shot if args.shot and os.path.exists(args.shot) else '', info, args.out, args)
    print(json.dumps(res, ensure_ascii=True))  # 控制台是 GBK：ASCII 输出防崩
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
