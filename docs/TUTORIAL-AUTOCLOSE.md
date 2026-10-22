# 新主题教程弹窗自动关闭（机端常驻服务）

> 2026-10-21 立。用户需求：新肉鸽主题首次进入会弹教程（几页、左上角 `✕ 閉じる`），MAA 不会关；
> 不关 → 车道反复"卡死→杀 maa→重开"，刷取效率被拖垮。即使手动关过一次，偶发 bug 仍可能再弹，
> 所以要做成**一直在线**的关闭机制，而不是一次性的。

## 机制

机端常驻一个 PowerShell watcher：

1. 每 ~0.8 秒枚举跑着肉鸽的 MAA：`maa.exe ... --batch run rogue_*`，从命令行里取 `-a 127.0.0.1:<port>`；
2. 对每台设备 `adb exec-out screencap` 截屏 → 裁左上角按钮区域（1280×720 基准：(95,85)-(215,135)）
   与模板 `runner\tutorial_tpl_close.png` 求平均像素差；
3. 分数低于阈值（720p=10；其它分辨率自动缩放、阈值 25）且距上次点击 >2.5 秒 → 存证据截图 + `input tap` 点关闭
   （720p 点 (152,109)，按分辨率等比缩放）；
4. 平时只截图不点击；没有肉鸽 maa 在跑时睡 2 秒轮询。

热路径都是编译好的 C#（Add-Type，GDI+ LockBits），每台设备一次 ~0.3 秒，2~5 台设备轮询周期 1~3 秒。

## 组成

| 文件 / 任务 | 说明 |
|---|---|
| `ops\tutorial_watch.ps1` | 主循环。`-SelfTest -SelfTestImage <png>` 单图打分；`-Probe` 对当前在跑的肉鸽设备打分（部署/排障用） |
| `ops\boot_tutorial_watch.ps1` | 幂等拉起 / 看门狗：进程在且心跳 <5 分钟 → 跳过；心跳过期/进程没了 → 重启。`-Status` 看状态 |
| `runner\tutorial_tpl_close.png` | 关闭按钮模板（120×50，取自 2026-10-21 JieGarden「通宝」教程弹窗） |
| `FleetTutorialWatch-AutoStart` | 计划任务，AtLogon（Administrator / Interactive / Highest）→ `ops\boot_tutorial_watch.ps1` |
| `FleetTutorialWatch-Watchdog` | 计划任务，每 10 分钟 → 同上（IgnoreNew） |
| `runner\tutorial_watch.log` | 运行日志（超过 8MB 自动转 .old；启动/每次点击一行，每 200 轮一行 alive） |
| `runner\tutorial_shots\` | 每次命中前存一张证据截图（保留最近 40 张） |
| `runner\tutorial_watch.hb` | 心跳文件（每轮刷新），给 boot 脚本判活 |
| `runner\tutorial_watch.stop` | 人工停：存在则主循环退出、boot 不再拉起 |

## 常用命令（机端 <项目>）

```powershell
# 状态（进程 / 心跳 / 是否被人工停）
powershell -NoProfile -ExecutionPolicy Bypass -File <项目>\ops\boot_tutorial_watch.ps1 -Status

# 抓一次实时分数（正常画面应为 NORMAL/高分；出现教程弹窗时应为 POPUP/低分）
powershell -NoProfile -ExecutionPolicy Bypass -File <项目>\ops\tutorial_watch.ps1 -Probe

# 单图自检
powershell -NoProfile -ExecutionPolicy Bypass -File <项目>\ops\tutorial_watch.ps1 -SelfTest -SelfTestImage <png>

# 看日志 / 证据
Get-Content <项目>\runner\tutorial_watch.log -Tail 20
Get-ChildItem <项目>\runner\tutorial_shots

# 人工停 / 恢复
New-Item -ItemType File <项目>\runner\tutorial_watch.stop   # 停
Remove-Item <项目>\runner\tutorial_watch.stop              # 恢复（等 watchdog 或手动跑 boot 脚本）
```

## 部署与验证记录（2026-10-21 23:58，DEPLOYED）

- 部署：写入两个脚本 + 模板；注册两个计划任务（Ready）；启动 watcher（pid=57952）；心跳 0.1 分钟。
- 自检（synthetic）：1280×720 弹窗帧 score=0 → `SELFTEST=POPUP`；1920×1080 弹窗帧 score=11.57（阈值 25）→ `SELFTEST=POPUP`。
- 实时 probe（当时在跑的两台）：port 17028 score=79.21、port 17280 score=61.68，均 NORMAL（无误点）。
- 幂等：连续跑两次 boot 脚本，第二次输出 `watcher already running pid=57952 ... skip`，只有一个 watcher 进程。
- 前身验证（本地 Python 版，同模板同坐标）在真实教程弹窗上命中过：22:56:35「通宝」、22:59:00「異境」，截图在本地 `_tmp_lane_az/mon/events/`。

## 已知边界 / 扩展

- **只认「✕ 閉じる」这个样式/位置**（1280×720 基准在左上角 (95,85)-(215,135)）。以后遇到别的位置/样式的教程弹窗，
  截一张弹窗图，按同基准裁出按钮区域替换 `runner\tutorial_tpl_close.png` 即可（阈值一般沿用 10）。
- 只监视**正在跑肉鸽 maa** 的设备（日常/剿灭不进肉鸽，不会出这种教程）。
- 只关"左上角叉"；其它确认/OK 型弹窗不归它管。
- 如果将来把关闭逻辑并进 Go runner（每车道一个看护线程），可以退役这个 PS 服务；在那之前先用它兜底。
