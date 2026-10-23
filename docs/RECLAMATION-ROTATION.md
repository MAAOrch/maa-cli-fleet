# 生息演算单独测试与 24h 轮换

日期：2026-10-24

## CLI 能力

`maa.exe reclamation` 只支持：

```text
theme = Tales（沙洲遗闻）
mode 0 = 无存档，进出关卡刷生息点数
mode 1 = 有存档，组装支援道具刷生息点数
```

不支持 RA-1 / RA-4 / RA-15。

## MAA 手动前置（官方手册）

- mode 0：需要在生息演算主界面开始；不能有存档；当前编队必须为空。
- mode 1：需要在驻扎地页面开始；有存档；当前为结算后第一天；后续三天无敌袭。
- MAA 目前没有 RA 模式导航，不能从主菜单自动进入。

## 单独测试步骤（蓝熹 / 模拟器 4）

1. 等 MAS 并行3 跑完，模拟器 4 停止；
2. 启动模拟器 4，确认蓝熹登录；
3. 手动把游戏停在：
   - 生息演算主界面（mode 0），或
   - 驻扎地页面（mode 1）；
4. 运行：

```powershell
$env:MAA_CONFIG_DIR="E:/maa-cli-fleet/config"
$env:MAA_STATE_DIR="E:/maa-cli-fleet/data/state_lx"
E:\maa-cli-fleet\bin\maa.exe --batch run reclamation_tales_lx -a 127.0.0.1:<port>
```

5. 看 `state_lx/debug/asst.log` 和 `gui.log`：
   - 能否识别生息主界面；
   - 是否进入/退出关卡；
   - 一轮结束后停在哪个界面；
   - 连续第二轮是否还能启动。

## 24h 轮换设计

runner 现在一个账号只有一条常驻车道、一个任务文件，跑完自动重来。
生息要 24h 刷，需要额外轮换层，类似肉鸽的轮换。

建议在 `accounts.json` 增加：

```json
{
  "id": "lx",
  "residentTasks": ["reclamation_tales_lx", "rogue_jiegarden_lx"],
  "residentRotateMinutes": 240
}
```

- 每 240 分钟切换一次任务；
- 生息失败/卡死时按现有 rogue 看护重试；
- 生息切回肉鸽时，肉鸽从主菜单启动，能自己导航；
- 后续可扩展成按次数或按完成结果切换。

在 runner 未改之前，可以先用：

```powershell
fleet.exe runner -conf runner\runner.json -set-rogue lx:reclamation_tales_lx
```

做手动轮换。

## 关键风险

- mode 0 用错存档会丢进度；
- mode 1 需要存档和特定天数；
- MAA 无 RA 导航，必须有人/额外脚本把游戏停在正确界面；
- 低练度账号 mode 0 进出关卡可能打不过，需要先确认能清哪一关。
