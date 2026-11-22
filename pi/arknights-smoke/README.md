# pi/arknights-smoke — 第一条 MaaFw 原生 Pipeline

对真机截屏做模板识别 + 真实点击，**不需要 Python 宿主**：`MaaPiCli.exe -d` 直接加载 `interface.json` +
`resource/` 执行。

| 文件 | 作用 |
|---|---|
| `resource/pipeline/observe.json` | **Pipeline v2**：`Observe*` 识别已知界面；`ContinueResultsClick` 命中结算页后点 (640,660) |
| `resource/pipeline/nav010.json` | **Pipeline v2**：`Nav010Start` 从地图点 0-10 → 面板 → 编队；停在编队，不消耗理智 |
| `resource/image/ui/*.png` | 从 `tools/arknights_agent/templates/ui/` 复制（含 `stage_10_label.png`） |
| `resource/image/results/*.png` | `action_end.png` / `battle_failed.png` |
| `interface.json` | PI v2 入口（`Observe` / `Continue`），MaaPiCli 直接用 |
| `setup_pi.ps1` | 把 MaaFw 运行时链接到 `runtime/maa_cli`，生成 `config/maa_pi_config.json` |

## Pipeline 规范

- v2 嵌套：`recognition: {type, param}`、`action: {type, param}`；节点 PascalCase，`next` 按“特例优先、通配最后”排。
- 没有实际等待需求的节点显式写 `rate_limit: 0` / `pre_delay: 0` / `post_delay: 0`，避免协议默认的 1s/200ms 隐式延迟。
- `ContinueResultsClick` 的 `target: [640,660]` 是**有意保留的固定坐标例外**：结算页的“点击画面继续”没有稳定模板，整屏点击是最稳的收尾方式；其它节点都应 `target: true` 继承识别框。
- 参考已安装的 `pipeline-guide` / `maafw-pipeline` / `maafw-debug` skills；改完必须用项目入口真机跑到任务级成功。

## 直接运行（MaaPiCli，零 Python）

```powershell
pwsh -File pi\arknights-smoke\setup_pi.ps1
cd runtime\maa_cli
.\MaaPiCli.exe -d
```

本机用 MaaEnd 自带的 `D:\Xiach\M-A\MaaEnd\maafw`（MaaFw `v5.15.0-beta.1`），`setup_pi.ps1`
会把 exe/DLL 硬链接过去、`MaaAgentBinary`/`plugins`/`resource` 用 junction，不复制模板。

**加/改任务后**要把 `pi/arknights-smoke/interface.json` 重新复制到 `runtime/maa_cli/interface.json`
（`setup_pi.ps1` 只在 setup 时复制一次），否则 MaaPiCli 报 `Task not found [config_task.name=...]`。

切任务改 `runtime/maa_cli/config/maa_pi_config.json` 的 `task[].name`（`Observe` / `Continue` / `Nav010`）。

**必须注意的坑**：`config/maa_pi_config.json` 里 `adb.name` 必须等于
`MaaToolkitAdbDeviceFind` 返回的 name，否则 MaaPiCli 的 `reconfig_adb` 匹配失败，传给
`MaaAdbControlUnitCreate` 的 `config` 是空串 → `Parse config failed` → 随后 segfault。
本机 MuMu 的探测名是 **`1-MuMuPlayer v5+`**（`setup_pi.ps1` 已默认填好）。

## Python binding（调试/看节点详情，fallback）

```powershell
python -m tools.arknights_agent.maa_pipeline --entry Observe
python -m tools.arknights_agent.maa_pipeline --entry Continue
```

识别、任务图、点击同样由 native 核心执行，Python 只 bind / 打印节点；`AdbController` 用
`AdbShell` 输入，避免和 `emu_daemon` 的 minitouch 抢。

## 2026-10-28 实跑

MaaPiCli 原生（无 Python）：

```text
### All tasks have been completed ###
exit 0
# debug/maafw.log:
reco hit [result.name=ObserveMap] [result.box=[1030,630,220,80]]
Tasker.Task.Succeeded
```

Python binding 取节点详情（v1 历史记录，节点名已改成 `ObserveResults` / `ContinueResultsClick`）：

```text
[maa] entry=Observe  status=succeeded  node=ObserveResults box=(66,256,184,32)
[maa] entry=Continue status=succeeded  node=ContinueResultsClick box=(66,256,184,32)
# 点击 (640,660) 后：
[maa] entry=Observe  status=succeeded  node=ObserveMap box=(1030,630,220,80)
```

`Nav010` 导航（2026-10-28，真机）：

```text
MaaPiCli.exe -d  ->  ### All tasks have been completed ###  exit 0
debug/maafw.log: reco hit [result.name=Nav010Squad] [result.box=[1040,395,150,225]]
# 停在 0-10 编队界面，未点“开始行动”，不消耗理智
```

## 干员升级 UI（2026-10-28 实测）

- 主页「干员」中心 **(992,338)** → 干员列表；列表顺序会变，**按名字 OCR 再点**，不要用固定坐标。
- 详情页右上 EXP 条 **(1018,147)** 打开升级页；`升级十` 是**技能升级**，不是等级，别点。
- 升级页：`MAX` **(1155,181)** → 目标等级；`确定` **(1191,492)** 锁定目标；`提升等级` **(1200,605)**
  才真正消耗；成功提示「干员等级提升！」。
- 实测成本：黑角 3★ E0 LV1→30 约 **6043 龙门币**；芬 4★ E0 LV1→40 约 **13947 龙门币**。
- 目标等级选择：升级页右侧大数字是等级滚轮（30/29），按需点选；`MAX` 会直接拉到 E0 上限。

## 下一步

- 在这套 resource 里加“干员升级 / 基建建造 / 仓库”节点；用 [MaaPipelineEditor](https://mpe.codax.site/) 编辑 `pipeline/*.json`。
- 新任务加到 `interface.json` 的 `task[]`，`MaaPiCli -d` 只跑 `config/maa_pi_config.json` 里列出的 task。
- 战斗有 Copilot 作业就走 Copilot；没有才回到 `runtime` 的自适应 planner。
