# 安全策略

## 不要提交的内容

本项目的运行时文件包含个人信息或凭据，默认已被 `.gitignore` 排除：

| 文件 | 内容 |
|---|---|
| `runner/accounts.json` | 账号 ID、游戏昵称、手机号、服务器 |
| `runner/state.json` | 任务台账（含账号 ID、输出文件名） |
| `runner/runner.json` | 本机路径、中心地址、nodeId |
| `runner/center.token` | 中心共享密钥 |
| `config/fleet.local.json` | 旧 PS 栈的 SSH 主机与私钥路径 |
| `center/state/token.txt` | 中心 token |
| `data/`、`ops/logs/`、`shots/` | 状态、日志、游戏截图 |

提交前自检：

```powershell
git status --short
git diff --cached | Select-String -Pattern '1[3-9]\d{9}|password|token|sshKey|BEGIN .*PRIVATE KEY'
```

## 如果泄露了

1. **立即轮换**：中心 token、SSH key、任何出现在提交里的密码；
2. 从当前提交中移除敏感文件，并补充 `.gitignore`；
3. 如果已经推到公开仓库，需要重写 Git 历史并 force-push（仅删除最新提交不够，
   旧对象仍可通过 commit SHA 访问一段时间）；
4. 如果涉及真实账号密码，优先在游戏 / 平台侧改密并检查登录记录。

## 报告问题

请不要在公开 issue 里粘贴账号、手机号、token 或截图原图。
可以通过 GitHub Security Advisory（私密）或仓库维护者的公开联系方式报告。
