# Web GUI 验收报告（copilot-picker，2026-11-23）

会话 B 隔离环境：`D:\maa-cli-fleet-b`（worktree）/ `D:\copilot-picker-local-2` / `127.0.0.1:8791`。
验收期间**没有触发任何消耗理智的真战斗**：作业搜索走到了「加入队列 / 参数校验 / 状态查询 / 停止（空跑）」为止。

## 结论

`web/` 这份前端之前确实「代码齐全但从没实测过」：首屏能渲染、接口基本对得上，但有 5 个实际 Bug（见下），已全部修复并重编进 `copilot-picker.exe`。
现在打开 <http://127.0.0.1:8791/> 可以进行：选号 → 名册（读/扫）→ 解析关卡 → 搜作业（普通/突袭过滤）→ 勾选/排序 → 加入队列/开始执行 → 看执行状态、SSE 日志与 asst.log。

## 修掉的 Bug

| # | 现象 | 根因 | 修复 |
|---|------|------|------|
| 1 | 没有 `asst.log` 时执行面板一直显示「读取失败，2 秒后重试」，控制台每 2 秒一个 404 | 后端 `GET /api/log/maa` 对「日志尚未生成」返回 404（语义正确），前端把它当错误 | `web/src/App.tsx`：404 单独识别成「等待日志生成」空态并给出解释，不再显示成错误 |
| 2 | 作业页没有难度过滤，普通/突袭/磨难混排 | 前端从未调用后端已有的 `mode` 参数 | `web/src/types.ts` / `App.tsx` / `components/JobsStep.tsx`：新增「全部/普通/突袭」筛选，切难度自动按当前关卡重搜；状态随 ui-state 持久化 |
| 3 | 加了过滤后仍然滤不干净：OS-S-3 的 8 份里「磨难」和「普通」混在一起 | 后端 `filterJobsByMode` 只看标题关键词（含“突袭”/“仅普通”），而「磨难」作业标题里没有“突袭”二字 | `internal/copilotpick/search.go`：改为按作业站 `difficulty` 数字过滤（1 普通 / 2 突袭 / 3 磨难），只有 `0=未标注` 才退回标题猜；新增单测 `TestFilterJobsByModeUsesDifficulty` |
| 4 | 双开时点「读 MAA 识别」可能导入**别的账号**的名册 | 读取优先级是外部 MAA 目录 `data/OperBoxData.json`（不按实例区分），本运行根扫出来的 `scan_OperBoxData.json` 反而不读 | `internal/copilotpick/server.go`：优先读本运行根扫描落盘，外部目录只作兜底 |
| 5 | S 关（磨难）作业进队列后不会按突袭执行 | 前端 `raidJobIds` 只认 `difficulty===2`，S 关高难是 `difficulty===3` | `web/src/App.tsx`：新增 `isRaidJob()`（2 或 3 都算突袭）并在 4 处入队/执行逻辑统一使用 |
| 6 | 前端一条 404/断连提示写死 `127.0.0.1:8787`（示例端口，和 8791 对不上） | 文案硬编码 | `web/src/api.ts`：改成不写端口的通用提示 |

另：补了验收清单要求的**推关台账**展示（后端 `/api/progress` 早就有，前端没接）：
- 新组件 `web/src/components/ProgressPanel.tsx` + 右栏 StepCard「05 推关台账」；
- 空台账显示友好空态，执行结束（SSE done）或点刷新时重读 `progress.json`。

## 页面/接口验收明细（干净首屏 + 逐项操作）

| 项 | 结果 |
|---|---|
| 首页加载 | 200，React 正常挂载；无 JS 异常、无网络失败；静态资源 `text/html` / `text/javascript` / `text/css` 正确 |
| 服务/账号状态 | `local1 · Official · emu1 · 在线 127.0.0.1:16416`；页面上无 `local0`/`16384`（不串实例 0） |
| 名册空态 | 首屏显示「未解析干员时仍可只按热度搜作业…」，不白屏 |
| 读 MAA 识别 | 点按钮 → `已读取 MAA 干员识别：403 人（source=local）`（来自本运行根扫描落盘，不再串号） |
| 扫描干员（只读链路） | 点按钮真实跑了一次 `maa startup + OperBox`（132 秒），页面显示「扫描中…」→ 403 名干员 / 117 六星 / 192 五星，`source=local` 提示清楚；只进干员页，无战斗、无理智消耗 |
| 解析关卡 | 输入 `OS-S-3` → `act46side_s03`，解析后自动选中，当前关卡 chip 正常 |
| 搜索作业（全部） | 候选 8 份（作业站约 30，另 10 份练度不足、9 份缺干员），字段/计数/卡片/排序序号都对 |
| 难度过滤 | 普通 = 4 份（3 普通 + 1 未标注），突袭 = 8 份（全部磨难）；「全部」= 8；切换会重搜并清空旧勾选 |
| 加入队列 | 队列面板出现 `OS-S-3 → act46side_s03 · 3 份作业`，`raidJobIds` 正确包含磨难作业 `75704` |
| 创建任务参数校验（未开打） | 空请求 → `400 {"error":"账号 不存在"}`；用实例 0 账号 `local0b` → `400 {"error":"账号 local0b 不存在"}`（隔离有效） |
| 状态轮询/停止 | `GET /api/run/active` → `{"run":null}`；`POST /api/run/stop` → `{"ok":true,"stopped":false}`；页面「停止」在没有任务时是 disabled |
| MAA 日志 | `GET /api/log/maa` 200（本次扫描生成的 asst.log），页面按 240 行滚动刷新，无报错 |
| 启动模拟器 | `POST /api/emu/start {accountId:local1}` → `{"already":true,"emu":"1","ok":true}`（实例 1 已在跑，幂等；没碰实例 0） |
| ui-state 持久化 | 刷新后恢复：账号 local1、名册 403、当前关卡 OS-S-3、难度=突袭、队列 1 关（`queue.in` 不参与） |
| SPA 回退 | 未知路径回 `index.html`，直接刷新任意路径不白屏 |

证据：`D:\maa-cli-fleet-b\_tmp\webcheck\` 下 `e2e-e2e.json`（完整步骤/API 记录）与 `e2e-S0-load.png`、`e2e-S5-filter.png`、`e2e-S6-queue.png`、`e2e-S8-restored.png`、`scan-final.png`。

## 没测/没动的东西

- **真战斗没跑**（按要求）：`POST /api/run/start` 只用非法参数验证了校验分支；SSE 事件流与截图轮询的「进行中」状态没有真实 run 可看。
- **「结束 MAA 进程」按钮没点**：`/api/maa/kill` 会按 profile 匹配杀 maa-cli，双开期间有误伤主会话进程的风险，验收阶段不碰。
- **nav / emu stop** 未点（会真导航或关实例）。
- `web/` 整个目录在仓库里是 **untracked**（主仓 `D:\maa-cli-fleet\web` 也一样，没进 git），所以前端修复目前只在 worktree 工作区；要入库需要决定是否把 `web/src` 首次提交进 `feat/slot2-test`。

## 涉及文件

- 前端：`web/src/{types.ts,api.ts,App.tsx,styles.css}`、`web/src/components/{JobsStep.tsx,ProgressPanel.tsx,QueuePanel.tsx}`；已 `npm run build` 重新生成 `web/dist`。
- 后端：`internal/copilotpick/{search.go,server.go,model/model.go,search_test.go}`（本报告同提交）。
- 产物：`D:\copilot-picker-local-2\copilot-picker.exe`（`slot2-dev-webgui5`，内嵌新 `web/dist`），监听 `127.0.0.1:8791`。
