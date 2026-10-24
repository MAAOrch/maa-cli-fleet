# 卫戍协议常驻（resident 任务）

## 背景

`卫戍协议：盟约`（Stronghold Protocol，MAA 内部任务前缀 `MiniGame@SPA@`）是明日方舟的
自走棋玩法。MAA 6.x 已内置该玩法的自动循环：进入独立模拟 → 选难度 → 开局 → 打到
第 5 回合 → 结算退出 → 返回主界面 → 再来一局。

但 MAA 的 `MiniGame@SPA@Begin` 假定游戏已经停在卫戍协议主页；游戏重启/日常插队后
不会自己从大厅走进去。本仓库补了一层**进场导航**（`MAAOrch@SPA@Enter`），并把
卫戍协议接进 runner 的**常驻任务车道**：

```text
日常到点 → 暂停卫戍（杀 maa，模拟器不关）→ 跑日常 → 日常结束 → 卫戍接回
```

与肉鸽车道完全同一套机制：跑完/挂了/卡死立刻重来，模拟器不关机；日常插队优先。

## 组成

| 文件 | 作用 |
|---|---|
| `config/tasks/spa_a32.toml` | a32（台服 史蒂芬芬）的常驻任务：StartUp + Custom |
| `config/maa/MAAOrch/spa_tw.json` | 进场导航任务（大厅横幅 → 关介绍 → 放弃旧存档 → 独立模拟） |
| `config/maa/MAAOrch/SPA*Txwy.png` | 台服界面模板（横幅、关闭、放弃、确定、返回主页、独立模拟） |
| `tools/install_maa_tasks.ps1` | 把上面的任务/模板装进 `data_fresh/resource`（MAA resource 升级后重跑） |
| `accounts.json` | `a32`: `enabled=true` + `residentTask="spa_a32"` |

> `residentTask` 是新字段；旧的 `rogueTask` 仍兼容。两者同时存在时 `residentTask` 优先。
> 车道的看板标签根据任务名自动显示「肉鸽」或「卫戍」。

## 部署

1. 构建并替换 `fleet.exe`（停 runner 后再覆盖，Windows 会锁正在运行的 exe）。
2. 安装任务与模板：

   ```powershell
   .\tools\install_maa_tasks.ps1 -DataRoot E:\maa-cli-fleet\data_fresh
   ```

3. 确认 `runner\accounts.json` 的 a32：

   ```json
   {
     "id": "a32",
     "name": "史蒂芬芬",
     "client": "txwy",
     "emu": "32",
     "daily": "daily_a32",
     "residentTask": "spa_a32",
     "enabled": true
   }
   ```

   同时确认这个账号**不在 AUTO-MAS 的任何调度队列里**（避免两套系统抢同一个模拟器）。

4. 重启 runner（写 `runner/runner.stop`，等它退出后由 `ops\boot_runner.ps1` 拉起，
   或直接跑 boot_runner）。日志应出现：

   ```text
   [卫戍:a32] 常驻任务启动（任务 spa_a32，实例 32）
   [卫戍:a32] 已发车 spa_a32 ...
   ```

## 台服适配要点

- 台服包名是 `tw.txwy.and.arknights`（不是 `com.wayi.arknights`）；`GamePackage` 已修正，
  否则日常前的 `am force-stop` 会静默失败。
- 进场导航用的是**台服实机截图**裁的模板；MAA 官方的 `resource/global/txwy` 里已有
  `MiniGame@SPA` 的 8 张模板，但 **不给 Tw 的 resource 打补丁也能跑**（base 模板在
  1280×720 下匹配正常，实测模板分数见下）。
- 有未完成存档时，卫戍协议主页会变成「繼續模擬 / 放棄模擬」；导航会先点「放棄模擬」→
  「確定」→「返回主頁」，再进独立模拟。MAA 自带的 SPA 任务不会处理这个存档。

## 实测（2026-10-24，台服 a32）

### 玩法循环

- 大厅横幅模板 `score=1.0`；放弃旧存档 `0.9916`；确定 `1.0`；返回主页 `1.0`；独立模拟 `0.999`。
- 从 `am force-stop` + StartUp 开始，3 分钟进入 `MiniGame@SPA@LoopWaitStart`。
- 12 分钟自动循环里完成 2 次 `ReturnHome` / 4 次 `EnterIndependent`，`TurnFive` / `DirectExit`
  正常推进；一局约 7–8 分钟，结算约 +13 卫戍认证（第 1 回合放弃），每日防卫目标 200。
