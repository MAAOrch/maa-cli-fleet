# 任务存储与下发（TASK-STORE-AND-DISPATCH）

> 2026-10-03 · 状态：**PROPOSED（待拍板）** · 本文只写设计，不含代码改动
> 缘起（用户定调）：机端只有 4 件事 —— **maa-cli 自举 / MuMu 实例·端口收敛 / 上报机端与任务心跳 / 执行任务**；
> 任务模板由「**任务制作器**」产出并存入库；**调度器发一个「任务码」**，机端拿码取任务。
> 配套：`docs/NODE-BOUNDARY-CONVERGENCE.md`（边界盘点与越界清单）。

---

## 1. 「就这 4 件，没有别的了吗？」—— 是，但要把搬走的东西明确归谁

### 1.1 完备性：现有代码逐条塞进这 4 个抽屉

| 现有能力 | 代码 | 归入 |
|---|---|---|
| maa-cli / MaaCore 自举安装 | `internal/setup` | ① 自举 |
| MuMu 实例·端口配置收敛（含端口漂移写回） | `internal/env` | ② 环境收敛 |
| 注册 / 状态 / 事件上报 | `internal/runner/slot.go` `Register/ReportSummary` | ③ 心跳上报 |
| 设备体检（maa pid / asst.log 年龄 / 游戏 pid） | `internal/runner/health.go` | ③ 心跳上报（数据源，非独立职能） |
| 设备生命周期（起停机 / 硬重启 / 孤儿清扫） | `internal/runner/device.go` | ④ 执行 |
| 发车 · 结果判定 · 重试 | `internal/runner/slot.go` | ④ 执行 |
| 肉鸽常驻车道 + 看护 | `internal/runner/rogue.go` | ④ 执行 |
| 本机资源闸门（内存 / CPU / 并发位） | `internal/runner/resources.go` | ④ 执行（边界项，见 1.2） |

**结论：装得下，没有第 5 件。** 剩下的代码（plan / roster / scheduleNextDaily / gen / queue.in / center）都不是「机端职能」，是越界，见 1.3。

### 1.2 唯一边界项：资源闸门 —— 建议留机端

- 空闲内存、CPU、能起几个模拟器是**本机事实**，中心看不见；放中心必然变成「机端上报 → 等中心指示」，更慢更脆。
- 但**并发上限是策略**。建议折中：**机端报容量，中心给上限**（`runner.json` 里的值退化为机端兜底水位）。
- 若你要最严（连并发位都由中心定），列为待拍板 §7-1。

### 1.3 搬走的东西归谁（别丢东西）

| 搬走的 | 原位置 | 新归属 |
|---|---|---|
| 前瞻排班（24h 容量打包） | `internal/runner/plan.go` | **调度器（中心）** |
| 班次网格（52 台三班） | `internal/runner/roster.go` + `tools/gen_roster.py` | **调度器（中心）** |
| 下次日常时刻（理智自循环） | `slot.go` `scheduleNextDaily` | **调度器（中心）** |
| 任务 toml 生成 | `internal/gen` | **任务制作器（中心侧）** |
| 账号表**事实源** | 机端 `runner/accounts.json` | **中心 DB**（机端降为缓存） |
| 本地热改 `queue.in` | `internal/runner/queue.go` | **中心 API** |
| center 服务 | 与机端**同二进制** | **独立进程 / 二进制** |
| 外层在岗（`ops/boot_runner.ps1` + 计划任务） | 主机 | **每台 node 机器各自注册一套**（不是机端进程内职能，但必须 N 份；见 §8.4） |

> ⚠️ **反向的一条边界**：**实例映射（账号 → emu idx / 端口）是机端事实**，必须由机端上报、中心记录。
> 文档里踩过：l-4 记 31 实际 9；端口 16672→16673 漂移。中心不可能知道这个。

---

## 2. 任务存哪：三层数据 + SQLite

### 2.1 先否掉一个做法：**机端直连数据库 —— 不要**

