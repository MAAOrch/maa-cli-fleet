# 执行面分层：控制面 / Executor / Resource / 人机面

> 2026-10-25 · 状态：**IMPLEMENTED（Phase 1 + 1.5：daily/ann 走 Executor；rogue 通过 ResidentExecutor 的 Start/Judge 接入，看护/轮换仍由车道驱动）** · 本文只写边界与契约，不含代码改动
> 缘起：fleet 要从 MAA 单一执行线延展到 MaaFramework / PI 资源；先把“谁执行、执行什么、怎么加新类型”定死，避免调度器里继续长 `switch`。
> 关联：`ARCHITECTURE-NODE-SELF.md`（机端自管铁律）、`RUNNER-UNIFIED-TASKS.md`（三类任务）、`RELEASE-STANDARD.md`（上线门槛）、`NODE-BOUNDARY-CONVERGENCE.md`（策略/执行边界）。

---

## 0. 一句话

fleet 不自己“会”玩游戏：它把任务交给**按类型注册的 executor**；executor 按 **resource** 描述的步骤执行；**MXU 只做人机调试**。
新增任务类型 = 新增 executor + resource + 注册项，**不改调度主循环**。

---

## 1. 四层职责（逻辑角色，不等于四台机器）

| 层 | 角色 | 当前归谁 | 回答的问题 | 不做什么 |
|---|---|---|---|---|
| **控制面** | 队列 / 班表 / 并发 / 重试 / 台账 | 单机 = runner；多机策略未来可上移 center | 何时、哪台设备、哪个账号、什么类型、失败怎么办 | 不点游戏、不解析 pipeline |
| **执行面** | 按类型把一次任务跑完 | `internal/runner` 内的 executor + 注册表 | 怎么把这件事跑完 | 不做全局排班 |
| **资源面** | 步骤 / 识别 / 导航 | MAA：`config/tasks/*.toml`；PI：`interface.json` + `resource/pipeline/` | 具体识别什么、点哪里 | 不决定何时跑 |
| **人机面** | 调试 / 手跑 / 看画面 | MXU、MFAAvalonia、MaaDebugger | 给人看 | 不进生产链路 |

两条硬约束：

1. **控制面是逻辑角色，不等于 center 进程。** 单机形态由 runner 兼任；多机形态才把“策略”上移 center。设备与环境仍归机端，见 `ARCHITECTURE-NODE-SELF.md`。
2. **执行面在机端内部，不是把执行搬到中心。** center 仍然只收 `register` / `report`，不加指令通道。

---

## 2. 现有资产怎么落位

| 现有能力 | 代码 | 新角色 | 迁移方式 |
|---|---|---|---|
| 日常 / 剿灭排队 | `internal/runner/slot.go`（`startMaa` / `judgeOutFile`） | MAA executor（Kind=`daily` / `ann`） | Phase 1 包一层，行为不变 |
| 肉鸽 / 卫戍常驻车道 | `internal/runner/rogue.go`（`residentLoop`） | MAA resident executor（Kind=`rogue`）+ 车道看护 | Phase 1.5 已接入 `ResidentExecutor`：发车/判定走执行器，看护/轮换/暂停仍在车道 |
| 设备生命周期 | `internal/runner/device.go` | 执行面共用基础设施 | 不抽 executor |
| 资源闸门 | `internal/runner/resources.go` | 控制面 / 执行面边界 | 保持机端判断 |
| 任务台账 / 热改 | `internal/runner/queue.go` | 控制面 | 保持现有 `queue.in` |
| 中心台账 | `internal/center` | 控制面（多机视图） | 只收上报，不加指令 |
| 前瞻排班 / 班次网格 | `internal/runner/plan.go` / `roster.go` | 控制面（策略） | 仍按 `NODE-BOUNDARY-CONVERGENCE.md` 收敛 |
| MXU | 新引入 | 人机面 | 只开发 / 调试 / 手跑 |

> Phase 1 的目标不是“重写三类任务”，而是把**执行入口**收成一个契约，让第四类任务能插进来。

---

## 3. 任务类型契约

### 3.1 四个词

