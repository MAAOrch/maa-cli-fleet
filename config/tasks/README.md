# config/tasks —— maa-cli 任务模板

## 命名约定

- **肉鸽**：`rogue_<theme>_<account>.toml`，例如 `rogue_jiegarden_l2.toml`。
- **卫戍协议**：`spa_<account>.toml`，例如 `spa_a32.toml`（依赖 `config/maa/MAAOrch` 的进场导航）。
- **日常**：`daily_<account>.toml`。
- **剿灭**：`ann_<account>.toml`。
- `daily_a*.toml` / `ann_a*.toml` 由生成器产生，属于本机运行态，默认不入库。

> ⚠️ **以文件里的 `theme` 字段为准，不要按文件名猜主题。**
> 历史上为了“立刻生效”，曾把新主题内容写进旧文件（例如 `rogue_mizuki_l7.toml`
> 里实际是 Sami），这些旧文件已经归档到 `legacy/`，不要再引用。

> ⚠️ **accounts.json 的 `client` 必须和任务文件的 `client_type` 一致。**
> 日服常驻账号填 `"client": "YoStarJP"`，台服填 `"client": "txwy"`。
> 不填会退回 Official resource：日服客户端更新后 StartUp 直接 Error，
> 表现为“肉鸽/卫戍车道一直重启但没在跑”。

## 当前启用映射

| 账号 | 启用 | 日常 | 常驻 |
|---|---|---|---|
| `l2` | ✅ | `daily_l2` | `rogue_jiegarden_l2`（界园肉鸽） |
| `l7` | ✅ | `daily_l7` | `rogue_jiegarden_l7`（界园肉鸽） |
| `a19` | ✅ | `daily_a19` | `rogue_jiegarden_a19`（界园肉鸽） |
| `a32` | ✅（台服） | `daily_a32` | `spa_a32`（卫戍协议） |
| `l1` | ⏸ | `daily_l1` | `rogue_sami_l1`（萨米） |
| `l4` | ⏸ | `daily_l4` | `rogue_sarkaz_l4`（萨卡兹） |
| `l5` | ⏸ | — | `rogue_sarkaz_l5`（萨卡兹） |

停用账号没有常驻车道；`fleet runner -set-resident` 会拒绝停用账号。

`a19`（v）使用 **23 号专用模拟器**（2026-10-24 从 AUTO-MAS 共用的 4 号克隆，保留 v 的登录）；
AUTO-MAS 里 v 用户已 `Status=false`，不再参与 `并行3`（05:20 / 19:20）调度，避免和肉鸽抢号。

## 热切换

```powershell
fleet.exe runner -conf runner\runner.json -set-rogue l2:rogue_jiegarden_l2
```

切换前确认：

1. 目标文件存在且非空；
2. 文件里的 `theme` 是你要的主题；
3. 当前主题没有进行中的探索（日服会锁其他主题卡，MAA 不会自动结算/放弃）。

## legacy/

`legacy/` 是旧别名归档：文件名和内容可能不一致，仅用于查历史/回滚，**不要**再写进
`accounts.json` 的 `rogueTask`，也不要直接拿来做热切换。