「让机端自己去数据库里找」听起来对称，但代价大：

1. 54 台目标机 = 54 份 DB 凭据，信任边界爆炸；
2. 绕过中心的审计点（本项目的铁律是**审计汇聚在中心**）；
3. 机端被 DB schema 绑死 —— 改表 = 改所有机端；
4. 与既定形态冲突：`node/README.md` 明确「机端只做出站连接」+ 一个 token。

**建议**：机端只连**中心 API**。「拿码 → 换模板」用两个 GET 表达即可，**效果与「自己去库里找」完全一样**，但凭据不出中心。

### 2.2 三层数据模型

```
任务制作器 ──写──► templates（做什么）
调度器     ──写──► jobs（谁 / 何时 / 做哪个模板）──发码──► 机端
机端       ──续租──► jobs.lease_expires_at（心跳）
```

- **templates**：模板（与账号解耦）
- **jobs**：任务实例 —— **那个「码」就是 job_id**
- **lease**：租约 —— 让第 ③ 件事（心跳）成为调度的必要条件

### 2.3 模板必须「去账号化」（本次实测的硬证据）

实测 `.shadow/accounts.json`（54 账号）：

| 维度 | 唯一组合数 | 明细 |
|---|---|---|
| daily（client × stage） | **9** | Official\|SR-7 ×28、Bilibili\|SR-7 ×9、YoStarJP\|1-7 ×5、Official\|SR-5 ×3、Official\|SR-8 ×1、txwy\|1-7 ×1、txwy\|CE-6 ×1、YoStarJP\|CE-6 ×1、(空)\|1-7 ×5 |
| ann（client × 关卡） | **4** | Official/Bilibili/YoStarJP/txwy \| 龙门市区 |

所以现在的 `daily_l1` / `daily_a07` 命名是**把账号烧进了文件名**：54×2 个文件里 90% 是重复字节。

建议模板 id = `kind@client@参数`：

```
daily@Official@SR-7            daily@YoStarJP@1-7
ann@Official@LungmenDowntown   （= 龙门市区，MaaCore 任务名 + @Annihilation）
rogue@YoStarJP@Mizuki@d3
```

### 2.4 表设计（SQLite 单文件 `center/state/fleet.db`）

| 表 | 关键列 |
|---|---|
| `nodes` | node_id, ver, host, last_seen, state_json |
| `accounts` | account_id, node_id, client, enabled, template_daily, template_ann, template_rogue |
| `templates` | template_id, kind, client, params_json, body_toml, hash, updated_at |
| `jobs` | job_id, node_id, account_id, template_id, kind, due_at, state, attempts, lease_expires_at, created_at, started_at, ended_at, note |
| `events` | ts, node_id, kind, json |

**选型理由**：`modernc.org/sqlite` 是**纯 Go、无 cgo** 驱动 → 保持 fleet.exe 单二进制；
单文件备份符合本项目运维习惯；`jobs` 需要按 `due_at`/`state`/`node` 查询 + **原子认领**，用 JSON + 文件锁做不干净。
**迁移**：现有 `registry.json` / `events.jsonl` 一次性导入。

### 2.5 库的种子从哪来 —— **AUTO-MAS 可以彻底下线**

用户澄清：`fleet gen` 读 `<AUTO-MAS>` **只是临时的** —— 我们的数据本来不该存在那里，**我们自己的库还没建**。
所以别把它当设计依赖；反过来说，**「建库」是所有其它项的地基**。

好消息：种子**我们自己已经有了**，不需要回头依赖 AUTO-MAS：

