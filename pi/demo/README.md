# pi/demo —— MaaPiCli 最小接线

这是一个**无设备冒烟项目**，用来验证 fleet 的 Phase 2 接线：

- `interface.json`：PI V2 最小项目；
- `resource/pipeline/demo.json`：`NavToHome` = `DirectHit` + `DoNothing`；
- `config/maa_pi_config.example.json`：MaaPiCli 的 ADB / resource / task 配置模板。

> 它只证明 MaaPiCli、项目根、`config/maa_pi_config.json` 和 fleet executor 能跑通，
> **不证明游戏导航**。真导航要把 pipeline 换成真实截图/锚点。

## 1. 链接 MaaPiCli

MaaPiCli 以 **exe 所在目录** 为项目根，所以 `MaaPiCli.exe` 必须和 `interface.json`、
`resource/`、`config/` 放在同一个目录（也就是本目录）。

```powershell
.\setup.ps1 -MaaBin "C:\Users\xiach\Downloads\MAA-win-x86_64-v5.10.4\bin"
```

脚本优先用 NTFS 硬链接（同盘不占额外空间），跨盘自动退化为复制。

## 2. 生成配置

```powershell
copy config\maa_pi_config.example.json config\maa_pi_config.json
notepad config\maa_pi_config.json
```

改三处：

- `adb.adb_path`：MuMu 的 `shell\adb.exe`；
- `adb.address`：实例端口，如 `127.0.0.1:16384`；
- `controller.name` / `resource` / `task[].name`：要和 `interface.json` 里的名字一致
  （当前是 `Android` / `Demo` / `Nav.ToHome`）。

## 3. 手跑一次

```powershell
.\MaaPiCli.exe -d
```

期望：

```text
### All tasks have been completed ###
```

退出码 0。失败时看 `config/` 和 `MaaPiCli` 输出；`-d` 只读已保存配置，不会重走交互菜单。

## 4. 接 fleet

在 `runner/runner.json` 里加：

```json
"piExe": "D:/maa-cli-fleet/pi/demo/MaaPiCli.exe"
```

重启 runner 后，`pi.demo` 这个 Kind 会注册到 Executor registry。用一条
`pi.demo:<account>` 任务即可走 `MaaPiCLIExecutor`（当前 demo 不依赖 job 参数，
真正按任务名/选项生成配置是 Phase 2 后续）。

## 5. 换成真导航

1. 用 MXU / MaaDebugger 打开本项目，截图、标锚点；
2. 把 `resource/pipeline/demo.json` 的 `DirectHit/DoNothing` 换成真实识别 + 点击；
3. 重新跑 `MaaPiCli -d`，确认 `interface.json` 的 `entry` 和 pipeline 节点名一致；
4. 再跑 fleet canary。

## 注意

- MaaPiCli 在部分控制器下会请求管理员权限（UAC）；ADB 一般不需要，但无人值守场景要先验证。
- 不要把 `config/maa_pi_config.json` 和 MaaPiCli 的二进制提交进仓库（已在 `.gitignore`）。
