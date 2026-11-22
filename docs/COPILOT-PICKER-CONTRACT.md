# copilot-picker 契约（Lead / core / frontend 共用）

目标：本机浏览器（网页 UI）→ 机队（Go 服务）→ MAA 全自动抄作业。
用户流程：粘贴 MAA 干员识别结果 → 选关卡 → 搜作业 → 开始 → 失败自动换下一份作业。

## 分工与写域

- Lead：`cmd/copilot-picker/`、`internal/copilotpick/`（config/accounts/server/SSE/executor）、`docs/`、部署脚本。
- core：`internal/copilotpick/ext/`（box/stage/prts/match + 测试）、`internal/copilotpick/ext/testdata/`。
- frontend：`web/`（React + TS + Vite + 手写 CSS；`web/dist` 构建产物由 `web/embed.go` 嵌入 Go）。
- 共享类型：`internal/copilotpick/model/model.go`，**只由 Lead 改**；core/frontend 按它对齐 JSON。

## HTTP API（服务端只监听 127.0.0.1，浏览器经 SSH 隧道访问）

所有响应 `Content-Type: application/json`，错误统一 `{"error":"中文信息"}` + 4xx/5xx。

1. `GET /api/health` → `model.Health`
2. `GET /api/accounts` → `{"accounts": [model.Account], "maaReady": bool, "maaError": string}`
   - 读机队 `runner/accounts.json`，按 emu 解析 adb（优先 `vm_config.json` 的 `port_forward.adb.host_port`，兜底端口公式）。
   - `online` 来自机队 `adb devices` 一次扫描。
3. `POST /api/box` `{"text":"<粘贴内容>"}` → `model.Roster`
   - 支持：MAA 干员识别「剪贴板」JSON 数组（PascalCase：`Name/Rarity/Elite/Level/Own/Potential/Id`）、MaaCore OperBox 回调 JSON（`own_opers`）、MAA 导出的 Markdown/CSV 表格、纯干员名列表（每行一个）。
4. `POST /api/stages/resolve` `{"query":"TL-9"}` → `{"stages":[model.Stage]}`
   - 先查 MAA 资源 `stages.json`（code 精确/包含匹配 → stageId），再用作业站 `GET /copilot/query?document=<query>` 从返回作业的 `content.stage_name` 里补全；按 stageName 去重，最多 12 个，标题取作业 doc.title。
   - `GET /api/stages/catalog` → `{"groups":[{"name":"TL 活动","stages":["TL-1",…]}]}`：把 MAA `stages.json` 按「活动 / 主线第 X 章 / 资源关 / 其它」分组，给页面做快捷关卡按钮（页面另存自定义分组在 localStorage，不经过服务端）。
5. `POST /api/jobs/search` `{"query"|"stageCode":"TL-9","stageName":"","roster":[...]|null,"rosterText":"...","limit":8}`
   - 服务端解析关卡（同 4），再按 `document=<关卡显示名>` 翻页拉作业（`order_by=views&desc=true`，最多 6 页 ×50），只保留 `content.stage_name == 目标 stageName`；
   - 用 box/matcher 过滤：缺干员 → `blocked`（不进 jobs）；练度不足降权；`jobs` 按「练度满足 > 评分 > 观看 > 稀有度总和 > 干员少」排序取前 limit。
   - 返回 `model.JobSearch`。
6. `POST /api/run/start` `model.RunOptions` → `{"runId":"..."}`
   - 支持半自动多关队列：`stages: [{stageCode, stageName, jobIds, raidJobIds?}]`，按数组顺序逐关执行；
     `stages` 为空时用 `stageCode/stageName/jobIds` 补成单关（旧调用兼容）。
   - 每关都需要 `stageName` 与至少一份作业；`raidJobIds` 是 difficulty=3 的作业子集，
     执行时 `copilot_list.is_raid=true`（MAA 进关后切突袭）；**手动模式只允许单关**。
   - `maxAttempts` 是每关的尝试上限，服务端按该关作业数截断。
7. 执行细节（2026-11-21 实测补）：
   - 作业站 `difficulty`：**1=普通，2=突袭，3=磨难，0=未标注**（早先按 3=突袭是错的）。
   - `manualMode` 只适合人工停在关卡详情页：执行器**不 startup、不导航**，直接单文件 copilot；
     助战（`support_unit_usage=1`）也只有单文件路径能用（maa-cli 任务文件不支持该字段）。
   - 作业站访问直连（Go 客户端对 maa.plus 不走 `HTTP(S)_PROXY`）；给 maa-cli 的子进程环境
     也去掉了代理变量（本机 Clash 会让 TLS 握手超时）。