| 表 | 种子来源 | 现状 |
|---|---|---|
| `accounts` | `runner/accounts.json`（54 条，含 `client`/`stage`/`ann`/`emu`/`daily`/`annTask`，以及我们加的 `rogueTask`/`resident`/`stateDir`） | ✅ 已存在（生产机 + `.shadow/`） |
| `templates` | 按 `(kind, client, stage)` 从 accounts 去重 → ~13 个；**正文样板**取仓库内 `config/tasks/daily_l1.toml`（daily）、`config/tasks/rogue_mizuki_l4.toml`（rogue） | ✅ 样板在仓库内 |
| `emulators` | 机端注册时上报 `(node_id, emu_idx, port)` | 建库后由机端填 |
| `nodes` | 机端注册（带容量，见 §8.3） | 同上 |
| `jobs` / `events` | 空表起步，由调度器 / 机端写入 | — |

**两个诚实的缺口**（不影响结论，但导入时会撞到）：

1. `ann_*.toml` **不在仓库里**（被 `.gitignore` 挡了）—— 样板要从**生产机**的 `config/tasks/` 拿，或从 `internal/gen/gen.go` 的生成函数还原（就是 `StartUp` 段 + `Fight(stage=<关卡>@Annihilation)`）；
2. 非肉鸽账号（`a*`）的 `daily_a*.toml` 同样被 gitignore —— 但**不需要**：模板按 `(client, stage)` 去重后只有 9 个，用 `daily_l*.toml` 的正文改两处参数（`client_type`、`stage`）即可生成全部。

→ 结论：**建库不需要 AUTO-MAS**。用 `accounts.json` + 仓库内 toml 样板就能灌满 `accounts` 和 `templates`；
`gen` 从此是历史包袱，可退役（或留作一次性导入器，读到什么算什么）。

---

## 3. 下发：调度器发「码」，机端换模板

### 3.1 协议（机端只出站，两条 GET）

```
1) GET  /api/next?node=<id>&slots=<n>
   → [{ job_id, account_id, template_id, template_hash, due_at, lease_sec }]
     ← 这一步就是「发码」：码 = job_id，短、可回执、可续租

2) GET  /api/template/<template_id>
   → TOML 原文（带 hash；机端本地有同 hash 缓存就不重复取）

3) POST /api/result   { job_id, ok, note, out_file }
4) POST /api/heartbeat{ job_id, progress }     ← 见 §4
```

### 3.2 机端怎么落地执行（**实测约束，必须按这个来**）

实测 `maa run --help` 原文：

> `<TASK>` — **Name of the task to run**. The task name is the name of the task file **without the extension**.
> The task file must be in the **tasks directory of the config directory**.

所以机端拿到模板后必须是：

```
写  $MAA_CONFIG_DIR/tasks/<job_id>.toml
跑  maa --batch run <job_id> -a 127.0.0.1:<port> -p <profile>
```

用 **job_id 当文件名**：天然唯一、可追溯、不会互相覆盖（现在 `daily_a07` 这种同名文件跨 job 会撞）。
→ 机端只做「**落字节 + 发车**」，**不解析模板语义、不拼模板** —— 正好卡在边界内。

### 3.3 容易漏的一件事：**profile 必须分层下发**（多机端下尤其致命）

实测机端 `config/profiles/default.toml`：

```toml
[connection]
adb_path = "<项目>/bin/adb/adb.exe"   # ← 节点局部（每台机的 adb 路径不同）
address  = "127.0.0.1:16384"                    # ← 节点局部（实例端口，还会漂）
[resource]
global_resource = "YoStarJP"                    # ← 与 client 绑定，跨节点一致
[instance_options]
touch_mode = "MaaTouch"                         # ← 节点局部（跟机器/模拟器走）
```

**不能整份下发**：`adb_path` 每台机不同、`address` 是实例端口 —— 多机端下整份下发 = 把别的节点的配置写坏。
正确做法拆两层，机端落盘时合成：

| 层 | 内容 | 事实源 |
|---|---|---|
| **共享层** | `client_type`、`global_resource`（由 client 推导） | **中心**（随模板下发） |
| **节点局部层** | `adb_path`、`address`、`touch_mode` | **机端**（就地注入） |

---

## 4. 心跳 = 租约续期（第 ③ 件事与调度闭环）

