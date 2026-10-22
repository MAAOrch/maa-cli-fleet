# 开机 / 意外退出 自动恢复（外层在岗保证）

> 2026-10-02 定稿，2026-10-22 按 Go 机端收敛更新。
> 分工原则：
> - **外层（计划任务）只管「进程在岗」**：谁不在拉谁，不碰槽位 / 模拟器 / 任务；
> - **runner 自己管自己的活**：队列、班表、设备生命周期、maa 发车、结果判定、自愈；
> - **中心只收上报**（没有指令通道）。

## 计划任务清单

| 任务 | 触发器 | 动作 | 管的进程 |
|---|---|---|---|
| `FleetRunner-AutoStart` | AtLogon（当前用户 / Interactive / Highest） | `ops\boot_runner.ps1` | 机端 `fleet.exe runner` |
| `FleetRunner-Watchdog` | 每 10 分钟、无限重复、IgnoreNew | 同上 | 同上 |
| `FleetTutorialWatch-AutoStart` | AtLogon（同上） | `ops\boot_tutorial_watch.ps1` | 教程弹窗 watcher（可选） |
| `FleetTutorialWatch-Watchdog` | 每 10 分钟、无限重复、IgnoreNew | 同上 | 同上 |

> 旧栈的 `FleetNode-AutoStart` / `FleetNode-Watchdog`（`boot_node.ps1`、`fleet_node.ps1`）
> 已在 2026-10-02 收敛时退役；机端只剩 runner 一个常驻进程。

## `boot_runner.ps1` 做什么

1. 找 `fleet.exe`（项目根目录，或仓库布局的 `dist\fleet.exe`）；
2. 有人工停标记 `runner\runner.stop` → 跳过；
3. **判活两道**：
   - 进程身份：`fleet.exe` 且命令行含 `runner`，排除 `-status/-once/-now/...` 一次性命令
     （不靠单一 PID，防 PID 复用误判）；
   - 心跳：`runner\heartbeat.txt` mtime 超过 6 分钟 = 主循环卡死 → 杀掉重拉；
     另外检查 `runner\hb_rogue_*.txt`，超过 15 分钟也算卡死；
4. 用 **WMI `Win32_Process Create`** 拉起（脱离计划任务 / SSH 会话进程树）。

## 恢复链（重启 / 蓝屏后无人干预）

```
开机 → 自动登录 → FleetRunner-AutoStart（~5s）
     → boot_runner.ps1
         → fleet.exe runner
             ├─ 读 runner\runner.json / accounts.json / state.json
             ├─ 起模拟器 → adb connect → 发车
             └─ 按班表/理智排下一次
```

## 实测证据（脱敏）

| 时间 | 事项 | 证据 |
|---|---|---|
| 10-01 16:48 / 22:50 | 两次蓝屏重启后自动登录 + AtLogon 任务自动恢复 | Security 4624（type-2，boot 后 ~4s）；TaskScheduler/Operational rc=0 |
| 10-02 06:10 | 进程身份判活（PID 复用场景演习） | 旧机端日志：`worker pid 失效（pid=4688 不是 cycle l-4，疑似 PID 复用）→ 清理后重拉` |
| 10-02 06:28 | `FleetRunner-*` 注册 + 触发恢复 runner | `boot_runner.log`：`runner 已拉起 pid=… rc=0`；中心台账 `last_seen` 恢复 |
| 10-02 06:30 | runner 恢复工作 | 中心台账 4 个槽位在跑（a18/a32/a40/a50） |

## 已知边界

- 计划任务是 `AtLogon`（Interactive）——**依赖自动登录**；如果机器不自动登录，需要改触发器或改用服务。
- 蓝屏 / 断电本身不修；这层只保证「再出事也能自己回来」。开机慢时从 boot 到 runner 可能需要几分钟。
- `fleet.exe runner -status` 是一次性命令，看门狗不会把它当成常驻进程。
- 人工停用：创建 `runner\runner.stop`（存在即不拉）；恢复时删除该文件。