8. 导航失败快停：作业集模式下如果 MAA 在 `Copilot@FullStageNavigation` 连续超限且没进过编队，
   判为「自动导航找不到关」（活动 EX 关不在主章节列表里），本关直接失败并提示用手动模式，
   不再把剩余作业挨个滑一遍。
7. `GET /api/run/{id}` → `model.RunSnapshot`（`events` 给最近 200 条；`stageIndex/stageTotal` 表示当前第几关/共几关）
8. `GET /api/run/{id}/events` → SSE：`data: <model.RunEvent>\n\n`，结束发 `event: done`
9. `POST /api/run/{id}/stop` → `{"ok":true}`
10. `GET /api/run/{id}/shot?ts=<unixnano>` → image/png（执行中的最新截图）
11. `POST /api/setup` → 初始化隔离 MAA 运行目录（junctions：`maa_data/lib`→MAA GUI 目录、`maa_data/resource`→MAA GUI 的 resource），返回 `{"ok":true,"maaReady":true}`
12. `POST /api/box/maa` `{"dir"?, "accountId"?}` → `model.Roster`
    - 读 MAA 桌面端「干员识别」/「更新数据」落盘的 `<MAA目录>\data\OperBoxData.json`；
    - 不传 `dir` 时按 `accountId` 的 emu 找同级 `MAA_<emu>`（MAA-Multi 布局），回退 `maaCoreSource`；
    - 注意 maa-cli 的 `OperBox` 任务只发回调、不落盘，所以扫描动作要在 MAA 桌面端做一次。
13. `POST /api/box/maa/scan` `{"accountId":"..."}` → `{"roster":model.Roster,"path":"..."}`
    - 用 maa-cli 跑 `startup` + `run operbox`，再从 `asst.log` 的 `OperBoxInfo` 回调抓结果并落盘；
    - 耗时约 1–2 分钟；同一模拟器不能被其它 MAA 驱动，否则会识别到 0 人。
14. `GET /api/log/maa` → `text/plain; charset=utf-8`（`asst.log` 最后 256KB，供页面「MAA 日志」弹层轮询）

## 前端要点

- 单页、深色科技风；四段式：①账号+粘贴干员 ②关卡解析/选择 ③作业列表+「加入队列」/开始 ④执行队列（半自动：人工选关+选作业，按顺序逐关执行）→ 右侧执行面板（状态时间线、SSE 日志、实时截图、停止）。
- 干员粘贴框旁提供「读取剪贴板」按钮（`navigator.clipboard.readText()`，失败给提示）。
- 作业卡片显示：ID/标题/评分/观看/难度（普通/突袭/未标注）/干员列表/缺失标红/练度不足标黄；默认勾选前 N 份，可调顺序（上移/下移/取消），开始按顺序尝试。
- 执行队列：03 里点「加入队列」把当前关卡+勾选作业追加到队列并清空勾选；队列里可上移/下移/移出/清空。队列非空时开始按钮变成「按队列执行（N 关）」；队列为空则按当前关卡直接单关执行。
- 执行面板：状态徽标（排队/启动游戏/导航/第 i/N 份/成功/失败/已停止），日志自动滚动，截图 2s 刷新，失败事件显式说明「换下一份作业」；多关时显示「第几关/共几关」。
- 设置抽屉：客户端（默认取账号）、自动编队/补信赖/忽略练度要求、最大尝试数、超时、**手动模式**（跳过导航，用户自己在关卡编队页；只支持单关）。
- 不要引入 UI 组件库/图标库；React 19 + 手写 CSS + inline SVG。构建命令 `pnpm build`（仓库根 `web/`）。

## 执行器（Lead 实现，供参考）

`maa.exe` 白名单子命令，全部带隔离环境变量：
`MAA_CONFIG_DIR/MAA_DATA_DIR/MAA_CACHE_DIR/MAA_STATE_DIR` → `data_copilot/`。
执行入口由配置 `copilotMode` 决定，默认 **joblist**：

