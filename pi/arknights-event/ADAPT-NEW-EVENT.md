# 新活动 5 分钟适配清单

换下次活动时，**只动这几处**，不要碰 `internal/copilotpick` 的通用逻辑、`cmd/fleet`、`web/`。

## 1. 裁模板（`pi/arknights-event/resource/image/EventNav/`，1280×720）

从本机截图裁，**文件名保持不变**（pipeline 引用的是固定文件名），覆盖即可：

| 模板 | 从哪裁 | 备注 |
|---|---|---|
| `home_terminal.png` | 主界面「终端」大字按钮 | 本客户端通用，一般不用换 |
| `terminal_event_card.png` | 终端页左侧当期活动卡 | 换活动必换 |
| `hub_main.png` / `hub_ex.png` / `hub_s.png` | 活动 hub 右下角三个区域入口 | 换活动必换 |
| `tab_main.png` / `tab_ex.png` / `tab_s.png` | 活动地图页底部三个区域页签 | 换活动必换 |
| `marker_main1.png` / `marker_main_st1.png` | 主线图上的关卡标签 | 换活动换代号 |
| `marker_ex1..4.png` | EX 图上的关卡标签 | 同上 |
| `marker_s1.png` | S 图上的关卡标签 | 同上 |
| `result_end.png` / `result_fail.png` / `save_dialog.png` / `detail_startbtn.png` | 结算页「行动结束」、任务失败页、保存阵容弹窗、详情页「开始行动」 | 通用，一般不用换 |

裁完自检（用 Python）：

```python
import cv2
img = cv2.imread(r"screenshot.png"); t = cv2.imread(r"模板.png")
print(cv2.minMaxLoc(cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED))[1])  # 应 ≈1.0
```

## 2. 改节点里的关卡代号

`resource/pipeline/event_nav.json`：

- `NavArrivedEX` / `NavArrivedS` / `NavArrivedMain` 的 `expected` 列表 = 新活动关卡代号
- `NavStageS1..5` 的 `expected` = `新前缀-S-N`（S 关必须走详情页+手动模式，见下）
- `NavDetailCodeS1..5` 的 `expected` 同理

> 关卡代号前缀（`OS-` 之类）在**模板图和节点里都是硬编码**的，没有自动发现机制。

## 3. 改推关配置

`tools/picker_push/events/<event>.json`：

- `event` = 作业站活动 id（如 `act52side`），`stages` 里每关的 `stageName` = 作业站关卡代码（`act46side_ex07`）
- `areas` 不变（main/ex/s），`manual_areas` 默认 `["s"]`
- `cost` 按关卡理智消耗填（不确定就 10，只用于估算什么时候停）

## 4. 先 dry-run 体检

```powershell
# 0) 静态自检：ROI 越界 / 模板缺失 / 节点引用错 —— 改完 JSON 必跑
python pi\arknights-event\tools\precheck.py
#    期望：0 error, 0 warn；有 error 一定先修（越界节点运行时只会静默识别失败）

# 1) 导航 + 作业候选体检
python tools\picker_push\push_event.py --event <event> --stages <第一关> --dry-run
#    期望：PI 导航 ✓ + 作业候选非空
```

## 5. 推第一关验证判定

```powershell
python tools\picker_push\push_event.py --event <event> --stages <第一关> --mode normal
```

看三件事：PI 导航耗时（正常 10~20s/次）、picker 判定（onMap 看任务链 / 手动模式看结算截图）、
台账 `D:\copilot-picker-local\progress.json` 有没有记上 3 星。

## 已知坑

- **S 关不能用 onMap**：MAA 的 `find_stage` 会把 `OS-S-N` OCR 成 `0S-S-N`，找不到就滑屏死循环。
  标准做法 = PI `进入S-N详情` + picker `manualMode`（还能拿结算截图真判星）。
- **刚打完一关不要先跑「进入X图」**：游戏停在结算页时区域导航链认不出，会硬等到超时（实测 242s）；
  直接跑「进入S-N详情」（自带收结算）或先跑「收结算页」。
- **`map_panel.png` 不是通用地图锚点**：S 图上背景不同，匹配只有 0.15 分；认「在活动图」用关卡
  标签 `marker_*` 数组（`NavOnMapAny`）。
- 换分辨率（非 1280×720 / 非 16:9）要重做全部模板与 ROI。