- 机端执行中周期性 `POST /api/heartbeat {job_id}` → 中心把 `lease_expires_at` 往后推。
- 租约过期（机端崩了 / 网络断了）→ 中心自动把 job 回 `pending`（或计一次 attempt）。
- 于是「上报心跳」**不再是单纯监控，而是调度的必要条件**：没有心跳，任务自动被回收。
- 顺带解决老问题：`running` 残留永不回收（`docs/RELEASE-STANDARD.md` G3）—— 由中心按租约判定，不再靠机端自省。

---

## 5. 必须拍板：中心不可达时，机端怎么办

`docs/ARCHITECTURE.md` §2 说「中心不可达 = 执行器继续本地自治，事件缓存待补报」，但**自治到什么程度没定**：

| 选项 | 行为 | 评价 |
|---|---|---|
| **A. 用最后一次下发的计划继续跑** | 机端缓存 plan + template，中心挂了不罢工，也不自作主张新排 | **推荐**：既不越界又不罢工 |
| B. 跑完手上这一件就停 | 等中心回来 | 安全但会空转（54 台里很多是常驻机） |
| C. 退回自主排班 | = 回到今天的状态 | 等于越界回来，否 |

特别地：**肉鸽常驻车道不该停**（它本来就是「只要在就一直刷」）→ 建模为**长租约**，定期续租即可。

---

## 6. 迁移路径（现有代码基本能复用）

| 现在（机端） | 搬去哪 | 复用程度 |
|---|---|---|
| `plan.go` 容量打包 / 提前延后窗口 / 并批 | 中心调度器 | **算法整段可用**，只是数据源从 `state.json` 换成 DB |
| `roster.go` 班次网格 | 中心调度器 | 逻辑可用，产出从「NextDue」变成 `jobs` |
| `gen.go` 生成 toml | 任务制作器 | 产出从「每账号一个文件」变成「去账号化模板」 |
| `runner` 的 slots / device / judge / rogue | 机端 | **不动** —— 它就是第 ④ 件事 |
| `syncQueue` / `scheduleNextDaily` | 删 | 由中心下发取代 |
| `queue.in` | 删 | 由中心 API 取代 |

---

## 7. 待拍板

1. **资源闸门 / 并发上限**：留机端（推荐）还是中心下发？
2. **机端是否直连 DB**：建议**否**（走中心 API）。你原设想的「自己去库里找」建议改成「拿码找中心换」——效果一样，但不把 DB 凭据发到 54 台机器上。
3. **中心不可达**：A（续跑最后计划，推荐）/ B（停下等）/ C（退回自治）？
4. **存储**：SQLite 单文件（推荐）还是继续 JSON 文件？
5. **模板去账号化**是否现在做？会顺带把 54×2 个任务文件收敛成 ~13 个模板。
6. **账号 → 节点** 是静态绑定（推荐）还是中心可动态改派？（见 §8.6）

---

## 8. 多机端 + 与后端/前端部署分离（**硬约束**）

> 用户定调：机端**本来就是要放在另外一台电脑上**的，与后端、前端**分离、不在一起**；且**机端不一定只有一个**。
> 这一条把「center 拆分」「gen 搬走」从可选升级为**必须**（详见 `docs/NODE-BOUNDARY-CONVERGENCE.md` §3 的 ②③⑤）。

### 8.1 这条约束推翻了我上一版的 4 处写法

| 位置 | 原写法 | 多机端下的问题 | 修正 |
|---|---|---|---|
| §1.3 外层在岗 | 留在「主机侧」 | 每台 node 机器都要各有一套 | → 每台 node 各自注册（§8.4） |
| §3.3 profile | 整份下发 | profile 混了节点局部字段 | → 拆共享层 / 节点局部层（已改） |
| §2.4 accounts | 只有 node_id | 少一层「模拟器实例」 | → 加 `emulators`（§8.2） |
| §3.1 /api/next | 按 node 过滤 | 缺容量与原子认领 | → §8.3 / §8.5 |

