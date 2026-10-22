# 机端边界收敛（NODE-BOUNDARY-CONVERGENCE）

> 2026-10-02 · 状态：**PARTIALLY IMPLEMENTED（方案保留）** · 本文只做盘点与方案
>
> 2026-10-22 现状：③（center 从机端二进制拆走）、⑤（机端专用布点包）、①（排班策略交回中心）、④（queue.in 收紧）
> 尚未落地；当前 `center` 仍与 runner 同一二进制，排班仍在机端。已落地的是 ② 的「gen 仅作临时 ETL」定位、
> 以及 10-02 的中心指令通道删除。本文保留为后续收敛依据。
> 缘起：用户定调 —— 「收敛机端：不属于它的职能，不能让它做。」
> 判定依据：`docs/ARCHITECTURE.md` §1 机端边界 + `docs/ARCHITECTURE-NODE-SELF.md` §1 分工 + `node/README.md` 边界。

---

## 1. 先摆冲突：两份文档对「排班」口径不一致

| 文档 | 条款原文 | 读出来的归属 |
|---|---|---|
| `docs/ARCHITECTURE.md` §1 | 机端「**不做全局策略裁决**（全局节奏/批量操作由中心表达为指令）」 | 策略 → **中心** |
| `docs/ARCHITECTURE-NODE-SELF.md` §1 | 「排班（谁先跑、并发多少）｜**机端**（**按后端策略**）」 | 执行 → 机端；策略 → **后端** |
| `node/README.md` 边界 | 机端「不做全局策略裁决」 | 策略 → **中心** |

**正解（两份都能同时成立）**：
- **策略面**（谁先跑、什么时候跑、全网节奏）属 **中心**；
- **执行面**（按给定时刻发车、本机资源闸门、设备生命周期、自愈）属 **机端**。

**问题在于**：`ARCHITECTURE-NODE-SELF.md` 写的那个「按后端策略」目前**是空的** —— 中心（`internal/center`）只有 register/report/fleet/health，**没有任何策略**，也没有下行通道。
真空被机端自己补上了：`plan.go` + `roster.go` 直接在机端做全局排班裁决。**这才是越界**，不是「机端不该跑任务」。

---

## 2. 盘点：机端（`fleet.exe`）今天实际承担的全部职能

| # | 职能 | 代码 | 判定 |
|---|---|---|---|
| 1 | maa-cli / MaaCore 自举安装 | `internal/setup` | ✅ 管理环境 |
| 2 | MuMu 实例·端口配置收敛 | `internal/env` | ✅ 管理环境 |
| 3 | 设备生命周期（起停机 / 硬重启 / 端口漂移 / 孤儿清扫） | `internal/runner/device.go` | ✅ 执行 |
| 4 | 发车 · 结果判定 · 重试 | `internal/runner/slot.go` | ✅ 执行 |
| 5 | 肉鸽常驻车道 + 看护（进程退出 / 日志停转 / 设备连不起） | `internal/runner/rogue.go` | ✅ 执行 |
| 6 | 本机资源闸门（空闲内存 / CPU / 并发位） | `internal/runner/resources.go` | ✅ 执行（文档明确归机端） |
| 7 | 注册 / 状态 / 事件上报 | `internal/runner/slot.go:294` | ✅ 上报 |
| 8 | 设备体检（maa pid / asst.log 年龄 / 游戏 pid） | `internal/runner/health.go` | ✅ 上报 |
| 9 | **前瞻排班**：24h 视界、容量打包、回写 `PlanAt`/`Priority` | `internal/runner/plan.go` | ❌ **越界**（策略面） |
| 10 | **班次网格**：52 台 / 16h 三班 / 锚点对齐 | `internal/runner/roster.go` + `tools/gen_roster.py` | ❌ **越界**（策略面） |
| 11 | **下次日常时刻**：理智自循环（`left*6-30`） | `internal/runner/slot.go` `scheduleNextDaily` | ❌ **越界**（全局节奏） |
| 12 | 从 `<AUTO-MAS>` 重建账号表 / 任务 toml / profile | `internal/gen` | ⚠️ 越界（迁移工具，且读别机路径） |
| 13 | center 服务与机端**同二进制** | `internal/center` + `cmd/fleet` | ⚠️ 越界（目标机不该有后端能力） |
| 14 | 本地热改 `queue.in`（enqueue/cancel/now/disable/enable） | `internal/runner/queue.go` | ⚠️ 越界（任务增删改 = 策略面） |

> 已确认**不在**机端：`internal/` 里 ssh / 隧道 / 跨机 / Invoke-Command **0 处命中**；
> 跨机驱动只留在旧 PS 栈 `ops/rogue_cli_ops.ps1`（过渡/回滚位，见 §6）。

---

## 3. 收敛方案（分项 · 按代价从小到大）

### ① 排班策略交回中心（核心项，改动最大）
- **中心负责**：算「谁在什么时候跑」。输入 = 各机端上报的理智 / 上次耗时 / 结果；输出 = 每台机一张执行时刻表。
- **机端只负责**：拉取时刻表 → 到点发车 → 回报结果；删掉 `plan.go` 的容量打包、`roster.go` 的班次网格、`scheduleNextDaily` 的自循环推算。
- **机端保留**：本机资源闸门（第 6 项）——它是**本机**事实，不是策略。
- 前置：需要 §4 的下行通道。

