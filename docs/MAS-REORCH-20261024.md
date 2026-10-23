# MAS 全量脚本去重与队列重排（2026-10-24）

## 目标

- 每个启用账号只保留一个正式脚本；
- 旧共享脚本、`补跑` 重复脚本、无启用用户的脚本归档；
- 正式队列 `并行1..7` 重建；
- 队列按单双分配 MAA 池；
- 客户账号业务配置切到 `independent`；
- 队列错峰，避免 7 条同时开跑。

## 结果

| 项 | 数量 |
|---|---:|
| 重排前脚本 | 76 |
| 正式脚本（启用账号） | 51 |
| 归档脚本 | 25 |
| 正式队列 | 7 |
| 正式队列项合计 | 51 |
| 启用账号身份 | 51 |
| 重复启用身份 | 0 |

- 归档脚本统一改名 `归档-*`，原启用用户改为 `Status=false`，配置保留。
- 旧共享脚本 `官-1/2/3/4` 已全部归档，原队列项已移除。
- `补跑单号-1017-*` 重复脚本已全部归档。
- 每个正式脚本只有一个启用用户；每个脚本只在一个正式队列里。

## 队列与 MAA 池

| 队列 | 账号数 | MAA | 定时（每日） |
|---|---:|---|---|
| 并行1-MAA_1 | 9 | MAA_1 | 05:00 / 19:00 |
| 并行2-MAA_2 | 8 | MAA_2 | 05:10 / 19:10 |
| 并行3-MAA_3 | 7 | MAA_3 / MAA_8（按队列项单双） | 05:20 / 19:20 |
| 并行4-MAA_4 | 6 | MAA_4 / MAA_9（按队列项单双） | 08:00 / 21:30 |
| 并行5-MAA_5 | 7 | MAA_5 | 08:10 / 21:40 |
| 并行6-MAA_6 | 7 | MAA_6 | 08:20 / 21:50 |
| 并行7-MAA_7 | 7 | MAA_7 | 10:30 / 00:15 |

- 波次 A：并行1/2/3（3 并发）；
- 波次 B：并行4/5/6（3 并发）；
- 波次 C：并行7（单独）。

这样任意时刻 MAS 最多 3 条队列并发，给 runner 的肉鸽任务留 CPU。

## 配置来源

- 51 个正式账号全部 `Info.Base=independent`。
- 切换前已把 `data/<scriptId>/Default/ConfigFile` 物化到
  `data/<scriptId>/<userId>/ConfigFile`，避免独立配置首次运行读到 MAA 安装目录残留。
- 归档脚本保留 `shared` 配置和禁用用户，不再参与调度。

## 备份

- 重排前完整备份：`E:\AUTO-MAS\backups\pre_reorch_20261024_001858`
- Apply 前配置备份：`E:\AUTO-MAS\backups\reorch_20261024_003113`
- 重排 Plan：`E:\maa-cli-fleet\dist\reorch_plan.json`
- Apply 报告：`E:\maa-cli-fleet\dist\reorch_apply_report.json`

## 验证

`tools/mas_reorch_verify.py` 数据项全项 PASS：

- 7 条正式队列、51 个队列项、顺序与 Plan 一致；
- 51 个启用身份、51 条启用用户记录，无重复身份；
- 归档脚本无启用用户，旧共享名已消失；
- 正式脚本 MAA 路径与 Plan 一致，MAA 路径不跨队列复用；
- 归档队列里的 6 个正式脚本项已清除。

脚本顺序已调整为 **51 个正式脚本在前、25 个 `归档-*` 在后**。

队列审计（`tools/mas_queue_audit.py`）：

```text
queues=14 official=7 temporary=0 archived=7 enabled=7
pathConflicts=0 duplicateScripts=0
```

配置前后对账（`tools/mas_reorch_compare.py --check-password`）：

```text
checked=51 user_issues=0 script_issues=0
```

- 51 个账号的 `Stage` / `Task` / `Notify` 整段未变；
- `Info.Name/Id/Server/Notes/Status/RemainedDay` 未变；
- 密码密文会因运行后重新 DPAPI 加密而变化，但解密后的明文一致；
- 脚本级只允许 `Info.Name` / `Info.Path` 变化，实际无其它字段变化。

`并行3/4` 显示 `REVIEW` 是因为它们各用两个 MAA 核心（MAA 池），属于预期。

## Canary

`并行5` 实跑 7 个账号（首个 b-1 已切 independent）：

- b-1 `Success!`，1-7 掉落正常，耗时 9m35s；
- b-12 / b-5 / 日-14 / 日-24 `Success!`；
- 日-4 `MAA 在完成任务前中止`，日志为「游戏掉线，停止任务」，理智 204 未消耗；
- b-13 前两轮都卡在 MAA 的 `adb devices` 枚举，各 10 分钟超时后被 MAS 重试；
  第三轮已连上 16992，`adb devices` 0.02s、`shell echo ok` 正常，正在跑日常。
  ADB server 与 runner 的 17028/17280 共用，属于 ADB 枚举偶发争用，不是账号问题。
- 官组 28 个克隆都有手机号 `Info.Id`，运行时 MAS 会按 Id 重写
  `StartUp.AccountName`，同模拟器串行切号不会串号。

结论：independent 配置路径可用；日-4 是账号/登录状态问题，b-13 是 ADB 枚举偶发卡死，
都不是重排配置错误。并发仍是 3 条队列，给 runner 留 CPU（runner `maxCpuPct=65`）。

## 已知问题

- **日-4（河清灯_别寄圆通，YoStarJP）游戏掉线**：需要首次登录/重新登录后
  再跑；不要直接重试队列，否则会连续失败。
- **MAA_8 已从 6.18.0 升级到 6.20.1**：应用了目录里已有的 OTA 包，
  与 MAA_9 的 `MAA.exe`/`MAA.dll`/`MaaCore.dll`/`MaaUtils.dll` 哈希一致，
  `resource/version.json` 一致。备份在
  `E:\MAA-Multi\backup\MAA_8_6.18.0_20261024`。
- 归档脚本仍保留在 MAS 脚本列表，只是统一前缀和禁用用户；
  稳定运行一段时间后可选择删除，或保持归档。

## 工具

| 工具 | 用途 |
|---|---|
| `tools/mas_reorch_apply.py` | 去重、归档、重建队列、MAA 池（Plan/Apply） |
| `tools/mas_reorch_base.py` | 物化独立 ConfigFile 并切 `Base=independent` |
| `tools/mas_reorch_verify.py` | 重排后全项对账 |
| `tools/mas_reorch_compare.py` | 重排前后账号业务字段对账 |
| `tools/mas_reorch_schedule.py` | 错峰定时与启用队列 |
| `tools/mas_reorch_cleanup.py` | 清归档队列重复项、正式脚本排前 |
| `tools/mas_reorch_fix_archive.py` | 补归档被锁脚本 |
| `tools/mas_watch_tasks.py` | 后台监视 dispatch 任务直到清空 |

## 后续

1. 观察 05:00 / 08:00 / 10:30 三个波次的首轮定时运行；
2. 重点看 `并行3/4` 的 MAA_8/MAA_9 切换是否还有日志锁；
3. 日-4 重新登录后再单独补跑；
4. 稳定后决定是否删除 25 个 `归档-*` 脚本，把脚本列表压到 51 个。