- **Task**：台账记录（谁、何时、什么类型、状态），对应 `runner/state.json` 里的 `Task`。
- **Job**：一次执行请求，交给 executor；此时设备已就绪。
- **Result**：执行结果 + 证据，回写台账并上报。
- **Executor**：只认一种（或一组）`Kind` 的执行器。

### 3.2 建议形状（Phase 1 定稿）

```go
type Executor interface {
    Name() string
    Run(ctx context.Context, job Job) (Result, error)
}

type Job struct {
    Key      string         // "daily:a07" / "pi.maaend.daily:a07"
    Kind     string         // daily | ann | pi.<project>.<entry>
    Account  model.Account
    Device   DeviceRef      // 模拟器 idx、adb 地址
    TaskFile string         // MAA 任务文件名；PI 任务可为空
    Params   map[string]any // PI 的 option 值；MAA 任务可为空
}

type Result struct {
    OK       bool
    Note     string
    OutFile  string
    CostSec  int
    Evidence []string       // 截图 / 日志路径
    Metrics  map[string]any // 理智、结算数等，按 Kind 解释
}
```

**分工写死**：

- runner 负责：`DeviceUp` / `DeviceDown`、常驻暂停与接回、资源闸门、重试、上报；
- executor 负责：拿到“设备已就绪”的 Job，按 resource 跑完一次，返回 Result；
- executor **不**自己起停模拟器、**不**改队列、**不**做全局排班。

### 3.3 命名与注册

- MAA 侧保留既有 `Kind`：`daily` / `ann` / `rogue`（常驻车道，含肉鸽 / 卫戍）。
- PI 侧用点分命名：`pi.<project>.<entry>`，例如 `pi.maaend.daily`、`pi.demo.nav`。
- `Task.Key` 仍是 `<kind>:<account>`，只在**第一个冒号**切分；`Kind` 内不允许冒号。
- 注册表：

```go
type Registry struct{ m map[string]Executor }

func (r *Registry) Register(kind string, e Executor)
func (r *Registry) For(kind string) (Executor, bool)
```

- `New()` 启动时注册 `daily` / `ann`；手工构造 `Runner`（测试 / 一次性命令）时惰性补齐，避免“没走 New”变成“daily 没有执行器”的事故。

- **未知 Kind 必须大声报错并停**（`RELEASE-STANDARD.md` G5），不许静默跳过。
- 新类型上线 = 注册一项 + 配一个 resource + 跑一次 L1 canary，不允许跳级。

### 3.4 一次执行的顺序

```
queue -> runner slot -> DeviceUp -> executor.Run -> Result -> 判定/记录 -> DeviceDown -> report
```

常驻型（肉鸽 / 卫戍 / 生息）由车道循环驱动：

```
residentLoop -> ResidentExecutor.Start（拿可 kill 句柄）-> 看护 -> 自然退出后 Judge
```

看护判据（进程退出 / `asst.log` 5 分钟不更新 / 设备连不起）和轮换/暂停仍在车道；executor 只负责发车与判定。

---

## 4. 新任务类型的 7 步流水线

| # | 步骤 | 产出 | 归属 | 验收 |
|---|---|---|---|---|
| 1 | **任务契约** | `kind`、参数、前置状态、成功判据、资源消耗 | 控制面 | 文档 + 注册项 |
| 2 | **实地考察** | 状态清单、锚点、截图、分辨率矩阵 | 资源 | 每个状态 ≥3 张截图，锚点全部命中 |
| 3 | **任务内编排** | pipeline JSON（`next` / `on_error` / `[JumpBack]` / `anchor` / `timeout`） | 资源 | MaaDebugger 单步通过 |
| 4 | **任务外接口** | `interface.json` 的 `task` / `option` / `preset` | 资源 | schema 校验 + MXU 可见 |
| 5 | **调试** | 调试记录、边界用例 | 资源 + 执行面 | MXU 手跑 + 异常路径通过 |
| 6 | **接执行器** | 子进程或 binding + 注册 | 执行面 | 无头跑通，失败可判、证据可查 |
| 7 | **接 fleet** | 调度策略、并发、重试、canary | 控制面 | L1 真结算 + 回滚演练 |

> 先做一条纵向切片（导航 + 一个业务任务），跑通再扩第二、第三个；不要一上来铺全套。

---