1. `maa startup <client> -p <profile> --batch --no-summary`（超时 240s）
2. **导航（joblist 模式）**：执行器不自己导航。每份作业 normalize 后写成本地文件，
   再生成 `MAA_CONFIG_DIR/tasks/cp-<runId>.json`（`type=Copilot`，`copilot_list` 只有一条），
   然后 `maa run cp-<runId> -p <profile> --batch --no-summary`，由 MaaCore 的
   `MultiCopilotTaskPlugin` 自己 `navigate_to_stage`（含活动图、突袭切换）。
   - 2026-11-21 本地实测（maa-cli 0.7.5 + MaaCore 6.12.2）：`maa copilot <单个 URI/文件>`
     走单作业分支**不导航**；只有 `copilot_list` 进多作业分支。单条 `copilot_list`
     同样触发导航，所以「一份作业」也能白拿 MAA 的导航。
   - 旧入口 `copilotMode=uri|file` 保留：`maa copilot` 单作业 + 外部 fight-kill 抢导航
     （后台 `maa fight --times 1`，tail `asst.log` 命中关卡详情后杀进程；见
     `docs/COPILOT-PICKER-NAV-LAB.md`）。
3. 每份作业：`GET /copilot/get/<id>` 落盘 → 见第 2 步的 `maa run`；超时=TimeoutMin。
4. 成功 = 结尾有 `TaskChainCompleted` 且日志有战斗结算证据（StageDrops / BattleFinish 等），
   防「假成功」；失败记入错误、自动下一份；全部失败 → `failed`。
5. 每份作业前截一次 adb 图存到 run 目录（证据），失败原因带最后 20 行 MAA 输出。
6. MAA 的作业集「没三星就中止整链」，不会自己换作业——**换作业重试必须放在外层循环**，
   不要把多份候选作业塞进同一个 `copilot_list`（成功会继续打下一份，白花理智）。
7. 多关队列（半自动）：外层按 `stages` 顺序逐关跑，每关内按 `jobIds` 顺序尝试；
   一关全部失败不会中断队列，继续下一关，最终 `failed` 时错误里列出失败关卡。
   关与关之间、同关换作业之前都会先 `maa startup` 回主界面，避免 MAA 从结算页找关。

## 名册来源与匹配口径（2026-11-20 补充）

名册（`POST /api/box` 的 `text`）现在四种来源都认，按内容自动识别：

| 来源 | 说明 | 技能/模组 |
|---|---|---|
| `data/OperBoxData.json` | MAA 小工具「干员识别」的落盘文件，**首选**：机队每台 MAA 的 data 目录下都有，不用手工粘贴 | `source=local` 时没有 |
| 干员识别「剪贴板」JSON | PascalCase 数组（Name/Rarity/Elite/Level/Own/Potential/Id） | 没有 |
| Markdown/CSV 表格 | 有表头按列名取；无表头按「名 星 精英 等级」猜 | 有技能列才算有 |
| 纯干员名清单 | 每行一个 | 没有 |

匹配口径（复刻作业站前端 `models/operatorMatcher.ts`，实现见 `internal/copilotpick/ext/match.go`）：

- 练度：折算进度分再比较（`E2 1 级` > `E1 80 级`），不是逐项死比。
- 作业没写练度（v1 老作业）：按星级补保守默认要求（六星精二 60 专三、四/五星精一 60 技能 7、
  三星精一 55、一二星精零 30），用 `impliedMissing` 计数。
- **名册没有技能数据时（`skillKnown=false`），技能与模组按「已拥有」处理**，并置
  `requirementFuzz=true`——数据缺失不能把账号判成满屏「需补练度」。
- 分档：>13 人或缺 ≥2 人 → `blocked`；缺 1 人 → `borrow`（同时有人练度不足则 `train`）；
  全齐 → `ready`；仅 1 人练度不足且 <13 人 → `borrow`（助战能顶掉那一个）。
- 排序：`ready` 且评分 ≥9 一档，`borrow`/低分 `ready` 一档，`train`、`blocked` 依次；
  档内评分 → 浏览量 → 出场人数（少者优先）。

执行侧：

- 每份作业下载后先过 `ext.NormalizeJobContent`：技能序号与星级不匹配降 0、专精/模组要求把精英
  下限抬到 2、`Click` 缺 `rect`/`location` 与 `Swipe` 缺端点的动作丢弃。MAA 桌面端会自己修这些，
  `maa copilot` 直跑不会——不修就是 `CopilotConfig parse failed` / 「读取文件失败」。
