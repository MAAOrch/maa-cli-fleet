# picker_push：活动推关（MaaFw PI 导航 + copilot-picker 执行）

职责拆三层（2026-11-22 与用户对齐，P0–P2 已落地）：

| 层 | 位置 | 干什么 |
|---|---|---|
| UI 导航 | `pi/arknights-event`（MaaFramework **PI V2**） | 主界面→终端→活动 hub→普通/EX/S 图；点开关卡详情；收结算页/失败页/保存阵容弹窗；切突袭；回主界面。兜底：补充理智弹层、网络重连、通用确认框 |
| 决策 | 本目录 Python（`push_event.py`） | 读台账/理智、判断页面类型、选作业、普通→突袭→下一关循环、失败换作业 |
| 执行 | copilot-picker（Go，`internal/copilotpick`） | `onMap`/`manualMode` 两种入口，MaaCore Copilot 按作业 JSON 自动编队+部署+开技能 |

Python 这边只剩 3 个文件（旧脚本都在 `_archive/`）：`push_event.py`（主流程）、
`pi_runner.py`（调 PI 任务）、`device.py`（客户端隔离 + 读屏判断页面/理智，**不做点击**）。

## 用法

```powershell
# 推 EX 第 8 关（普通+突袭，台账里已 3 星的自动跳过）
python tools\picker_push\push_event.py --event act46side --stages OS-EX-8

# 从某关推到某关
python tools\picker_push\push_event.py --event act46side --from OS-S-1 --to OS-S-5

# 只跑普通 / 只跑突袭
python tools\picker_push\push_event.py --event act46side --stages OS-S-2 --mode raid

# 只体检导航+作业候选，不开打
python tools\picker_push\push_event.py --event act46side --stages OS-S-1 --dry-run

# 单独跑一个 PI 导航任务（第二个参数是任务名）
python tools\picker_push\pi_runner.py "进入EX图"
```

前提：`pwsh -File pi\arknights-event\setup_pi.ps1` 生成 `runtime\maa_cli_event`；
picker 服务在 `127.0.0.1:8790` 跑着（`D:\copilot-picker-local`）。

## 推关顺序（用户指定）

**本关普通 → 本关突袭 → 下一关普通 → 下一关突袭 → …**
某关突袭几份作业都打不过就记下跳过，继续下一关；理智不足（MAA `BattleStartAll` 起不了
战斗）整批停手报告。

## 两条执行路径（2026-11-22 定标准）

| 路径 | 适用 | 判定 |
|---|---|---|
| **onMap** | EX 图（主线同理） | 游戏停在活动图 → MAA `find_stage` OCR 认关卡 → 自动切突袭开打。结算页被 MAA 自己点掉，所以按「Copilot 链完成且无 TaskChainError」判通关、台账记 3 星 |
| **manualMode** | **S 图（推荐新活动也用这条）** | PI 先点开关卡详情页（需要突袭就先切突袭）→ `maa copilot` 单文件执行。能拿**结算截图真判星**，也绕开 MAA 缺新活动资源时 `find_stage` 把 `OS-S-N` 认成 `0S-S-N` 的坑 |

## 活动配置

`events/<event>.json`：`api/account/client/adb/device`、`client_package`/`other_clients`
（客户端隔离）、`areas`、`manual_areas`（默认 `["s"]`）、
`stages.<代号>.{stageName,area,mode,cost}`（作业站关卡代码如 `act46side_ex07`）。

## 判定口径（2026-11-22 修）

- **普通关必须 3★ 才算达标**：manualMode 下 `verifySettlement` 数星，1★/2★ 也会记进台账
  （保留最高星）但**判本份作业失败、自动换下一份重打**，直到 3★ 或 `--max-jobs` 用完
  （3★ 才解锁突袭/代理指挥，1★ 直接往下走会撞「突袭未解锁」）。onMap 无结算页，按任务链完成判 3★。
- **onMap**：MAA 打完会自己点掉结算页回关卡图，不能用结算截图判星；按
  「`TaskChainCompleted` 且无 `TaskChainError`」判通关（0 星会命中
  `Copilot@FightMissionFailed` 报 TaskChainError），台账记 3 星。
