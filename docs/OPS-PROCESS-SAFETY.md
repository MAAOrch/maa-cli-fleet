# 进程清理安全规则（防误杀 runner 车道）

日期：2026-10-24
事故：用 `Get-Process maa.real | Stop-Process -Force` 清理测试进程，误杀 runner 的 4 条常驻车道。

## 发生了什么

- runner（`fleet.exe runner`，PID 1240）本身没停，但它的常驻车道也起 `maa.real.exe`；
- 按进程名全局杀 → 4 条车道同时失去 maa → 看护逻辑判「任务中断」→ 10 秒后自动重发；
- 表现：肉鸽/卫戍车道全部关掉又重启，失败计数 +1；
- 肉鸽局内进度不丢（MAA 是接着跑，不是重开新局），但当前一轮的判定被打断。

## 硬性规则

1. **禁止按进程名杀 maa**
   - ❌ `Get-Process maa* | Stop-Process -Force`
   - ❌ `taskkill /IM maa.exe /F`
   - ✅ 只按 PID：`Stop-Process -Id <pid> -Force`

2. **杀之前先核对命令行**，确认是本次测试的那一个：
   ```powershell
   Get-CimInstance Win32_Process |
     Where-Object { $_.Name -match 'maa' } |
     Select-Object ProcessId, CommandLine
   ```
   按 `-a 127.0.0.1:<端口>`、`--batch run <任务名>`、`MAA_STATE_DIR` 三者交叉确认。

3. **测试进程必须与生产隔离**
   - 独立 profile：`-p <测试名>`（不要共用 default）
   - 独立状态目录：`MAA_STATE_DIR=E:/maa-cli-fleet/data/state_<测试名>`
   - 独立模拟器实例：不占用 runner 的 emu 索引

4. **需要整体停机时走优雅停**
   - 落 `runner/runner.stop` → runner drain 后退出 → 再动 maa；
   - 恢复时删除该文件并确认 `fleet.exe runner` 已起。

5. **清理后必须核对车道**
   ```powershell
   Get-CimInstance Win32_Process |
     Where-Object { $_.CommandLine -match 'maa.*--batch run' } |
     Select-Object ProcessId, CommandLine
   ```
   确认 4 条常驻（l2/l7/a19/a32）都在。

## 当前生产车道（2026-10-24 20:26 核对）

| 账号 | 任务 | 端口 |
|---|---|---|
| l2 | `rogue_jiegarden_l2`（-p YoStarJP） | 17280 |
| l7 | `rogue_jiegarden_l7`（-p YoStarJP） | 17028 |
| a19 | `rogue_jiegarden_a19`（-p Official） | 17120 |
| a32 | `spa_a32`（-p txwy） | 16388 |

## 备注

- runner 的 `keepEmus` 当前为 `[25,28,9,34,52]`；
- 蓝熹测试实例 30 是克隆机，独立于以上车道。

---

# 附：队列结束后的自动排程（next_run.py）

日期：2026-10-29。起因：12 条并行队列原来各挂两个固定定时（05:00/19:00、08:00/21:30 一类），
和账号实际理智恢复完全脱节——有的号 12 小时就回满了，有的号还没回一半就又被拉起来跑。
改成**队列自己跑完后算下一次**。

## 在哪里

| 东西 | 路径 |
|---|---|
| 源（仓库） | `tools/fleet_pause/next_run.py` |
| 机端 | `E:\fleet-pause\next_run.py` |
| 日志 | `E:\fleet-pause\next_run.log` |
| 调用点 | `E:\fleet-pause\after_q01~q12.ps1` 末尾 |

生成器：`tools/fleet_pause/gen_scripts.py`（改模板后重跑，再 scp 到机端）。

## 判据

数据源是 MAS 自己写的历史记录，不用自己算回归速率：

```
E:\AUTO-MAS\history\<游戏日>\<账号>\*.json
    "sanity": 0,
    "sanity_full_at": "理智将在 2026-10-30 17:00 回满（20h 23m 后）"
```

```
候选 = 本队列每个号的 sanity_full_at
raw  = min(候选)            # 谁先回满用谁（早跑不浪费，晚跑才溢出）
next = min(raw, now + 12h)  # 回满比 12 小时还晚就 12 小时一轮（保底一天两轮）
```

因为 0→210 要 21 小时，**12h 上限基本总生效**；只有某号结束时还剩 ≥90 理智（卡关卡没刷掉之类）
才会被回满时间提前。

## 护栏

| 情况 | 处理 |
|---|---|
| 早于 now+20min | 推到 20 分钟后 |
| 晚于 now+26h | 截到 26 小时 |
| 落在 00:00~04:20 | 推到 **04:30**（游戏日 04:00 换日）|
| 一个历史 json 都没有 | 回退 `now + 6h` |
| 写定时时 MAS 正忙 | 轮询 `/api/dispatch/runtime/snapshot` 等空闲，最多 40 分钟 |

## 两条硬约束（踩过）

1. **不能直接在 after 脚本里改**。任务结束时 MAS 配置是锁的（`update_user` 会返回
   `配置已锁定, 无法修改`），而且 after 脚本 600s 后被 `kill_tree_on_cancel` 杀掉。
   所以用 `Invoke-CimMethod Win32_Process Create` + `pythonw.exe` 拉起脱离进程。
2. **只写定时槽不够，还要 `TimeEnabled`**。队列的 `Info.TimeEnabled=false` 时槽写了也不跑
   （并行12 原来就是这种情况）。脚本会顺手打开。

## 副作用

- 脚本只保留**一个**启用的定时槽，多余的关掉——历史遗留的 `00:15` 那种不一致会被自愈；
- 时间会**逐轮漂移**：每轮 = 上一轮结束 + 12h，所以每天往后挪一个队列耗时（约 1.5 小时），
  漂到凌晨被 04:30 截一次再重来。这是规则本身的特性，不影响清理效率。

## 排查

```powershell
Get-Content E:\fleet-pause\next_run.log -Tail 30 -Encoding UTF8
# 干跑（只算不改）：
& "E:\AUTO-MAS\runtime\environment\venv\Scripts\python.exe" E:\fleet-pause\next_run.py --queue "并行1-MAA_1"
```

要临时停用：把 `after_qNN.ps1` 末尾那段删掉/改回，定时回到手工维护。