### ② `fleet gen` 降级为「一次性导入器」，之后退役 —— **硬约束**
- **澄清（用户）**：`fleet gen` 读 `<AUTO-MAS>` **只是临时的** —— 我们的数据本来就不该存在那里，**我们自己的数据库还没建**。
  所以它不是「机端的一个职能」，而是一个 **临时引导器（ETL）**；**别把它当设计依赖**。
- **真正的问题**不是它读哪个路径，而是：**「任务数据的产出」这件事现在临时挂在机端上**。库建好后，产出归**任务制作器**，`gen` 退役。
- **动作**：`internal/gen` + `tools/gen_roster.py` 移出机端运行时；`gen` 降级为一次性导入器（把存量灌进库）；机端二进制去掉 `gen` 子命令。
- **附带的真问题（仍成立）**：`internal/gen/gen.go:250` 生成 profile 时写死 `E:/MuMu Player 12/shell/adb.exe` —— 这正是「profile 混了节点局部字段」的源头（见 `docs/TASK-STORE-AND-DISPATCH.md` §3.3）。

### ③ `center` 从机端二进制拆走 —— **硬约束（必须做，不是可选）**
- **依据（用户定调）**：机端**本来就是要放在另外一台电脑上**的，与后端、前端**分离、不在一起**。而现在 `center` / `setup` / `env` / `gen` / `runner` 全在同一个 `fleet.exe` 里（`cmd/fleet/main.go`）—— 装到目标机上的那个二进制**带着后端服务能力**。
- **动作**：拆成 `cmd/fleet-center`（或 build tag），机端产物**不含** `center` 子命令。

### ④ `queue.in` 热改收紧
- 任务的增删改改为**中心下发**（走 §4 通道）；机端不再有 `-enqueue/-cancel/-now/-disable` 入口（或降级为纯调试通道、默认关闭）。

### ⑤ 布点包必须只含机端 —— **硬约束（必须做，不是可选）**
- **现状**：`node/install_target.ps1:43` 拉的是**整个仓库 zip** 并覆盖解压到 `$ProjectDir` —— 于是机端机器上被塞进了 `center/`、`cmd/`、`internal/`、`docs/`，以及 **`ops/rogue_cli_ops.ps1`（含 ssh 目标与密钥配置的旧栈）**、`tools/gen_roster.py`。
- **动作**：发布机端专用包（只含 node-only 的 `fleet.exe` + `config/` + `ops/boot_runner.ps1` + `node/install_target.ps1` + README）；`install_target.ps1` 改拉该包。
- 收尾语也要改：`install_target.ps1:133` 现在写「账号表用 fleet gen 生成」—— gen 搬走后这句就是错的。

---

## 4. 连带决策：中心 → 机端的下行通道（**必须拍板**）

现状：`node/README.md` 写明 center「**只收上报，无指令通道**」—— 10-02 收敛时以「建成后 0 使用」为由删除。
而 `docs/ARCHITECTURE.md` §2/§8 的原设计是「**执行器拉取**（机端只做出站连接，目标机不开放端口）」。

只要收敛项 ①（或 ④）落地，这条通道就必须重新存在。两个选项：

| 选项 | 形态 | 代价 | 评价 |
|---|---|---|---|
| **A. 中心出表、机端拉取** | 中心 `GET /plan?node=<id>` 返回执行时刻表；机端定时拉 → 只读执行 | 小：不开实时指令流，机端仍只出站 | **推荐**：中心是策略事实源，机端是纯执行者，最贴合铁律 |
| **B. 恢复指令通道** | 中心表达指令、机端 poll + 回执（ARCHITECTURE §2 原设计） | 大：要把删掉的通道整套重建（含回执/租约） | 能力更全，但当前无需求驱动 |

---

## 5. 文档同步（收敛后必须一起改，否则口径继续打架）

| 文件 | 要改的行 |
|---|---|
| `docs/ARCHITECTURE-NODE-SELF.md` | §1 表格「排班」行：机端只**执行**中心下发的时刻表 |
| `node/README.md` | 「边界」段：补「不自算排班策略」；「运行」段删 `-enqueue/-cancel/...` |
| `docs/ARCHITECTURE.md` | §3 数据归属：明确账号表/任务文件归机端（驻场资产）、时刻表归中心 |
| `README.md` | 仍停留在 PS 单体时代，需重写（与本收敛一起做） |
| `docs/DAILY-ROSTER.md` | 若 ① 落地，班表逻辑迁到中心侧，本文移到 center 文档 |

---

## 6. 不在本次范围（但要知道它在）

- `ops/rogue_cli_ops.ps1`（40KB）仍含 **ssh / 隧道 / 跨机驱动 / cycle-watch 调度** —— 旧 PS 栈，现为过渡与回滚位。
  按铁律它整套都「不是机端的活」；退役它 = 砍掉 Go 侧的回滚位，**建议单独一次决定**，不混进本次收敛。
- `ops/boot_runner.ps1` + 计划任务：跑在**主机**上的外层在岗保证，不是机端进程内职能，**保留**。

---

## 7. 待拍板

1. ~~收敛范围~~ → **②③⑤ 已定为硬约束**（机端必须与后端/前端分离部署，见 §3）；仍待拍板的是 **①（排班策略）** 与 **④（queue.in）** 是否同批做。
2. 下行通道选 **A**（中心出表机端拉取）还是 **B**（重建指令通道）？
3. 旧 PS 栈（`ops/rogue_cli_ops.ps1`）是否同批退役？
4. 是否**先把班表（未提交的 roster 改动）**按本次结论处理 —— 它还没上生产，是收敛的最佳时机；否则先上生产再回迁，等于白跑一遍。