- 旧坑：`strings.Contains(logText,"FightMissionFailed")` 是假阳性——MAA 轮询
  `WaitUntilEndOfAction` 时把 `Copilot@FightMissionFailed` 列进候选，日志每场战斗都带，
  会把**每一场**判 0 星还重打一遍。现在只认真正命中的 `"task":"FightMissionFailed"`。
- **PI 任务结果判定**：MaaPiCli 即使超时失败也打印「All tasks have been completed」，
  `pi_runner` 只认 `debug/maafw.log` 里**本次启动时间之后**的 `Tasker.Task.Succeeded/Failed`；
  一条新记录都没有 = 失败（已删掉 stdout fallback，防假成功）。

## 作业难度过滤（2026-11-22 加）

作业站标题会写明适用难度（「仅突袭」/「仅普通」/不带字样=通用）。分难度搜作业时服务端
（`internal/copilotpick/search.go` 的 `filterJobsByMode`）会按标题筛：
打普通跳过含「突袭」的、打突袭跳过「仅普通」/「普通」的，避免拿错难度的轴白打 0 星
（实测 S-3 突袭连试 3 份普通作业全 0★）。

## adb 端口自动探测（2026-11-22 加）

MuMu 实例重启后 **adb 端口会漂**（实测 16384 → 16385），配置里写死的端口立刻失效。
现在两端都自动校正：

- picker（Go，`accounts.go`）：`accounts.json` 的 port → vm_config → 端口公式逐个试，
  只认 `adb devices` 里**真正在线**的那个；都不在线时现场问 `MuMuManager info -v <idx>`
  要实时 `adb_port`（带 20s 缓存）。
- 驱动（Python，`device.resolve_device`）：启动时同样校正，日志会打
  `[device] adb 端口漂移：… 改用 …`。

所以 **不用再手工改 `accounts.json` / 活动配置里的端口**。

## 理智药（默认关闭）

默认 `--max-potions 0`：理智不足（MAA `BattleStartAll` 起不了战斗）就停手报告。
要用就 `--max-potions N`（N 为最多几个），**双保险**：

1. PI「补充理智」任务必须 OCR 到弹层底部的「理智合剂」确认语才点 ✓，认不到直接失败停手
   （等价于“当前选中的不是理智药 → 绝不动手”，绝不碰至纯源石）；
2. 补完再读一次理智，没验证到上涨也停手。

## 稳定性措施（P0）

- **客户端隔离下沉到 picker**：`internal/copilotpick/nav.go` `isolateClient()`
  在任何 `maa startup`/起游戏/重启前 force-stop 非目标服的舟游客户端
  （2026-11-22 05:32 官服在前台被误唤醒，白打 3 场）；驱动启动时也会 `device.ensure_only_client()`。
- **PI 弹窗兜底**：`NavRefillDialog`（理智不足弹层，点 X 关掉）、`NavNetworkRetry`
  （重连接口）、`NavCloseDialog`（通用确认/关闭）。
- **PI 结果判定**：MaaPiCli 即使任务超时也会打印「All tasks have been completed」，
  `pi_runner` 改看 `debug/maafw.log` 里最后的 `Tasker.Task.Succeeded/Failed`。

## 复刻活动：已通关的关判断不了

复刻继承上一轮进度，但游戏内**没有可机读标记**（地图节点逐像素一样、详情页三星是主题装饰、
代理指挥不是通关标记）。唯一可靠做法 = picker 的 `progress.json` 台账 + 人工告知起始关。
重复推会白花理智（只有 0 星失败会返还）。

## 文件

- `push_event.py`：推关驱动
- `pi_runner.py`：调用 `pi/arknights-event` 的 PI 任务（含结果判定）
- `device.py`：客户端隔离 + 读屏判断页面类型/理智（纯读，不点操作）
- `events/act46side.json`：雪山降临1101 复刻配置
- `_archive/`：已退役的纯 Python OCR 导航（`eventnav.py` 等），只作历史参考

## 相关文档

- PI 项目：`pi/arknights-event/README.md`
- 新活动适配清单：`pi/arknights-event/ADAPT-NEW-EVENT.md`
- picker 契约（`onMap`、判定口径）：`docs/COPILOT-PICKER-CONTRACT.md`