- 普通/突袭写成几条见 `ext.EntryVariants`：15+ 章 `#s` 只写 `IsRaid=true`，磨难/简单
  （`easy_`/`tough_`）只写 `IsRaid=false`，其余按 `difficulty` 位标志展开。

## 2026-11-21 夜补丁（执行与判定，真机踩出来的）

1. **真假成功判定**（原 bug：日志里的 `StageDrops` 每次启动都有，来自 `asst::StageDropsConfig::parse`，
   等于没有证据，0 星也被记「完成」）：
   - 日志出现 `FightMissionFailed`（MAA 识别到「任务失败」页）→ 直接判作战失败；
   - 战斗后截 `settle.png`：活动主题的**三星徽章**亮像素 ≥ 3000，或通用**蓝星** ≥ 300 → 通关；
   - 护栏：全黑过场帧（mean < 18）与亮屏（mean > 100，例如活动地图）都不下结论，最多 12 次 × 1.5s。
2. **作业修正新增「去掉模组要求」**：本机账号多数没解锁作业要的模组，MAA 会卡在
   `BattleQuickFormationModule-N` 让整份作业编队失败；现在把 `requirements.module` 置 -1 并记一条事件。
3. **作业集导航看门狗**：活动 EX/S 关不在 MAA 主章节列表里，`navigate_to_stage` 会
   `ChapterSwipeToTheRightAndPlot` / `StageNavigationSlowlySwipeLeft` 死循环（实测一次卡到 20 分钟超时）。
   看门狗 15s 采样，累计 ≥ 8 次就杀掉 maa 并判「导航找不到该关」。EX/S 关改用
   `tools/picker_push/push_stage.py`（手动定位 + 逐作业重试）。
4. **运行环境故障中止**：起任务前先 adb 截图预检；maa 退出码 3221225794（0xC0000142）记为环境故障
   并中止整轮（模拟器崩时不会再拿一串作业挨个撞）。
5. **名册落盘**：`<root>/roster.json`，重启自动恢复（之前每次重启都要重扫 1~2 分钟干员识别）。

## 2026-11-22 补：`onMap` 模式（活动图专用）与两处判定修复

活动图（主线/EX/S 三张图）**不在 MAA 的主章节列表里**：从主界面起导航，MaaCore 会在章节
列表里 `ChapterSwipeToTheRightAndPlot` 死循环（picker 的看门狗会掐掉）。但只要游戏**已经停在
活动地图上**，MultiCopilotTaskPlugin 的 `find_stage` 会直接 OCR 当前屏认出 `OS-EX-3` 这类
关卡并点进去，突袭/磨难也按 `is_raid` 自己切（2026-11-22 日志 `find_stage stage results` 实测）。

用法：`POST /api/run/start` 带 `"onMap": true`（同时 `manualMode=false`）：

- 执行器**不做** `ensureGame` / `maa startup`，直接把作业包成 copilot_list 走 `maa run cp-*`；
- 只支持单关（多关时中间页不可控）；
- 关与关之间由外部驱动（`tools/picker_push/push_event.py`）负责把游戏带回活动图。

两处判定修复（都是这条路踩出来的）：

1. **`FightMissionFailed` 假阳性**：MAA 轮询 `Copilot@WaitUntilEndOfAction` 时会把
   `Copilot@FightMissionFailed` 列进 `to_be_recognized` 候选，日志里**每场战斗**都带着这串字。
   原来 `strings.Contains(logText,"FightMissionFailed")` 把每一场作业集战斗都判成 0 星
   （OS-4 白打一遍）。现在只认真正命中任务的 `"task":"FightMissionFailed"` /
   `"task":"Copilot@FightMissionFailed"` / `match_templ | FightMissionFailed`。
2. **onMap 下不能再用结算截图判星**：MAA 打完会自己点掉结算页回到关卡图，`verifySettlement`
   再截图只剩亮屏地图，永远「没截到结算页」。onMap 模式改判：**Copilot 链
   `TaskChainCompleted` 且无 `TaskChainError` = 通关**（0 星会命中 FightMissionFailed 报
   `TaskChainError`）；台账直接记 3 星（`recordProgress(trust=true)`）。
   手动模式仍以结算页截图数星为准。

