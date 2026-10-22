# 架构铁律：机端自管环境，后端只管跨机的事

> 2026-10-01 立（用户口述，作为硬约束）
> 一句话：**机端存在的原因是"现场"**——截图/识别/操作要求低延迟、必须在设备隔壁；
> 后端与机端**不一定是同一台机器**，所以凡是"这台机器自己的事"，一律由机端自己处理。
>
> **2026-10-02 收敛定稿**：机端只有三件事——**管理环境、执行任务、上报**。
> 机端 = 一个二进制 `fleet.exe`（`cmd/fleet/main.go`，Go 主语言，~3.5k 行）；
> 原来的 PS 机端代理（fleet_node.ps1）/ PS worker / node.go 代理 / 指令通道已全量退役（清单见文末 §6）。

---

## 1. 分工（谁负责什么）

| 事 | 归属 | 说明 |
|---|---|---|
| MAA 本体（CLI + 核心 + 资源） | **机端** | 自己查最新版、自己下载安装（`fleet setup`）；不依赖任何旧安装 |
| 本机环境（模拟器路径、adb、实例列表、端口） | **机端** | 自己探测；端口/索引漂了自己认（`fleet env check`） |
| 账号表 / 任务文件 / profile | **机端** | 自己从本地数据生成（`fleet gen`），缺失即重建 |
| 设备与任务生命周期（起停机、发车、判定、自愈） | **机端** | 现场操作，后端不参与（`internal/runner/`） |
| 排班（谁先跑、并发多少） | **机端**（按后端策略） | 本机资源闸门由机端自己判断（`resources.go`）；前瞻排班在 `plan.go` |
| 机队台账 / 指令下发 / 审计 / 对外 API / UI | **后端** | 只做"机端做不了"的跨机事务；**不碰**机端本地路径与文件 |
| 加入机队所需的地址与密钥 | 外部输入一次 | `runner.json` 的 `centerUrl` + `center/token.txt`（相当于 join），之后全自动 |

## 2. 机端进程与代码布局（收敛后）

```
fleet.exe（一个二进制）
  ├ center   机队后端：台账（节点/机台）+ 事件 + 只读 API（另一台常开机上跑）
  │            POST /register /report（X-Fleet-Token 鉴权）、GET /fleet /health
  ├ setup    自举：下 maa-cli + 补 MaaCore/资源（-direct 直连 GitHub，绕开 CN 镜像）
  ├ env      模拟器环境：show / set / restart / check / converge（单台单键原则）
  ├ gen      从 AUTO-MAS 配置生成 accounts.json + daily/ann 任务 toml + profiles
  └ runner   执行任务 + 上报（唯一常驻进程；daily/ann/rogue 三类统一排队）
```

```
cmd/fleet/main.go        入口 + 最小 flag 解析（272 行）
internal/model/          共用数据结构（Node/Account/Task…；ReadFileBOM 剥 BOM）
internal/center/         后端服务（292 行；cmd 通道已删，只收上报）
internal/setup/          自举安装（343 行）
internal/gen/            账号表/任务文件生成器（282 行）
internal/env/            MuMu 实例环境管理（189 行）
internal/netproxy/       代理感知 HTTP client（67 行）
internal/runner/         调度器全套（~2100 行）：
  runner.go  队列模型 + tick 循环 + 上报（Config/State/Runner、syncQueue、recoverResidual）
  slot.go    单任务生命周期：DeviceUp → startMaa → judgeOutFile → 理智自循环排下次
  queue.go   Task 类型 + 热改通道 queue.in（enqueue/cancel/reset/now/disable/enable）
  plan.go    前瞻排班：按 NextDue×CostSec 铺 24h 时间线，自动并批
  rogue.go   常驻肉鸽车道：per-account goroutine，看护判据 = 进程退出/日志 5 分钟不更新/设备连不起
  device.go  MuMu/adb/maa 的手脚：DeviceUp/Down、MuMuAll（按对象切块解析）、SweepOrphans、DeviceHardReset
  health.go  设备体检（maa pid / asst.log 年龄 / 游戏 pid；只查"关注机"）
  resources.go 资源闸门：GlobalMemoryStatusEx + GetSystemTimes；低于水位停止入队
```

机端边界（**不做的事**）：不渲染界面；不做全局策略裁决；不驱动别的机器；
不接中心遥控指令（任务的增删改在机端本地做：`fleet runner -enqueue/-cancel/-now/...`，
CLI 落 `runner/queue.in`，runner 每 30s tick 合并——状态在内存里，外部改文件会被覆盖，所以走热改文件）。

## 3. 自举链（一台全新机器）

