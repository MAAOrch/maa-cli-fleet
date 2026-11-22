# Plan B：AI 改轴（演习免费试错）

Plan A（`pi/arknights-event` + `tools/picker_push` + `copilot-picker`）负责**跑作业站现成的作业**。
Plan B 是 Plan A 的兜底：当作业站候选作业**全部打不过**（例如 2026-11-22 的 OS-S-2 突袭，
10 份候选全 0★），就在这里基于本机地图/名册/失败战报让 AI（或人）**改写/自研 copilot JSON**，
用**演习模式免费试错**（只耗演习券，不耗理智），打到 3★ 后再切真理智正式打一把。

## 边界（绝不污染 Plan A）

- 新代码全部在 `tools/plan_b/`。
- 对既有模块的改动只有三处（都很小）：
  1. `pi/arknights-event`：新增「进入演习」「退出演习」PI 任务（`precheck.py` 通过）；
  2. `copilot-picker`：`RunOptions` 增加 `copilotFile`（直接跑本地作业 JSON）和
     `noProgress`（演习不记台账）；
  3. 无 —— `push_event.py` / 现有导航链 / 台账口径都没动。
- 演习开打前**强制走演习模式**（PI 点「演习」进编队页），不碰理智。

## 模块

| 文件 | 作用 |
|---|---|
| `context_builder.py` | 打包情报：MAA 格点表（`Arknights-Tile-Pos`，突袭优先 `#f#` 变体）→ ASCII 网格 + 两张带 `[x,y]` 标注的校验图；名册摘要（picker `/api/box/maa`）；从历史失败 run 里挑 Seed 作业；生成 `prompt_pack.md` |
| `validator.py` | 静态预检：必填字段/动作类型/干员在名册/精英等级满足/Deploy 坐标越界/不可部署格/方向/`kills·costs` 类型；不通过直接打回，不浪费演习券 |
| `battle_observer.py` | 战斗中每 12s 截图轮询（只读），OCR 击杀数/费用/生命/演习横幅，击杀新高存关键帧；结束后解析 `settle.png`（判星，含活动徽章 themed 口径）+ MAA `asst.log`（按启动时间戳过滤）→ `battle_report.json` |
| `iterate.py` | 主循环：校验 → PI「收结算→S图→S-N详情→切突袭→进入演习」→ picker 跑 `candidate.json` → 观察 → 战报；未满 3★ 生成下一轮提示 |

## 用法（Phase 1 = manual 模式）

```powershell
# 1) 打包情报（第一次或换关卡时）
python tools/plan_b/context_builder.py --stage act46side_s02 --code OS-S-2 --raid `
    --out D:\copilot-picker-local\runs\plan_b\20261122-2230

# 2) 把 AI/人改好的作业放到 <bundle>\candidate.json（没有就先拿 seed 跑通链路）

# 3) 演习跑一轮
python tools/plan_b/iterate.py --bundle D:\copilot-picker-local\runs\plan_b\20261122-2230 `
    --event act46side --rounds 1

# 4) 看 <bundle>\rounds\round-01\{battle_report.json, frames\*, next_round_prompt.md}
#    未满 3★ 就按提示改轴，覆盖 candidate.json 重跑第 3 步
```

产物目录：

```
<out>/
  context.json          # 机器可读：地图尺寸/可部署点/出生点/终点/Seed
  grid.txt              # ASCII 网格
  grid_topdown.png      # 游戏视角（y=0 顶排，[x,y] 与作业 JSON location 一致）
  grid_bottomup.png     # 数学坐标（y 向上）另存一份，避免读错方向
  roster.md             # 精二/高练度干员摘要
  seeds/job-*.json      # 参考作业（失败记录里挑的）
  prompt_pack.md        # 给 AI 的硬性约束 + 改轴说明
  rounds/round-01/
    candidate.json      # 本轮用的作业
    battle_report.json  # 战报（stars/kills/frames/asst 错误）
    frames/*.png        # 关键帧（击杀新高、费用卡满）
    next_round_prompt.md
```

## 判定 / 坑

- 判星：任务失败页 → 0；活动徽章结算（单个白色六边形）→ 3（与 picker 的 themed 口径一致）；
  普通结算页数左边亮起的青色六角星。
- 演习不耗理智，但**每轮消耗 1 张演习券**（券会自然恢复）。
- 演习从「关卡详情页 → 演习 → 编队页」进入；MAA 的 copilot 从编队页开始能正常编队+开打
  （已实测，battle_observer 能看到「演习中」横幅）。
- 没有改 `cz`/`fleet` 机队代码，也没有碰 `web/`。

## 已知限制（Phase 1）

- 只支持 S 关（演练入口只在 S 关验证过；EX/主线入口未做）。
- `--mode api`（LLM 自动改轴闭环）只是接口预留，未实现。
- 名册来自 MAA `OperBoxData.json`，没有职业/专精字段：validator 只做「干员是否存在 + 练度」校验，
  高台/地面只做「格子必须可部署」的硬校验。
- 正式打（3★ 后切真理智）还没自动化，Phase 2 再接。