### 8.2 加一层 `emulators` —— 真正的资源是模拟器，不是账号

- 账号 → 模拟器 → 节点 是**物理绑定**：游戏就登在那台机的那个实例上。
- 端口是 node-local **且会漂**（实录 16672→16673）→ **只由机端上报，中心只读**。
- 主键必须是 `(node_id, emu_idx)`：单靠 `emu_idx` 在多机端下会撞。

### 8.3 节点注册必须带「容量与能力」

`nodes(node_id, ver, host, max_slots, free_ram_mb, cpu_pct, emu_total, emu_running, last_seen)`

派活前中心必须知道这台现在还能吃几个 —— 现在是 `runner.json` 里写死 `slots`/`maxConcurrent`。

### 8.4 外层在岗 = N 份

`node/install_target.ps1` 本来就是「一键布点」，所以是**在每台机端上各跑一次**，各自注册
`FleetRunner-AutoStart` / `FleetRunner-Watchdog`。

### 8.5 派活必须 node 级「原子认领」

`GET /api/next?node=X` 只返回 `node=X` 的 pending，并在**同一事务里置为 leased**。
多机端下「两个 node 领到同一个 job」不是理论问题。

### 8.6 节点离线：它名下的任务怎么办（**必须拍板**）

- 游戏实例物理在那台机上 → **不自动改派**（改派 = 让 MAA 去连一台没有这个游戏的机器，连不上或连错号，**危险**）；
- 建议：`last_seen` 超时 → 名下 `pending`/`leased` job 置 `held`（挂起）+ 标记需人工；node 回来自动恢复。

### 8.7 中心不可达（§5）在多机端下反而更简单

每个 node 缓存**自己那一份** plan + template，各自续跑，互不影响。

### 8.8 模板层与节点数无关（反而更值）

54 账号 / N 个节点，模板仍是 ~13 个；模板下发是一次性的，**增减节点不影响模板**。

### 8.9 部署分离的三条硬性后果（已并入收敛清单）

| # | 后果 | 依据 |
|---|---|---|
| 1 | `center` 从机端二进制拆走（**必须**） | 目标机上不该有后端能力 |
| 2 | `fleet gen` 移出机端（**必须**） | 不是「它读哪个路径」，而是**任务数据的产出不该由机端承担**；AUTO-MAS 只是临时引导源 |
| 3 | 布点包只含机端（**必须**） | 现在拉整个仓库 zip，把 `center/`、`ops/rogue_cli_ops.ps1`（含 ssh 密钥配置）都塞进了机端机器 |

---

## 9. 建议的开工顺序（**库是地基**）

| # | 步骤 | 产出 | 依赖 |
|---|---|---|---|
| 1 | **建库** | SQLite + schema（`nodes`/`accounts`/`emulators`/`templates`/`jobs`/`events`）+ 导入器（`accounts.json` & toml → 库） | — |
| 2 | **任务制作器** | 最小形态：改库里的 `templates`（先 CLI，后 API） | 1 |
| 3 | **调度器** | 库里 `accounts`+`templates` → `jobs`（`plan.go` 算法整段搬过来） | 1 |
| 4 | **机端瘦身** | 删 `plan`/`roster`/`scheduleNextDaily`/`queue.in`/`gen`；改成「拉 jobs → 拉模板 → 写 `<job_id>.toml` → 跑 → 回报」 | 2,3 |
| 5 | **拆二进制 + 瘦布点包** | `cmd/fleet-center` / node-only 包（收敛 ②③⑤） | 独立，**可并行** |
| 6 | **旧 PS 栈退役** | 删 `ops/rogue_cli_ops.ps1` 那套 | 单独决定 |

> 第 5 步**不依赖库**、纯收益（现在机端机器上躺着旧 PS 栈的 ssh 目标与密钥配置），**可以现在就做**。
