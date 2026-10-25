# 生息轮换 / 前置命令 Canary（仅 lx）

> 2026-10-26 · 状态：**待执行** · 范围：只验证 `lx` 测试账号，不碰 a19 / a32 / l2 / l7 生产车道
> 对应代码：`ResidentTasks` / `ResidentRotateMinutes` / `ResidentPreCommand`，以及 `residentLoop` 的轮换分支
> 关联：`OPS-PROCESS-SAFETY.md`（禁止按进程名杀 maa）、`ARCHITECTURE-EXECUTOR.md`、`RUNNER-UNIFIED-TASKS.md`

---

## 0. 这次验证什么

1. `ResidentPreCommand` 在 `force-stop` **之前**执行，且 `{addr}` 被替换成 `127.0.0.1:<端口>`；
2. 配了 `ResidentPreCommand` 时跳过 `force-stop`，让脚本自己把游戏留在发车位置；
3. `ResidentTasks` 到点轮换：杀当前 MAA → 切下一个任务 → 不判旧 out → 立刻重发；
4. 只配 `residentTasks`、不配 `residentTask` 时，首轮从列表第一个任务开始；
5. 日常插队仍能暂停/接回轮换中的车道。

---

## 1. 前置条件

- 独立的 `lx` 测试账号/模拟器/端口；**不要复用生产车道正在用的实例**。
- 独立的 runner 配置与状态目录，避免影响生产 runner：

```text
runner/canary-lx.json          # 复制 runner.json，改 stateRoot / stateFile / accountsFile
runner/accounts.lx-canary.json # 只放 lx 一个账号
data/state_lx_canary/          # 独立状态目录
```

- `config/tasks/ra_cycle_lx.toml`、`config/tasks/rogue_jiegarden_lx.toml` 存在。
- 备份 `runner/accounts.json`、`runner/state.json`（即使 canary 用独立配置，也留一份）。
- 当前 `fleet.exe` 从 `master` 构建：

```powershell
cd D:\maa-cli-fleet
go build -o fleet.exe ./cmd/fleet
```

---

## 2. lx 配置

只改 canary 配置里的 lx，不动生产账号：

```jsonc
{
  "id": "lx",
  "name": "lx 生息测试",
  "emu": "<lx 的 MuMu 实例索引>",
  "client": "<lx 实际服务器：Official / Bilibili / YoStarJP / txwy>",
  "resident": true,
  "residentTasks": ["ra_cycle_lx", "rogue_jiegarden_lx"],
  "residentRotateMinutes": 2,
  "residentPreCommand": "echo {addr}>> D:\\maa-cli-fleet\\logs\\ra-pre-canary.log"
}
```

说明：

- `residentTask` 可以不写；`ResidentTaskFile()` 会回退到 `residentTasks` 的第一个非空任务。
- `residentRotateMinutes: 2` 只用于 canary；生产按实际轮换周期配（例如 240 分钟）。
- 先跑**无害的 echo 命令**，确认顺序和 `{addr}` 替换，再换成真正的生息删档脚本。

---

## 3. 执行步骤

### 3.1 启动 canary runner

```powershell
.\fleet.exe runner -conf runner\canary-lx.json
```

不要在同一个实例上再起生产 runner。确认 `fleet.exe runner -status` 只显示 lx。

### 3.2 观察第一轮

日志里应出现：

```text
[常驻:lx] 前置命令：echo 127.0.0.1:<端口>>> D:\maa-cli-fleet\logs\ra-pre-canary.log
[常驻:lx] 已发车 ra_cycle_lx（pid=...）
```

验收：

- [ ] `前置命令` 在 `已发车` 之前；
- [ ] `logs\ra-pre-canary.log` 里有 `127.0.0.1:<端口>`；
- [ ] 前置命令和发车之间没有 `force-stop` / 游戏被杀；
- [ ] 生产车道没有被暂停或重启。

### 3.3 等第一次轮换（约 2 分钟）

日志里应出现：

```text
[生息:lx] 轮换任务 → rogue_jiegarden_lx
[肉鸽:lx] 已发车 rogue_jiegarden_lx（pid=...）
```

验收：

- [ ] 旧 MAA 被杀后没有 `judgeOutFile` 的“完成/失败”判定；
- [ ] 新任务立刻发车，没有 10 秒等待；
- [ ] label 从 `生息` 变成 `肉鸽`；
- [ ] `state.json` 没有残留旧任务或重复任务。

### 3.4 等第二次轮换（再约 2 分钟）

- [ ] 轮换回 `ra_cycle_lx`，label 变回 `生息`；
- [ ] 两轮都按 `residentTasks` 顺序循环。

### 3.5 换成真实生息删档脚本

把 `residentPreCommand` 换成实际脚本（只改 canary 配置），重启 canary runner：

- [ ] 脚本在 `force-stop` 之前执行，且退出码为 0；
- [ ] 脚本执行后游戏停在生息主题入口/大地图，MAA 能从该画面发车；
- [ ] 失败时日志有 `前置命令失败：...`，且不会把生产车道带崩。

> 注意：`runResidentPre` 失败目前只记日志、不阻塞发车；真实脚本必须自己保证幂等和退出码。

### 3.6 日常插队 / 接回

```powershell
.\fleet.exe runner -conf runner\canary-lx.json -enqueue daily:lx
```

- [ ] 日志出现 `日常插队（暂停）`，当前 MAA 被杀；
- [ ] 日常跑完后车道自动接回当前轮换任务；
- [ ] 轮换计时没有把“暂停时间”误算成任务运行时间（下一轮仍按新的 `taskSince` 走）。

### 3.7 停止与清理

```powershell
# 优雅停：落 runner.stop，等 drain
New-Item runner\runner.stop -ItemType File
```

- [ ] canary runner 退出后没有 lx 的 maa 残留；
- [ ] 生产车道仍在，PID/端口未变；
- [ ] 删除 `runner/runner.stop`。

---

## 4. 回滚

1. 停 canary runner；
2. 恢复 `runner/accounts.json` / `runner/state.json` 备份（如果动过）；
3. 删除 `runner/canary-lx.json`、`runner/accounts.lx-canary.json`、`data/state_lx_canary/`；
4. 确认生产 `fleet.exe runner` 仍在，4 条常驻车道（l2/l7/a19/a32）在线；
5. 按 `OPS-PROCESS-SAFETY.md` 只按 PID 清理测试进程。

---

## 5. 证据模板

```text
canary 时间：
lx 端口 / emu：
前置命令：
第一次轮换：
第二次轮换：
日常插队：
异常：
生产车道核对（l2/l7/a19/a32）：
结论：PASS / FAIL
```

---

## 6. 禁止事项

- ❌ 在 a19 / a32 / l2 / l7 上直接试；
- ❌ `Get-Process maa* | Stop-Process -Force` 或 `taskkill /IM maa.exe /F`；
- ❌ 没跑过 echo 无害命令就直接上真实删档脚本；
- ❌ 在同一个模拟器实例上同时跑 canary 和生产 runner。
