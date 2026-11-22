# -*- coding: utf-8 -*-
"""从已有截图里裁出「页面锚点」模板，给 nav.py 做状态识别用。

锚点原则：挑该页面**独有**、位置固定的东西（同一分辨率下 UI 不动），
用「固定 ROI + 与模板的平均绝对差」判状态，不做全图搜索。
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
TPL_DIR = HERE / "templates"

# state -> (源截图, ROI[x0,y0,x1,y1], 说明)
SOURCES = {
    # 主界面：右侧「终端」大牌子的标题区域（别的页面没有这块）
    "home": (r"D:\maa-cli-fleet\_tmp\pk\home_ok.png", (720, 215, 1090, 300), "主界面 终端牌"),
    # 终端页：左侧活动缩略图
    "terminal": (r"D:\maa-cli-fleet\_tmp\pk\terminal.png", (150, 300, 340, 470), "终端 活动卡"),
    # 活动 hub：左下「时间啊，向前！」logo
    "hub": (r"D:\maa-cli-fleet\_tmp\pk\event.png", (20, 470, 330, 590), "活动 hub logo"),
    # EX 图：中间清真寺金色穹顶（S 图这块是绿草地）
    "map_ex": (r"D:\maa-cli-fleet\_tmp\pk\nav_ex.png", (520, 150, 820, 260), "EX 图 清真寺"),
    # S 图：左侧红旗/绿草地
    "map_s": (r"D:\maa-cli-fleet\_tmp\pk\smap2.png", (240, 330, 360, 620), "S 图 红旗草地"),
    # 关卡详情页：右下「开始行动」按钮
    "detail": (r"D:\maa-cli-fleet\_tmp\pk\tap2.png", (1070, 625, 1215, 700), "详情页 开始行动"),
    # 结算：左上「行动结束」大字
    "settlement": (
        r"D:\copilot-picker-local\runs\20261122-002730-z6s8\settle.png",
        (45, 190, 360, 290),
        "结算 行动结束",
    ),
    # 标题页：「清除缓存/网络检测」按钮（重启游戏后停这里，点 START 才进主界面）
    "title": (r"D:\maa-cli-fleet\_tmp\pk\state_x.png", (25, 22, 310, 70), "标题页 清除缓存"),
    # 过场/加载页：Rhodes Island 三角标
    "loading": (r"D:\maa-cli-fleet\_tmp\pk\now_y.png", (500, 210, 760, 470), "加载 Rhodes Island"),
    # 干员页：顶部职业筛选条（点错时最常见的目标）
    "operators": (r"D:\maa-cli-fleet\_tmp\pk\step1.png", (700, 20, 1250, 60), "干员页 筛选条"),
}


def main() -> None:
    TPL_DIR.mkdir(parents=True, exist_ok=True)
    meta = {}
    for state, (src, roi, desc) in SOURCES.items():
        im = Image.open(src).convert("RGB")
        crop = im.crop(roi)
        out = TPL_DIR / f"{state}.png"
        crop.save(out)
        meta[state] = {"roi": list(roi), "desc": desc, "file": out.name}
        print(f"{state:11s} roi={roi} -> {out.name}")
    (TPL_DIR / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"共 {len(meta)} 个锚点，写到 {TPL_DIR}")


if __name__ == "__main__":
    main()