- 卫戍协议一局可能较长，车道的「同一 cur_task 空转」阈值对该车道放宽到 20 分钟
  （肉鸽仍为 8 分钟）。

### 上线轮换

- `daily:a32` 首次运行成功（`completed=7`，耗时 17m58s），按班表排下一次 **16:33**。
- 日常结束 42 秒后 `[卫戍:a32]` 自动接回，`spa_a32` 发车；4 分钟内从大厅走到
  `MiniGame@SPA@ExitButton`（自动作战中）。
- runner 日志：

  ```text
  [judge] daily_a32_1024_040030.out: completed=7 → 成功
  [daily:a32] 日常完成 → 下次日常 2026-10-24 16:33:00（理智 4/171）
  [daily:a32] 常驻机 → 保持开机，卫戍接回
  [卫戍:a32] 日常结束 → 放开卫戍
  [卫戍:a32] 已发车 spa_a32（pid=59184）
  ```


## 2026-10-24 故障与修复

当天下午出现过一次「看起来没在刷」，实际是两个独立问题叠加：

### 1. 日服客户端更新 → JP 常驻车道全挂

- 17:29 左右日服客户端更新到 `37.7.41`；
- `l1/l2/l4/l5/l7` 的 `accounts.json` 里 `client` 为空，runner 发车时没有 `-p YoStarJP`；
- MAA 加载了 **Official** resource，认不出日服标题画面：
  `StartUp Error` → `Roguelike Error`，日志里还会看到
  `Global resource Official already set, ignoring YoStarJP`。
- 修复：把这些日服常驻账号的 `client` 设为 `YoStarJP`，重启 runner。
  **以后手工加日服常驻账号必须填 `"client": "YoStarJP"`；台服填 `"txwy"`。**

### 2. 台服 emu 32 黑屏 → SPA 车道空转

- 16:33 的日常跑完后，emu 32 的游戏进程还在，但截图是**纯黑**（5336 字节）；
- MAA 每次退出都没有任务摘要（`completed=0 errored=0`），旧逻辑只会重发 MAA，
  不会修画面，所以看起来「一直在重启但没在刷」；
- 硬重启 emu 32 后恢复，SPA 进场导航重新走通。
- runner 新增：常驻车道**连续 2 次**无任务摘要 → 自动 `DeviceHardReset`；
  卫戍车道的日常暂停超时放宽到 5 分钟，给硬重启留时间。

### 3. 旧 PS 栈抢车道

- `node/fleet_node.ps1`（`FleetNode-AutoStart` / `FleetNode-Watchdog`）仍在跑，
  和 Go runner 管同一批 `l2/l7`；18:17 四路 MAA 同时退出，就是它在做 worker 巡检/抢占。
- 已禁用这两个计划任务并停掉 `fleet_node.ps1`；只保留
  `FleetRunner-AutoStart` / `FleetRunner-Watchdog`。
- 旧栈仅保留为手动兜底，不再常驻。

修复后 18:26：a32 在 SPA 自动作战（回合 3），l2/l7/a19 在界园战斗；
a32 的卫戍等级已到 **LV 52**，下次日常 `2026-10-25 08:33`。

## 注意事项

- **活动期限**：台服本期的作战结束时间是 `2026/11/05 03:59`。到期后 `MiniGame@SPA@Begin`
  找不到入口，车道会不断重试；届时停用 a32（`fleet runner -disable a32`）或换任务。
- MAA resource 升级/重装后，`data_fresh\resource\tasks\MAAOrch` 与
  `resource\template\MAAOrch` 可能被清掉，重跑 `install_maa_tasks.ps1`。
- 卫戍协议是**无理智消耗**的玩法；日常插队仍按班表/理智自循环走，两者互不占理智。
- 若游戏内出现新弹窗/公告挡住大厅，导航会尝试用「關閉」模板关掉；连续失败时看
  `data_fresh\state_a32\debug\asst.log` 的 `MAAOrch@SPA@` 匹配分数。
