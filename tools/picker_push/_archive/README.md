# _archive：已退役的驱动/导航脚本

这里的东西**不要再改、不要被新代码 import**，只作历史参考。

| 文件 | 曾经干什么 | 为什么退役 |
|---|---|---|
| `eventnav.py` | 纯 Python 的 OCR 导航：adb 截图 + RapidOCR 认页面 + adb `input tap` 点关卡 | UI 操作已改由 MaaFw PI（`pi/arknights-event`）接管：框架管识别/点击/超时/重试，Python 不再自己点屏。里面的 `stage_cleared()`（拿「代理指挥」复选框猜已通关）**已验证是错的**（未通关的关点它也会变白） |
| `nav.py` | 固定 ROI 模板认页面（TL 活动版） | 模板/坐标绑死 act55side，换活动即失效；被 PI 取代 |
| `push_stage.py` | TL 活动的手动模式推关（固定坐标点关卡） | 同上，被 `push_event.py` + PI 取代 |
| `mk_templates.py` / `stage_map.json` | TL 活动的模板生成与坐标表 | 同上 |

现在主线只有三个：

- `push_event.py`：推关决策（台账/理智/普通→突袭循环/换作业）
- `pi_runner.py`：调用 PI 任务
- `device.py`：客户端隔离 + 读理智（纯读屏，不点操作）