## 5. 两层编排，不要混

### 5.1 任务内（pipeline）

- 导航、清弹窗、回主界面、识别、点击；
- 正常路径用 `next`；
- 失败 / 超时用 `on_error`；
- 临时弹窗用 `[JumpBack]`（处理完自动回父节点）；
- 动态返回用 `anchor`；
- 用 `timeout` / `max_hit` 兜底，禁止无界递归。

### 5.2 任务间（fleet）

- 队列、班表、依赖、并发、资源闸门、重试、上报；
- “日常到点插队 → 暂停常驻 → 跑完接回”属于这一层；
- “几点跑、跑几次、哪台机跑”不写进 pipeline。

### 5.3 反模式

| 反模式 | 为什么不行 |
|---|---|
| 把“几点跑”写进 pipeline | pipeline 只描述一次任务内部流程，不持有全局时间 |
| 把“点哪个按钮”写进 fleet | 设备/分辨率/游戏版本一变，调度器就得跟着改 |
| 导航只做成独立 PI task，指望主任务自动接上 | 客户端只按列表顺序跑；自动衔接必须靠 `on_error` / `[JumpBack]` |
| 把 `pretask` 当任务前置 | PI 的 `pretask` 是 Controller 启动前执行外部程序，不是任务钩子 |
| 用 `sleep` 硬等动画 | 用 `pre_wait_freezes` / `post_wait_freezes` 或识别锚点 |

---

## 6. 实地考察清单

写 pipeline 之前，先把“现场”摸清；这张表直接决定后面返工次数。

| 项 | 要记什么 | 验收 |
|---|---|---|
| 状态清单 | 主界面、加载中、弹窗、断线、维护、登录失效、更新提示 | 每个状态有名字、有截图 |
| 锚点 | 稳定 OCR 文本 / 固定图标 / ROI；避开动画和随机内容 | 每个锚点在所有截图上命中 |
| 环境 | 分辨率、DPI、语言、服务器（官服 / B 服 / 日服 / 国际服） | 至少覆盖当前机队实际组合 |
| 时序 | 动画、加载、结算、网络重试 | 用等待冻结 / 识别代替 `sleep` |
| 失败路径 | 弹窗挡住、游戏崩溃、账号登出、模拟器端口漂移 | 每条都有 `on_error` 出口 |
| 归档 | 截图命名 `state_<name>_<res>.png`，记录在 `pi/<project>/docs/recon.md` | 下一个人能照着复现 |

---

## 7. 标准基线

| 面 | 采用 | 锁法 |
|---|---|---|
| Pipeline | MaaFramework 任务流水线协议 v5.x（当前文档到 v5.10） | 锁 MaaFramework tag |
| 界面 | Project Interface V2，`interface_version: 2`（当前文档到 v2.10.x） | `interface.json` + 官方 schema |
| 工程 | MaaPracticeBoilerplate 目录结构 | 记录 template commit |
| 校验 | `tools/interface.schema.json` | VSCode 关联 + CI 校验 |
| 客户端 | MXU = 开发 / 调试；MaaPiCli = 无头 / 生产；MFAAvalonia = 可选 | 版本写进项目 README |
| 升级 | 先读 changelog，再 diff schema，最后跑 smoke | 不追最新，锁 stable |

纪律：

- 核心资产只用标准 PI V2 + 标准 pipeline 字段；客户端扩展（例如 MXU 的行内 Markdown label）放 `import` 覆盖层，不混进主 `interface.json`；`input_type` / `setting` / `hotkey` 这类字段先查客户端支持矩阵。
- 不用废弃字段：`is_sub` / `interrupt` / 旧版 `Advanced` 配置。
- `entry` 名当公开 API：发布后尽量不改名；要改就新增 entry，旧 entry 保留一段时间。
- 客户端支持度要实测：例如 MaaPiCli 对 `setting` 只解析不渲染、`hotkey` 未实现，核心任务不要依赖这些。
- 版本锁 stable 组合，不用 beta 上生产；当前参考 MaaFramework `v5.14.x`、MXU `v2.8.x`、MaaPiCli 对应 release。

---

## 8. 与现有文档的关系

