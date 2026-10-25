# Phase 2 —— PI 纵向切片（SCAFFOLD IMPLEMENTED）

> 2026-10-26 · 状态：**代码骨架已落地；真机 canary 待跑**
> 关联：`ARCHITECTURE-EXECUTOR.md` §9 Phase 2、`pi/demo/README.md`

---

## 0. 一句话

fleet 用 `MaaPiCli -d` 跑 PI 项目：MaaPiCli 的 **exe 所在目录 = 项目根**，
根下放 `interface.json`、`resource/`、`config/maa_pi_config.json`。
fleet 只负责起进程、看退出码、收日志；任务内导航/识点由 PI 资源描述。

---

## 1. Spike 结论（本机 MAA v5.10.4）

| 问题 | 结论 |
|---|---|
| 非交互运行 | `MaaPiCli.exe -d`，读已保存的 `config/maa_pi_config.json` |
| 项目根 | **exe 所在目录**（`main.cpp` 用 `library_dir()` 作为 user/resource path） |
| 配置文件 | `<exe目录>/config/maa_pi_config.json` |
| 退出码 | 成功 0，失败 -1（`MaaPiCli` 的 `run()` 返回值） |
| `--help` | 不支持；会先加载 `interface.json`，不要依赖 help |
| PI 版本 | v5.10.4 的 README 标注支持到 PI v2.5.0；本 demo 只用 v2.1 字段 |
| 管理员权限 | 部分控制器会 UAC 提权；ADB 路径一般不需要，无人值守前要实测 |
| 任务选择 | `-d` 用配置里的 `task` 列表；要换任务/选项需改 `maa_pi_config.json`（或等新版子命令 CLI） |
| `MaaAgentBinary` | 在 MAA 包的 `share/`，不是 `bin/`；`pi/demo/setup.ps1` 会自动复制 |
| v5.10.4 冒烟 | interface/config/tasks 解析通过；ADB control unit 创建失败（`Parse config failed`）后 MaaPiCli segfault；真机 canary 用 v5.14.x+ |

`maa_pi_config.json` 关键结构（来自 `ProjectInterface/Types.h`）：

```jsonc
{
  "controller": { "name": "Android" },       // 对应 interface.controller[].name
  "adb": {                                    // 仅 Adb 控制器需要
    "name": "MuMu",
    "adb_path": "C:/.../adb.exe",
    "address": "127.0.0.1:16384"
  },
  "resource": "Demo",                         // interface.resource[].name
  "task": [{ "name": "Nav.ToHome", "option": [] }]
}
```

---

## 2. 已实现

| 项 | 内容 |
|---|---|
| `MaaPiCLIExecutor` | `internal/runner/executor_pi.go` |
| 执行命令 | `<piExe> -d`，cwd = `piExe` 所在目录 |
| 结果 | `Result.OK = 退出码 0`；`OutFile` 写 `<logDir>/pi_<kind>_<时间>.out`；`Note` 取输出末 8 行 |
| 注册 | `runner.json` 设 `piExe` 后注册 Kind `pi.demo`（当前切片固定这一个 Kind） |
| 回退/隔离 | 不配 `piExe` 就不注册；MAA 的 daily/ann/rogue 完全不受影响 |
| 测试 | 假命令覆盖成功/失败/取消/空 exe/日志截断，不依赖真 MaaPiCli |
| Demo | `pi/demo/`：PI V2 `Nav.ToHome` = `DirectHit + DoNothing`；`setup.ps1` 硬链接/复制 MaaPiCli |

---

## 3. 真机 Canary（待跑）

前提：一台测试模拟器/账号，**不要用生产车道**；MaaPiCli 与 `pi/demo` 在同一目录。
建议用 MAA v5.14.x+（v5.10.4 已验证项目解析，但 ADB control unit 创建会失败）。

1. 准备：

   ```powershell
   cd D:\maa-cli-fleet\pi\demo
   .\setup.ps1 -MaaBin <MAA bin>
   copy config\maa_pi_config.example.json config\maa_pi_config.json
   # 改 adb_path / address
   .\MaaPiCli.exe -d
   ```

   验收：退出码 0，输出 `### All tasks have been completed ###`。

2. 接 fleet：

   ```json
   "piExe": "D:/maa-cli-fleet/pi/demo/MaaPiCli.exe"
   ```

3. 用一条 `pi.demo:<account>` 任务（queue.in / 手工台账）跑，确认：

   - [ ] executor 起来的是 `MaaPiCli.exe -d`；
   - [ ] `logDir` 里有 `pi_pi_demo_*.out`；
   - [ ] 退出码 0 → `Result.OK=true`；失败 → `OK=false` + `Note` 有末行日志；
   - [ ] 生产 daily/ann/rogue 车道没有被影响。

4. 把 `resource/pipeline/demo.json` 换成真实导航/业务 pipeline，重跑一次；
   这一步才验证“PI 能玩游戏”，demo 只验证接线。

---

## 4. 待拍板 / 后续

1. **Kind 粒度**：当前固定 `pi.demo`；下一步需要 `pi.<project>.<entry>` 的动态注册/前缀解析。
2. **按 job 生成配置**：现在要求 `config/maa_pi_config.json` 预先配好；要支持
   `Job.TaskFile` / `Params` 选任务和 option，需要原子写配置 + 任务串行化。
3. **资源分发**：MaaPiCli + DLL 目前由 `setup.ps1` 硬链接/复制；是否纳入 `fleet setup`。
4. **管理员权限**：MaaPiCli 可能 UAC 提权；无人值守要先验证 ADB 控制器是否触发。
5. **Result.Metrics**：先从退出码/日志判成功；后续再解析结算/截图。
6. **常驻 PI 任务**：Phase 2 先只做排队型；常驻要复用 `ResidentExecutor` 的句柄/看护。

---

## 5. 参考

- `source/MaaPiCli/README.md`（`-d`、项目根、config 路径）
- `source/MaaPiCli/CLI/main.cpp`（`library_dir()` + `-d`）
- `source/include/ProjectInterface/Types.h`（`Configuration` / `AdbConfig` / `Task`）
- `pi/demo/README.md`
