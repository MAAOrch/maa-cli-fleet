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