- **`ARCHITECTURE-NODE-SELF.md`**：设备、环境、执行仍归机端；executor 是机端内部的分层，不是把执行搬到 center。
- **`RUNNER-UNIFIED-TASKS.md`**：daily / ann 是排队型，rogue 是常驻型；executor 只替换“怎么跑”，不替换“什么时候跑、跑几次”。
- **`RELEASE-STANDARD.md`**：新 executor = 新 canary；未知 Kind 必须报错；回滚路径先想好。
- **`NODE-BOUNDARY-CONVERGENCE.md`**：`plan.go` / `roster.go` 仍是策略面，不因为抽了 executor 就变成执行面。

---

## 9. 演进路线

### Phase 0（本文，已完成）

- 定四层职责、任务契约、命名、标准基线、反模式；
- 不改代码。

### Phase 1：抽接口，不改变行为

- 新增 `Executor` + `Registry`；
- 把现有 `slot.go` 的“发车 + 判定”包成 Kind=`daily` / `ann` 的 MAA executor；
- 常驻车道（Kind=`rogue`）保持原路径：`residentLoop` 仍直接 `startMaa` + 看护，Phase 1.5 再抽；
- 未知 Kind 直接判失败（不重试、不动设备）；
- 验收：现有测试全绿；daily / ann canary 结果与改造前一致；rogue 车道行为不变。

### Phase 1.5：常驻车道接入 executor（已完成 2026-10-26）

- 新增 `ResidentExecutor` 扩展：`Start(job) (*maaRun, error)` + `Judge(outFile)`；
- `residentLoop` 优先从 `"rogue"` 注册项取执行器；未注册时回退 `startMaa` / `judgeOutFile`；
- 看护、轮换、暂停、日常插队仍由 `residentLoop` 负责，executor 只负责发车和判定；
- 验收：`go test ./internal/runner` 全绿；live canary 按 `CANARY-RECLAMATION-ROTATION.md`。

### Phase 2：PI 纵向切片

- 建 `pi/demo/`：`interface.json` + `Nav.ToHome` + 一个业务任务；
- 新增 `MaaPiCLIExecutor`，用 `MaaPiCli -d` 跑（参数以 `--help` 为准）；
- 一台真机、一个账号，跑 L1 canary；
- 记录：启动耗时、退出方式、日志路径、失败判定、是否需要配置文件模板。

### Phase 3：按需上 binding

- 只有 Phase 2 证明“子进程拿不到结构化状态”时，才引入 `maa-framework-go`；
- 目标：回调直接映射 `Result`，不再解析日志；
- 否则停在 Phase 2，避免多养一套 native 依赖。

---

## 10. 待拍板

1. **MaaPiCli 非交互传参**：`-d` 能否按任务名 / option 值直接跑？先做 spike，再定 Phase 2 形态。
2. **Executor 粒度**：一个 `MaaCLIExecutor` 注册多个 Kind，还是每个 Kind 一个实例？
3. **PI 目录**：仓库内 `pi/<project>/` 是否合适？还是独立仓库 / release 包？
4. **资源分发**：随 fleet 包、`fleet setup` 下载，还是手动放？
5. **Result.Metrics 范围**：理智、结算数、截图、耗时，哪些进台账？
6. **常驻 PI 任务**：Phase 2 先只做排队型，还是同时支持常驻？
7. **center 策略**：多机形态下，“何时跑”上移 center 的时机（不影响本文执行面契约）。

---

## 11. 决策记录

| 时间 | 项 | 结论 |
|---|---|---|
| 2026-10-25 | 分层 | 控制面 / 执行面 / 资源面 / 人机面 |
| 2026-10-25 | 执行面位置 | 机端内部；不改变机端自管铁律 |
| 2026-10-25 | PI 执行器 | 先 MaaPiCli 子进程；binding 后置 |
| 2026-10-25 | GUI | MXU 只做开发 / 调试，不进生产链路 |
| 2026-10-25 | 标准 | PI V2 + Pipeline v5.x + schema；核心不用客户端扩展 |
| 2026-10-25 | 新类型上线 | 注册项 + resource + L1 canary，禁止跳级 |
| 2026-10-26 | 常驻执行器 | `ResidentExecutor.Start/Judge`；看护/轮换/暂停留在车道 |
