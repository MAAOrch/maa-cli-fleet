# -*- coding: utf-8 -*-
"""把 shots 里所有成品拼成一张大图（每张缩略 + 账号/时间标题）。"""
import glob
import os
import re
import time
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
SRC = os.environ.get('SHOTS_OUT', os.path.join(ROOT, 'shots'))
DESK = os.environ.get('SHOTS_DESK', os.path.join(os.path.expanduser('~'), 'Desktop', 'maa-shots'))
FONT = r'C:\Windows\Fonts\msyh.ttc'
COLS = 4
CELL_W = 620
CAP_H = 34

files = sorted([p for p in glob.glob(os.path.join(SRC, '*.png')) if not os.path.basename(p).startswith('_')])
print('成品 %d 张' % len(files))
if not files:
    raise SystemExit(1)

f_cap = ImageFont.truetype(FONT, 22)
rows = (len(files) + COLS - 1) // COLS
# 以第一张的长宽比为准（都是 1280x720）
im0 = Image.open(files[0])
cell_h = int(CELL_W * im0.size[1] / im0.size[0])
W = COLS * CELL_W + (COLS + 1) * 8
H = rows * (cell_h + CAP_H) + (rows + 1) * 8 + 46
sheet = Image.new('RGB', (W, H), (24, 26, 32))
d = ImageDraw.Draw(sheet)

f_head = ImageFont.truetype(FONT, 30)
head = 'AUTO-MAS 日常完成凭证拼图 · %s · 共 %d 张（顺序：官-4 → 官-2 → 官-3 → 官-1）' % (
    time.strftime('%Y-%m-%d %H:%M'), len(files))
d.text((12, 10), head, font=f_head, fill=(255, 214, 102))

order = {'官-1': 3, '官-2': 1, '官-3': 2, '官-4': 0}


def key(p):
    b = os.path.basename(p)
    m = re.match(r'(官-\d+)_(.*)_(\d{8}-\d{6})\.png', b)
    return (order.get(m.group(1), 9) if m else 9, m.group(3) if m else b)


files.sort(key=key)

for i, p in enumerate(files):
    r, c = divmod(i, COLS)
    x = 8 + c * (CELL_W + 8)
    y = 46 + 8 + r * (cell_h + CAP_H + 8)
    im = Image.open(p).convert('RGB').resize((CELL_W, cell_h), Image.LANCZOS)
    sheet.paste(im, (x, y))
    b = os.path.basename(p)
    m = re.match(r'(官-\d+)_(.*)_(\d{8})-(\d{6})\.png', b)
    cap = '%s  %s  %s' % (m.group(1), m.group(2), m.group(4)[:2] + ':' + m.group(4)[2:4]) if m else b
    d.rectangle([x, y + cell_h, x + CELL_W, y + cell_h + CAP_H], fill=(38, 42, 52))
    d.text((x + 8, y + cell_h + 5), cap.encode('utf-8').decode('utf-8'), font=f_cap, fill=(230, 234, 240))

out = os.path.join(SRC, '_拼图_%s.png' % time.strftime('%Y%m%d_%H%M'))
sheet.save(out, 'PNG', optimize=True)
desk_out = os.path.join(DESK, os.path.basename(out))
sheet.save(desk_out, 'PNG', optimize=True)
print('已写 %s  (%dx%d, %dKB)' % (out, W, H, os.path.getsize(out) // 1024))
print('桌面副本 %s' % desk_out)
