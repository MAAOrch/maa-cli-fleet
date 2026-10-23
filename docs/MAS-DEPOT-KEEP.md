# MAS 仓库保持（DepotMaintain）实测记录

日期：2026-10-24

## 结论

- 常规仓库保持 `Task.IfDepotMaintain`：51/51 原来全部关闭。
- 活动仓库保持 `Task.IfEventDepotMaintain`：50 个关闭；MikuKong 标记开启但计划为空，实际不生成任务。
- MAA 原生 `DepotMaintain` 默认策略适合日常：
  - `UpdateDepot=true`：每轮识别库存；
  - `SkipDuringActivity=true`：活动期间常规库存自动跳过；
  - `SkipDuringResourceCollection=true`：资源收集期间跳过；
  - `OnlyFirstInsufficientPlan=true`：每轮只刷第一个未达标计划；
  - 计划级 `UseMedicine=false` / `UseStone=false`。

## MAA 生成的任务（实测）

```json
{
  "UpdateDepot": true,
  "SkipDuringActivity": true,
  "SkipDuringResourceCollection": true,
  "OnlyFirstInsufficientPlan": true,
  "SortByProgress": false,
  "UseAutoSeries": true,
  "UseMedicine": true,
  "UseStone": true,
  "UseExpiringMedicine": true,
  "PlanList": [
    {
      "Stage": "1-7",
      "DropId": "30012",
      "DropCount": 1,
      "UseMedicine": false,
      "MedicineCount": 0,
      "UseStone": false,
      "StoneCount": 0
    }
  ]
}
```

任务级药/石是 MAA 默认 true；计划级是 false/0，实际刷取时以计划级为准（待 farming canary 再确认）。

## DepotData 隔离与第二轮

- 实时文件：`<MAA>/data/DepotData.json`，结构 `{"done": true, "data": {"itemId": count}}`。
- 每用户档案：`E:\AUTO-MAS\data\<scriptId>\<userId>\DepotData.json`。
- 第一轮：
  - 若用户档案不存在，MAS 会删除实时文件，避免复用上一个账号的库存；
  - MAA `UpdateDepot=true` 识别库存；
  - 任务完成后 MAS 把 `DepotData.json` 归档到用户目录。
- 第二轮：
  - MAS 先把用户档案写回 MAA 安装目录；
  - `UpdateDepot=true` 再更新；
  - 按 `DepotMaintainPlans` 顺序检查，只刷第一个未达标计划；
  - 全部达标则跳过，理智留给 Fight。
- 同一任务重试：
  - 仓库任务已完成 → 重试时禁用，不重复刷；
  - 未完成（理智不足）→ 重试继续补。

## MikuKong 识别结果（2026-10-24 04:03）

```text
龙门币        14,529,000
固源岩         3,014
高级作战记录      37
技巧概要·卷3     184
采购凭证        1,875
识别物品数      85
```

目标只设 1 个固源岩，识别后直接跳过；`DepotData.json` 已按用户归档。

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