```
① 下 fleet.exe（项目 Release；或用 node/install_target.ps1 一键：拉包→放 exe→写配置→注册计划任务→拉起）
② 放好 runner/runner.json（centerUrl / nodeId / mumuManager / adbExe / maaExe / 各目录）
③ fleet setup                # 下 maa-cli → bin\maa.exe + data\（MAA_*_DIR 全部重定向进项目）
   （或 fleet setup -direct：直连 GitHub 下 MaaCore 整包解成 data\lib + data\resource）
④ fleet gen -automas <AUTO-MAS>   # 生成 accounts.json + config/tasks/*.toml + profiles
⑤ fleet runner               # 由计划任务 FleetRunner-AutoStart / Watchdog 拉起
     └ 队列 → 起机 → 跑 → 停机 → 上报后端（:8790，只收上报无指令通道）
```

**验收标准**：一台干净机器上只允许两条人工输入——`fleet.exe` 文件、后端地址+密钥；其余全自动。
（现状：③④ 仍是手工命令，没有接进 ②；布点脚本会在后续版本补。）

## 4. 三类任务 + 与 PS 栈的收敛路径

详见 docs/RUNNER-UNIFIED-TASKS.md（定稿 2026-10-02，`4b535c0`）：
daily/ann 是排队型任务（跑完按理智余量/周次排下一次），rogue 是常驻型（跑完/挂了立刻重来，
实例不关机）；互斥 = 日常到点插队时暂停肉鸽（杀 maa、模拟器不动），跑完自动接回。

| 时间 | 事件 |
|---|---|
| 10-02 20:45 | 5/5 PS worker 全量迁入 runner，cycle/watch worker 退役 |
| 10-02 22:45 | fleet_node.ps1 / node.go 代理 / 指令通道退役（原"接令代理"自建成 0 使用：events.jsonl 无 command 记录） |
| —             | 隧道驱动退役（跨机=再布一台机端，机端只在本机驱动模拟器） |

## 5. 反例（今天犯的，留证）

| 时间 | 行为 | 违反 |
|---|---|---|
| 10:0x | 我从本机侧**手工生成**账号表/任务文件、**手工写** runner.json（还因 PS 字符串转义写坏过一次） | 环境事项越权到后端侧 |
| 10:2x | 机端 MAA 复用 `<MAA-CLI>` 旧安装（AUTO-MAS 时代留下） | 机端未自管环境 |
| 10:2x | MAA 核心/资源用 junction 指向 `%APPDATA%\loong\maa\data` | 同上（脏依赖） |

## 6. 待办

- [x] 退役 PS 机端/worker/隧道/指令通道 → runner 一体（10-02，5/5 迁移 + 脚本/包删除）
- [x] MAA 安装收敛到 `<项目>\bin\maa.exe` + `<项目>\data\`（setup/gen 已按此布局写入；老 <MAA-CLI> 与 junction 退休待现场验证）
- [ ] `fleet setup` 补全：环境探测 + 默认配置生成 + 调 `fleet gen` + 自检报告
- [ ] `fleet doctor`：检查并修复本机环境（maa/核心/资源/任务文件/profile/实例索引漂移）
- [ ] `fleet update`：MAA 升级 + **自我更新**（下临时文件→退出时替换自己，解决 exe 被占用）
- [ ] `install_target.ps1` 在全新机器实测（当前标 UNVERIFIED）
- [ ] 后端侧清理：不再存放/引用机端本地路径

## 7. 坑（保留，仍在用）

- `.ps1` 必须带 UTF-8 BOM（PowerShell 5.1 无 BOM 按本地编码解析中文会坏）；读 JSON 一律走 `model.ReadFileBOM`。
- 常驻进程必须用 WMI `Win32_Process Create` 拉起（Start-Process/直接 exec 的子进程会随 ssh/计划任务会话结束被杀）——`ops/boot_runner.ps1` 已按此实现。
- 看门狗判活认**进程身份**（fleet.exe 且 `runner` 且排除 -status/-once 等一次性命令），不靠单一 PID（防 PID 复用误判，10-01 踩过）；心跳两道：主循环 `runner/heartbeat.txt`（超 6 分钟=卡死）+ 车道 `runner/hb_rogue_*.txt`（超 15 分钟）。
- `runner.go` 的 rog lane 记录只能收一次（`.Wait()` 返回值），自己退出后不能再收一次否则永久阻塞（l-4 车道 6 小时事故）。
- MuMu `info -v all` 输出不是严格 JSON 且字段字母序：必须按 `"<idx>": {…}` 对象切块取字段，不能按行解析（会把属性算到上一实例头上 → SweepOrphans 关错机器）。
