# arknights-event：活动图导航（MaaFramework PI V2）

按 [PI V2 协议](https://github.com/MaaXYZ/MaaFramework/blob/main/docs/zh_cn/3.3-ProjectInterfaceV2协议.md)
写的 MaaFramework 项目：**只负责 UI 导航**，不负责打关。

## 任务

| 任务 | entry | 作用 |
|---|---|---|
| 进入普通图 | `NavMainStart` | 任意界面 → 圣巡之旅 |
| 进入EX图 | `NavEXStart` | 任意界面 → 变革之路 |
| 进入S图 | `NavSStart` | 任意界面 → 喀兰之心 |
| 进入S-1..5详情 | `NavSDetail1..5Start` | **收上一场结算页 + 点开本关详情 + 校验右上角关卡代号**（一次 MaaPiCli 调用干两件事，P1 提速） |
| 切突袭模式 | `NavRaidStart` | 详情页点「突袭模式」，校验按钮变「开始突袭」 |
| 收结算页 | `NavCollectStart` | 行动结束/任务失败/保存阵容弹窗 → 一路回主界面 |
| 收结算页留在地图 | `NavCollectMapStart` | 同上，但停在活动图（手动模式逐关推用） |

## 导航链路

```
主界面 --终端--> 终端页 --雪山降临活动卡--> 活动 hub --圣巡之旅/变革之路/喀兰之心--> 区域图
区域图 --关卡标签--> 关卡详情页 --突袭模式--> 突袭详情
活动图 --返回--> hub --返回--> 终端页 --返回--> 主界面
```

`Nav*Start` 的 `next` 是有序回退链：先认「已经到了」，再认「详情页/地图页/hub/终端/主界面」，
从当前页面能接上的那一跳开始走——**同一个任务在任意页面调用都能收敛，不需要每次退回主界面**。

## 实测耗时（2026-11-22，本机）

| 任务 | 耗时 |
|---|---|
| 收结算页（从活动图/结算页回主界面） | 5~14s |
| 进入S图 / 进入EX图 | 8~15s（已在图上时 5~8s） |
| 进入S-N详情（含收结算） | 9~10s |
| 切突袭模式 | 6~7s（首次切换约 60s 的那次是 ROI 越界导致的重试，已修） |

> 单次 MaaPiCli 启动本身约 5~8s（设备发现 + 资源加载），是主要固定开销。

## 部署 / 运行

```powershell
# 生成运行根 runtime\maa_cli_event（复用 runtime\maa_cli 那套验过的 DLL + OCR 模型 junction）
pwsh -File pi\arknights-event\setup_pi.ps1
# 单跑任务
python tools\picker_push\pi_runner.py "进入EX图"
```

驱动由 `tools/picker_push/pi_runner.py` 写 `runtime\maa_cli_event\config\maa_pi_config.json`
并执行 `MaaPiCli.exe -d`；**每次跑前会把本项目的 interface.json 同步到运行根**，改完不用重 setup。

## 识别方案

- **到位判定用 OCR 认关卡代号**（`OS-EX-1`/`OS-S-3`/`OS-1`…）：模板会被「通关后标签变蓝、
  地图重排」弄失效（2026-11-22 S 图反复踩），OCR 只认文字不受样式影响。
- 其余 UI 锚点一律 **TemplateMatch**（模板 `resource/image/EventNav/*.png`，从本机 1280×720 截图裁）。
  本机 MaaFw 的 OCR 模型能认中文，但「终端」这种大字反而认不出，所以按钮类锚点用模板。
- **改完 JSON 必跑 `tools/precheck.py`**：检查 ROI 越界（MaaFw 是 `[x,y,w,h]`，越界只静默失败）、
  模板文件存在性、next/entry 引用、task 可达性。`setup_pi.ps1` 末尾会自动跑。
- **NavCloseDialog 只点无害词**（关闭/取消/知道了），**绝不匹配「确定/确认」**——那会误点
  碎石补理智、放弃作战之类的按钮。

## 关键坑（都是踩过的）

- `config\maa_pi_config.json` 的 `adb.name` 必须等于 `MaaToolkitAdbDeviceFind` 的名字
  （本机 `1-MuMuPlayer v5+`），否则 controller config 为空 → segfault。
- **ROI 是 `[x, y, w, h]`，必须满足 x+w ≤ 1280、y+h ≤ 720**；越界节点会识别不到（只在日志里
  留一条 `roi is out of range` 警告），2026-11-22 因此让「切突袭模式」白试 60s、收集链卡住。
- **认「在活动图」不要用左侧积雪清理面板模板**：S 图背景不同，匹配只有 0.15 分；
  用关卡标签数组 `NavOnMapAny`（`marker_*`）。
- **不要放「DirectHit → 按返回键」兜底**：认不出的页面会被它按 Android 返回，
  在主界面按返回会弹「是否确认退出游戏？」。认不出就让任务超时失败，交给驱动处理。
- **MaaPiCli 即使任务超时失败也会打印「All tasks have been completed」**：
  结果判定看 `debug/maafw.log` 里最后的 `Tasker.Task.Succeeded/Failed`。
- **刚打完一关不要先调「进入X图」**：游戏停在结算页，区域导航链认不出会硬等到超时（实测 242s）。
  直接调「进入S-N详情」（自带收结算）或先「收结算页」。
- 换活动要重裁哪些模板、改哪些节点：见 `ADAPT-NEW-EVENT.md`。

## 端口漂移（2026-11-22 踩到）

MuMu 实例重启后 adb 端口可能从 16384 漂到 16385。PI 侧按**设备名**匹配（`1-MuMuPlayer v5+`），
不受影响；Python/驱动侧由 `device.resolve_device()` 自动校正。文档里写死端口的说明以这里为准：
连不上先 `MuMuManager.exe info -v 0` 看 `adb_port`。

## 文件

- `interface.json`：PI 声明（controller/resource/task）
- `resource/pipeline/event_nav.json`：全部节点
- `resource/image/EventNav/`：模板（1280×720）
- `resource/model`：junction → `D:\Xiach\M-A\MaaEnd\resource\model`（OCR 模型，setup 脚本建）
- `setup_pi.ps1`：生成 `runtime\maa_cli_event` 运行根
