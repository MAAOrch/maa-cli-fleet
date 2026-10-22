# 日常完成凭证（大厅截图 + 信息面板）标准流程

> 2026-10-03 立 · 首张实测 `官-4 示例账号 16:03`（AUTO-MAS / MAA_1）
> 状态：**IMPLEMENTED + TESTED**（当日 21 轮出图，含 1 例切号失败无图，判据见 §3）
> 脚本：`tools/patch_screenshot_plugin.py` · `tools/shots_pipeline.py` · `tools/shot_annotate.py` · `tools/shots_contact_sheet.py` · `tools/switch_audit.py`

## 0. 目标与产物

每轮日常跑完，在**主界面（大厅）**自动截一张图，并在图上叠信息面板：

1. **截图时间**
2. **账号用户名**
3. **手机号 + 各任务的完成情况与时长**（+ 服务器 / 模拟器 / 来源 / 合计 / 理智）

产物落两处（同名）：

```
<项目>\shots\<台>_<账号>_<YYYYMMDD-HHMMSS>.png      ← 主目录（归档）
%USERPROFILE%\Desktop\maa-shots\...                  ← 桌面副本（给人看）
```

## 1. 链路（4 段）

```
① 拍    MAA ScreenshotTaskPlugin：在「领取奖励(Award)」链启动时截图
         → <MAA槽位>\debug\Award\<时间戳>_raw.png
② 取数  gui.log 的「切号 -->> <掩码>」+ 任务链事件；在线用户表取账号/手机号
③ 画    shot_annotate.py（Pillow）：底部叠面板（横屏走两栏，面板 ≤62% 高）
④ 归档  shots\ + 桌面；可选拼图 shots_contact_sheet.py
```

## 2. 步骤

### 2.1 打补丁（每个 MAA 槽位一次）

```bash
python tools/patch_screenshot_plugin.py <MAA-Multi>/MAA_6/resource/tasks/tasks.json Award
```

- **原理**：MAA 6.18 自带 `ScreenshotTaskPlugin`；把**任务名**加进 `ScreenshotTaskPlugin-Config.next`，
  插件在该任务**开始时**截图（源码 `verify()`：`task.ends_with(item)`）
- **落盘**：`save_img(utils::path("debug") / m_task_chain)` → `debug\<任务链名>\<时间戳>_raw.png`
- 日常链最后一步是「领取奖励」→ 链名 `Award` → 所以盯 `debug\Award\*.png`
- **要打全**：MAS 一共用 12 个 MAA 槽位（`MAA_1`..`MAA_12`，一个槽位服务多个脚本）→ 12 个全打，MAS 跑的任何日常都会出图
  （2026-10-03 只先打了 4 个共享槽位 `MAA_6/MAA_2/MAA_10/MAA_1`，当晚补全其余 8 个：`MAA_3/4/5/7/8/9/11/12`）
- 批量补丁（主机侧一行循环）：
  ```bash
  for S in MAA_1 MAA_2 MAA_3 MAA_4 MAA_5 MAA_6 MAA_7 MAA_8 MAA_9 MAA_10 MAA_11 MAA_12; do
    python tools/patch_screenshot_plugin.py <MAA-Multi>/$S/resource/tasks/tasks.json Award
  done
  ```
- ⚠️ 补丁必须在 **MAA 进程启动之前**写好（同一轮里已启动的 MAA 不会加载）——首轮没图就是这个原因
- ⚠️ MAA 升级会覆盖 `tasks.json` → 用 MAAOrch 的 `mas_patch_guard` 做持久化

### 2.2 出成品（逐轮 / 常驻看门）

```bash
python tools/shots_pipeline.py --backfill                # 把已有 raw 图补齐成品 + 清黑帧
python tools/shots_pipeline.py --watch --minutes 300     # 常驻：新图一出现就出成品并推到桌面
```

关键口径：

- **同轮聚类**：`Award` 目录里时间相邻 ≤120s 的图算一轮；**只取体积最大的那张**
  （一轮常连拍 2–3 张：第一张是大厅，后两张是转场黑帧，约 550KB vs 1.5MB）
- **账号归属（2026-10-03 二次修正，务必按这个来）**：以 **MAS 自己的 history 记录**为准 ——
  `<AUTO-MAS>\history\<日期>\<账号名>\<脚本>-<时间>.log`，该文件 mtime ≈ 本轮结束时刻，
  截图发生在「领取奖励」（结束前 1~2 分钟）→ 用 `[截图时刻-2min, +15min]` 窗口锚定到账号/脚本。
  **再加一道槽位约束**：账号所属脚本的 MAA 槽位必须等于截图所在槽位，否则不认。
  ⚠️ 不要用「`gui.log` 的切号掩码」当唯一依据：B 服/台服的账号 ID 是用户名/邮箱（不是手机号），
  掩码会全部塌成空串互相覆盖 —— 实测把 MAA_9（B服槽位）的 7 张图全标成了台服的 `示例账号C`；
  官服有掩码尚可，但也必须配槽位约束。
- **任务段**：只取「**最后一个开始唤醒**」起到截图时刻的那一轮；
  链事件配对用 **LIFO**（被中途停掉的轮次会留下未配对的 start，FIFO 会串到上一轮）
- 成品 <600KB 的判为黑帧版；**raw <900KB 的一组直接跳过**（只有转场黑帧时不写、也不动已有成品）
- ⚠️ 重出同一天同一账号时，只在**新成品合格**的前提下才清旧件 —— 否则会把好图删成只剩黑帧版（2026-10-03 踩过：官-1 `某账号` 16:06 那张就这么丢的，只剩拼图里的缩略图）

### 2.3 单张手动出（或吃机队老格式）

