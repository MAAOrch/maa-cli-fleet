# resource/pipeline（基座层）说明

`event_nav.json` 已**清空归档**（内容为 `{}`），不再定义任何节点。

原因：导航节点已拆成「通用层 + 活动层」两层，避免同一节点名被重复加载：

| 层 | 路径 | 内容 |
|---|---|---|
| 基座层 | `resource/image/EventNav/*.png`、`resource/model` | 模板图与 OCR 模型，保持原位，供两层共用 |
| 通用层 | `resource/common/pipeline/common_nav.json` | 跨活动通用节点：主界面/终端、结算/失败/保存阵容、理智/网络/关闭弹窗、突袭、演习、**剧情跳过（NavStorySkip / NavStorySkipConfirm）** |
| 活动层 | `resource/events/<event>/pipeline/event_override.json` | 当前活动专属：终端活动卡、hub 入口、区域页签、到位锚点、`<前缀>-S-N` 关卡节点 |

`interface.json` 的 `resource[0].path` 按以下顺序叠加（后加载层覆盖同路径图片）：

```json
["resource", "resource/common", "resource/events/act46side"]
```

换活动不要改这个空文件，也不要再往 `resource/pipeline/` 里放新节点；用
`tools/gen_event_override.py` 生成活动层，再 `setup_pi.ps1 -Event <event>` 切换即可。
详见 `ADAPT-NEW-EVENT.md`。
