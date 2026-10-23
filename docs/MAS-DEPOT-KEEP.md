# MAS 仓库保持（DepotMaintain）实测记录

日期：2026-10-24

## 结论

- 常规仓库保持 `Task.IfDepotMaintain`：51/51 原来全部关闭。
- 活动仓库保持 `Task.IfEventDepotMaintain`：50 个关闭；MikuKong 标记开启但计划为空，实际不生成任务。
- MAA 原生 `DepotMaintain` 默认策略：
  - `UpdateDepot=true`：每轮强制识别库存；
  - `SkipDuringActivity=true`：活动期间常规库存自动跳过；
  - `SkipDuringResourceCollection=true`：资源收集期间跳过；
  - `OnlyFirstInsufficientPlan=true`：每轮只刷第一个未达标计划；
  - 任务级 `UseMedicine/UseStone/UseExpiringMedicine` 默认 true。

## 实测发现的关键问题

### 1. 仓库识别会失败

第二轮强制识别时日志：

```text
DepotRecognitionTask::analyze_basic_items failed to match DepotAllTab
DepotInfo {"data":"{}","done":true}
任务出错: 仓库识别
```

识别失败后 MAA 把当前库存当成 **0**，目标 40 变成要刷 40，并且使用了 1 个过期理智药。
这不是配置错误，是 MAA 6.20.1 在当前仓库 UI 下识别 `DepotAllTab` 失败。

### 2. 默认任务级会动药/石

生成的任务默认：

```text
UseMedicine=true
UseStone=true
UseExpiringMedicine=true
```

即使计划级 `UseMedicine=false`，过期理智药仍可能被使用。

## 安全生产模板（已验证）

在每用户 MAA 配置
`data/<scriptId>/<userId>/ConfigFile/gui.new.json`
的 `Configurations.Default.TaskQueue` 中加入原生模板：

```json
{
  "$type": "DepotMaintainTask",
  "UpdateDepot": false,
  "IsStageManually": false,
  "SkipDuringActivity": true,
  "SkipDuringResourceCollection": true,
  "OnlyFirstInsufficientPlan": true,
  "SortByProgress": false,
  "UseAutoSeries": true,
  "UseMedicine": false,
  "UseStone": false,
  "UseExpiringMedicine": false,
  "PlanList": [],
  "Name": "库存保持与养成保存",
  "IsEnable": true,
  "TaskType": "DepotMaintain"
}
```

MAS 会按名字/TaskType 找到这个模板，覆盖 `PlanList`，其余字段保留。
MikuKong 实测生成结果：

```json
{
  "UpdateDepot": false,
  "UseMedicine": false,
  "UseStone": false,
  "UseExpiringMedicine": false,
  "OnlyFirstInsufficientPlan": true,
  "PlanList": [
    {"Stage":"LS-6","DropId":"2004","DropCount":40},
    {"Stage":"CE-6","DropId":"4001","DropCount":300000}
  ]
}
```

## 两阶段启用流程

### 第一阶段：识别库存

- `UpdateDepot=true`（MAA 默认即可，不需要安全模板）；
- `IfDepotMaintain=true`；
- `DepotMaintainPlans` 只设一个极小目标（例如 `1-7 / 30012 / 1`）；
- 跑一轮，检查 `data/<scriptId>/<userId>/DepotData.json`：
  - `done=true`；
  - `items` 约 85 个；
  - 有真实的 4001/30012/2004 等数量。
- 识别失败时不要进入第二阶段；目标 1 保证即使失败也只多刷 1 个。

### 第二阶段：日常保持

- 加入上面的安全模板（`UpdateDepot=false`，药/石全关）；
- 设置真实 `DepotMaintainPlans`；
- 计划顺序即优先级（`OnlyFirstInsufficientPlan=true`）：
  1. 高级作战记录 LS-6
  2. 龙门币 CE-6
  3. 技巧概要 CA-5
  4. 采购凭证 AP-5
  5. 芯片 / 固源岩按账号需要
- 目标建议 `max(current + buffer, baseline)`，不要固定 200 万/9999。

## 第二轮实测（MikuKong）

第一轮识别：

```text
龙门币        14,529,000
固源岩         3,014
高级作战记录      37
技巧概要·卷3     184
采购凭证        1,875
识别物品数      85
```

第二轮（`UpdateDepot=false` + 安全模板，目标 LS-6 40 / CE-6 30 万）：

```text
#1：高级作战记录 不足 37/40，需刷取 3
开始行动 1~4 次，-144 理智
Depot updated from stage drops
完成任务: 库存保持与养成保存 #1
开始任务: 理智任务
```

- 没有再跑“仓库识别”；
- 没有使用药/石；
- 只刷第一个未达标的 LS-6，CE-6 已满足未刷；
- 4 次 LS-6 后 2004 从 37 涨到 53，超过目标 40 后停止；
- 随后普通 Fight 才使用过期药（属于 Fight 任务自身配置，不是仓库任务）。

## DepotData 隔离与第二轮

- 实时文件：`<MAA>/data/DepotData.json`，结构 `{"done": true, "data": {"itemId": count}}`。
- 每用户档案：`E:\AUTO-MAS\data\<scriptId>\<userId>\DepotData.json`。
- 第一轮：
  - 若用户档案不存在，MAS 会删除实时文件，避免复用上一个账号的库存；
  - MAA `UpdateDepot=true` 识别库存；
  - 任务完成后 MAS 把 `DepotData.json` 归档到用户目录。
- 第二轮：
  - MAS 先把用户档案写回 MAA 安装目录；
  - `UpdateDepot=false` 直接用档案，不再强制识别；
  - 按 `DepotMaintainPlans` 顺序检查，只刷第一个未达标计划；
  - 全部达标则跳过，理智留给 Fight。
- 同一任务重试：
  - 仓库任务已完成 → 重试时禁用，不重复刷；
  - 未完成（理智不足）→ 重试继续补。

## 活动仓库行为

```json
Task.IfEventDepotMaintain = true
Task.EventDepotMaintainPlans = [
  {"StageType":"all","DropCount":200,"OrundumDropCount":0}
]
```

- `all` 每轮展开当前活动全部关卡；
- 无活动关时解析为空，任务不生成，日志“本轮跳过”；
- 常规任务 `SkipDuringActivity=true`，活动期间自动让路；
- `OrundumDropCount=0` 跳过搓玉关；
- `material` / `activity` 的 `StageIndex` 是序号，换活动可能指向不同材料，跨活动不如 `all` 稳。

## 推荐落地

1. 先做识别轮：`IfDepotMaintain=true`，计划只设一个极小目标，跑一轮拿到每账号真实库存；
2. 按 `target = max(current + buffer, baseline)` 计算目标；
3. 计划顺序即优先级（因为 `OnlyFirstInsufficientPlan=true`）：
   - 高级作战记录 LS-6
   - 龙门币 CE-6
   - 技巧概要 CA-5
   - 采购凭证 AP-5
   - 芯片 / 固源岩按账号需要
4. 活动计划统一 `all / 200 / Orundum 0`，等下一个活动验证；
5. 不搓玉的账号不要设 9999，`OrundumDropCount=0` 即可跳过。