```bash
python tools/shot_annotate.py --shot <raw.png> --json <info.json> --out <out.png>
python tools/shot_annotate.py --shot <raw.png> --from-out <daily_aXX_*.out> --name 示例账号A --phone <PHONE> --server Official --emu 49 --out out.png
```

`info.json` 契约：

```json
{ "ts": "2026-10-03 16:03:21", "account": "示例账号", "account_id": "a05",
  "phone": "<PHONE>", "server": "Official", "emu": "11",
  "source": "AUTO-MAS 官-4（emu 11 / MAA_1）",
  "tasks": [ {"name": "开始唤醒", "start": "15:22:58", "end": "15:24:55", "dur": "1m57s", "status": "Completed"} ],
  "sanity_cur": 3, "sanity_max": 210 }
```

- `--mask-phone` 可把手机号打成 `130****9165`（对外发布建议开）
- 竖屏（手机 900×1600）自动放大字号；横屏（MAA 帧 1280×720）自动两栏

### 2.4 拼图（一次看全部）

```bash
python tools/shots_contact_sheet.py     # → shots\_拼图_YYYYMMDD_HHMM.png + 桌面副本
```

4 列网格，每格缩略 620px + 标题条 `台-账号-HH:MM`；排序 `官-4 → 官-2 → 官-3 → 官-1`、同台按时间。

## 3. 验收（每次上线必做两条）

1. **OCR 回读**：对成品底部面板做 OCR，确认 时间 / 账号 / 手机号 / 任务行 / 合计 都在（本项目用 `rapidocr_onnxruntime`）
2. **切号对账**：

```bash
python tools/switch_audit.py     # 配置里的启用账号顺序 vs 实际切号日志 vs 大厅图张数
```

输出三个信号：`config order` / `real switches` / `lobby shots`，并给出
`NEVER SWITCHED TO (suspect)` 与 `SWITCHED TO SOMETHING NOT IN LIST`。

**判读口径（重要）**：

- 有切号行 + 有图 = 正常
- **没切号行 + 没图** = 切号失败（安全失败：MAA 在账号管理里找不到该号会报错退出，**不会**在别人号上跑）
- 切号行与实际奖励/时长对不上 = 真·串号（尚未见过，出现要立刻停台排查）

2026-10-03 实测：官-1 的 `示例账号B`（171\*\*\*\*9454）**没切过也没图** → 判为切号失败（该号不在 emu 13 的登录记录里）；
官-2/官-3/官-4 全对齐；官-3 的 `示例账号D`、`示例账号A` 是 `Status=false`（停用），跳过属预期。

## 4. 已知坑（都踩过）

| 现象 | 原因 | 处置 |
|---|---|---|
| 一轮 2–3 张、后面全黑 | Award 链里多个子任务都命中 `ends_with("Award")` | 只取体积最大那张 |
| 账号名错位一轮 | 用「当前配置」当名字 | 用切号日志 + 时间窗 |
| 任务段串到上一轮 | 中途停轮留下未配对的 start | 链事件配对用 LIFO + 只取最后一个 StartUp |
| `UnicodeEncodeError` | 主机控制台是 GBK，脚本 print 中文/生僻字（如 `૮ต蓝熹ตա`） | 脚本一律 ASCII 输出（`ensure_ascii=True` 或 `encode('ascii','replace')`） |
| `cv2.imread` 读不到图 | 用了 MSYS 的 `/tmp/...` 路径 | 用 Windows 路径（`os.environ['TEMP']` 拼接） |
| 截出来的 PNG 打不开 | PowerShell `>` 重定向把二进制写成 UTF-16 | 用 `cmd /c "adb ... > file.png"` 或 `[IO.File]::WriteAllBytes` |
| 有 raw 图但没成品 | 看门脚本窗口到了（`--minutes`）/ 账号是刚建的新号 | 重跑 `--backfill`；账号表走在线接口 |
| 好图被重出流程删掉 | 重出时无条件清同轮旧件，而本轮只剩黑帧 | raw<900KB 跳过 + 成品不合格不写不删（已修） |
| 某一轮完全没有 raw 图 | MAA 自带的 debug 图片自动清理（`filenum_ctrl`）会把旧图删掉 | 出图后尽快归档（看门默认 30s 一轮），别等第二天回填 |
| 大厅图里读不到昵称 | 方舟主界面不显示玩家名 | 身份判据只能靠切号日志（+ 登录页 OCR 掩码） |

## 5. 回滚

```bash
# 关掉截图（保留历史图）
python tools/patch_screenshot_plugin.py <槽位>/resource/tasks/tasks.json --remove Award   # 或恢复 tasks.json.bak-pi-*
# 停看门
taskkill /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq *shots_pipeline*"              # 或按 pid
# 清归档
rmdir /s /q <项目>\shots  &  rmdir /s /q %USERPROFILE%\Desktop\maa-shots
```

（`patch_screenshot_plugin.py` 每次写入前都会留 `tasks.json.bak-pi-<时间戳>` 备份。）

## 6. 证据（2026-10-03）

| 项 | 值 |
|---|---|
| 首张 raw | `<MAA-Multi>\MAA_1\debug\Award\2026.10.03-16.03.21.798_raw.png` |
| 首张成品 | `官-4_示例账号_20261003-160321.png`（大厅 OCR：`终端 / 签到活动 / 理智/210 / 编队`） |
| 当日出图 | **21 轮**（官-1×6、官-2×6、官-3×6、官-4×5；其中官-1 的 `示例账号B` 轮切号失败无图） |
| 拼图 | `_拼图_20261003_2013.png`（2520×2394，22 格） |
| 面板回读 | OCR 实读：`账号：示例账号 手机号：<PHONE> 截图时间：2026-10-03 16:03:21` + 7 行任务 |
