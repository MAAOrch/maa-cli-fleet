# tools/dsh_mcp — DSH 远程 MCP 网关

把**本机 DSH** 封装成一个受控的远程 MCP Server，供云端 Gemini（或任意 MCP 客户端）通过
**Streamable HTTP + 内置 OAuth 2.1** 直接调用本机子代理。

- 传输：Python 官方 `mcp` SDK，Streamable HTTP，端点固定 `/mcp`，**仅监听 `127.0.0.1:8795`**。
- 鉴权：除 `GET /health`（固定返回 `{"ok": true}`）外，所有 `/mcp` 请求必须带 Bearer：
  内置 OAuth 2.1 签发的 `access_token`（Gemini 等远程客户端），或本机 `DSH_MCP_TOKEN`
  （兼容本地脚本/冒烟）；**Token 为空时进程直接拒绝启动**。
- OAuth：内置 RFC 9728 / RFC 8414 发现端点、RFC 7591 动态客户端注册、PKCE(S256) 授权码、
  刷新令牌；授权页口令直接复用 `DSH_MCP_TOKEN`，不需要任何外部 IdP。
- 派活：`dsh_dispatch` 后台拉起 headless DSH 子进程，**立即返回 `job_id`**，绝不阻塞等待。
- 同工作区严格互斥：一个 workspace 同时只允许一个 running 任务。
- 每次调用（含 dry-run、急停）逐条追加审计到 `ops/dsh_mcp_audit.jsonl`。

目录：

```
tools/dsh_mcp/
├── server.py                     # MCP Server + Streamable HTTP + 鉴权 + --check 冒烟
├── ws_tools.py                   # ws_glob/ws_grep/ws_edit_file/ws_write_file/ws_run_cmd
├── oauth.py                      # 内置轻量 OAuth 2.1（发现/DCR/PKCE/授权码/刷新令牌）
├── job_runner.py                 # 单个 job 的监督进程（日志/超时/退出码）
├── common.py                     # 白名单、路径安全、审计、job 元数据
├── headless-account.patch.yml    # 让 headless 复用本机已登录 DeepSeek 账号
├── start.ps1                     # 启动脚本（强制 DSH_MCP_TOKEN）
└── README.md
```

运行态文件：

```
runtime/dsh_jobs/<job_id>/output.log    # dsh --json 的 NDJSON 实时输出
runtime/dsh_jobs/<job_id>/meta.json     # 任务元数据/状态（server 维护）
runtime/dsh_jobs/<job_id>/runner.json   # job_runner 与 dsh 的 PID
runtime/dsh_jobs/<job_id>/exit.json     # 结束状态/退出码/final 摘要（job_runner 写）
runtime/dsh_jobs/<job_id>/final.txt     # 最终答复纯文本（有的话）
runtime/dsh_mcp_oauth.json              # OAuth clients/codes/tokens（只存 SHA-256 摘要）
ops/dsh_mcp_audit.jsonl                 # 全量审计（每次 MCP 调用一条）
```

---

## 一、开工探测结论（2026-11-23，本机实测）

### 1. DSH 真实启动路径

- `dsh` = `%LOCALAPPDATA%\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd`
- 该 `.cmd` 设置 `ELECTRON_RUN_AS_NODE=1`，用 `DeepSeek Harness.exe`（Electron 自带 Node）
  执行 `app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js`。
- 因此子进程可以直接 `Popen(["...\dsh.cmd", ...])`，不依赖当前 shell。

### 2. 非交互调用方式：`dsh --profile headless`

```text
dsh --profile headless [--json] [--patch <overlay.yml>]
    [--permission-mode read-only|workspace-write|danger-full-access]
    [--approval-policy ask|never] [--session-id <id>] [--add-dir <path>] -
```

- `-` 表示 **从 stdin 读任务文本**（本网关就用这个传 prompt，避免命令行转义/注入）。
- `--json`：stdout 输出逐行 NDJSON 事件（`session` / `status` / `tool_call` /
  `text` / `final` …），诊断信息走 stderr。网关把 stdout+stderr 合并写 `output.log`。
- **工作目录就是 cwd**：`subprocess.Popen(..., cwd=<白名单工作区>)` 即生效，
  启动事件里的 `cwd` 字段可直接验证。
- 退出行为：任务结束进程退出；`final.reason.kind == "error"` 时表示模型/工具链报错。

实测（`D:\tmp\dsh_probe`）：`--json -` 读 stdin、cwd 生效、在 workspace-write 沙箱内
成功写文件、`final` 事件带 `text`，整轮约 8s。

### 3. 凭据与模型（本机必须带 patch）

headless profile 默认配置为 `commandcode / deepseek/deepseek-v4.1-flash`，其 provider
解析环境变量 `COMMANDCODE_API_KEY`；**本机没有该变量**，直接跑会得到：

```
MISSING_CREDENTIAL: llm-pi-ai: no credential for provider route "commandcode";
its profile resolves COMMANDCODE_API_KEY, which is not set
```

本机 desktop profile 用的是 `deepseek-account / deepseek-flash`（已登录 DeepSeek 账号）。
headless bundle 默认把 `authorization` / `credentials` / `deepseek-account` 三个插件
disabled 掉，所以网关通过 `--patch headless-account.patch.yml` 重新启用：

```yaml
- id: authorization
  disabled: false
- id: credentials
  disabled: false
- id: deepseek-account
  disabled: false
- id: agent-default-model
  config:
    provider: deepseek-account
    model: deepseek-flash
```

这套组合实测有效（`Reply with exactly: PONG` -> `PONG`）。如果以后改用环境变量 API Key，
可删除 patch 或在 `job_runner._build_argv` 去掉 `--patch`。

### 4. 权限模式

- 网关默认 `--permission-mode workspace-write --approval-policy never`：
  子代理的沙箱根 = 工作区目录，只能写工作区内文件；无人值守下不会卡审批。
- 需要构建/装包等更大权限时，启动网关卡前设置环境变量
  `DSH_MCP_PERMISSION_MODE=danger-full-access`（**会放宽到整机，谨慎**）。

---

## 二、安全红线（实现位置）

| 红线 | 实现 |
|---|---|
| 仅监听回环 | `server.py` 固定 `uvicorn.run(host="127.0.0.1")`，无 0.0.0.0 分支 |
| 强制鉴权 | `_AuthGate` 包住 `/mcp`：接受 OAuth `access_token` 或 `DSH_MCP_TOKEN`，否则 401 |
| OAuth 发现 | 401 带 `WWW-Authenticate: Bearer resource_metadata="<origin>/.well-known/oauth-protected-resource"` |
| 授权口令 | OAuth 授权页口令 = `DSH_MCP_TOKEN`，`hmac.compare_digest` 常数时间比较 |
| PKCE | 只接受 `code_challenge_method=S256`；token 端点校验 `SHA256(code_verifier)` |
| 令牌存储 | `runtime/dsh_mcp_oauth.json` 只存 SHA-256 摘要；授权码一次性、5 分钟过期 |
| 空 Token 拒启 | `_serve()` 启动前检查 `DSH_MCP_TOKEN`，为空 `return 2` |
| 目录白名单 | `common.WORKSPACES` 硬编码 `main -> D:\maa-cli-fleet`、`slot2 -> D:\maa-cli-fleet-b` |
| 防路径穿越 | `common.safe_read_path()` 拒绝绝对路径/盘符/UNC/`..`，解析后必须仍在白名单根内 |
| 敏感文件黑名单 | accounts.json、.env、`*token*`、`*secret*`（命中任意路径段即拒读） |
| 同工作区互斥 | `_dispatch_lock` + `active_job_for_workspace()`（重启后按 exit/PID 回收） |
| 全量审计 | `common.audit()` -> `ops/dsh_mcp_audit.jsonl`，字段含 `ts/tool/workspace/apply/job_id/status`；`ws_*` 由 `ws_tools` 内部记账，`server._audit_call` 不重复记录 |
| 命令执行约束 | `ws_run_cmd` 固定 `shell=False`、cwd=工作区根、白名单程序（python/py/go/git/npm/npx/rg），拦截 git 重定向选项/危险子命令/远端写 |
| 动态 Origin | 元数据 URL 从 `X-Forwarded-Proto`/`X-Forwarded-Host`/`Host` 推导，`DSH_MCP_PUBLIC_URL` 可覆盖 |

> 修改白名单：编辑 `common.py` 的 `WORKSPACES` 字典即可，**不要**从远端参数接收路径。

---

## 三、11 个 MCP 工具

`dsh_*` 6 个走“派活给 headless DSH 子代理”；`ws_*` 5 个由网关进程直接执行（不经过第二层子代理），
路径同样受白名单/防穿越/敏感文件黑名单约束，且各自在 `ws_tools.py` 内部逐条写审计。

| 工具 | 类型 | 参数 | 说明 |
|---|---|---|---|
| `dsh_list_workspaces` | 只读 | — | 白名单工作区、各目录 git 分支/状态、当前 running 的 `job_id` |
| `dsh_dispatch` | 写 | `workspace`, `prompt`, `apply=false`, `timeout_sec=600` | 默认 dry-run 返回命令预览；`apply=true` 检查互斥后在后台拉起，立即返回 `job_id` |
| `dsh_status` | 只读 | `job_id`, `tail_lines=50` | 状态、耗时、退出码、`output.log` 尾部 |
| `dsh_result` | 只读 | `job_id`, `include_git_diff=true` | 最终输出摘要 + `git status --short` + 精简 diff（超 8000 字符截断） |
| `dsh_cancel` | 写 | `job_id` | `taskkill /T /F` 结束整棵进程树并标 `cancelled` |
| `dsh_read_file` | 只读 | `workspace`, `rel_path`, `max_chars=8000` | 白色单内读文本，受穿越/敏感黑名单保护 |
| `ws_glob` | 只读 | `workspace`, `pattern`, `max_results=200` | 按相对 glob 列文件（支持 `**`），跳过 `.git`/`node_modules`/`__pycache__`/`runtime`、链接与敏感文件 |
| `ws_grep` | 只读 | `workspace`, `pattern`, `rel_path=""`, `glob_filter=""`, `max_matches=100`, `ignore_case=false` | 正则逐行检索，返回 `path/line/text`；跳过二进制、超大文件与敏感文件 |
| `ws_edit_file` | 写 | `workspace`, `rel_path`, `old_string`, `new_string`, `replace_all=false` | 原子替换；`replace_all=false` 时要求 `old_string` 恰好出现 1 次，否则拒绝 |
| `ws_write_file` | 写 | `workspace`, `rel_path`, `content`, `overwrite=false` | UTF-8 原子写入；目标已存在默认拒绝，需显式 `overwrite=true` |
| `ws_run_cmd` | 写 | `workspace`, `argv`, `timeout_sec=30` | `shell=False` 运行白名单程序（python/py/go/git/npm/npx/rg），cwd=工作区根，env 已剥离 Token |

`ws_run_cmd` 是“受控但不沙箱”的执行入口：白名单内的 python/go/npm 本身就能执行任意代码，
因此调用方必须持有网关 Bearer Token。

状态取值：`starting` / `running` / `cancelling` / `succeeded` / `failed` / `cancelled` / `timeout`。

示例（MCP 客户端侧）：

```json
{"workspace": "slot2", "prompt": "跑一遍 go test ./... 并总结失败原因", "apply": true, "timeout_sec": 900}
```

返回：

```json
{"ok": true, "job_id": "20261123-003500-slot2-a1b2c3", "status": "running", "...": "..."}
```

之后用 `dsh_status` 轮询，完成后再 `dsh_result` 验收。

---

## 四、启动

```powershell
# 1) 生成并设置 Token（至少 32 位随机串）
$env:DSH_MCP_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"

# 2) 启动（默认 127.0.0.1:8795）
.\tools\dsh_mcp\start.ps1

# 3) 另开窗口跑冒烟（会临时占用 8795，通过后自动拉起/停掉实例）
.\tools\dsh_mcp\start.ps1 -Check
```

也可以直接：

```powershell
python tools\dsh_mcp/server.py
python tools\dsh_mcp/server.py --check
python tools\dsh_mcp/server.py --list
```

`--check` 自动验证（27 项）：

1. 空 `DSH_MCP_TOKEN` 时进程拒绝启动（红线段）；
2. `GET /health` 返回 `{"ok": true}` 且无需 Token；
3. 无 Token `POST /mcp` -> `401` 且 `WWW-Authenticate` 指向保护的资源元数据；
4. 两个 `/.well-known/oauth-protected-resource` 路径与 `/.well-known/oauth-authorization-server`；
5. `POST /oauth/register`（DCR）-> `201`；
6. `GET /oauth/authorize` 授权页 -> `POST /oauth/authorize`（错误口令 401 / 正确口令 302+code）；
7. `POST /oauth/token`（code + PKCE S256）-> access/refresh，授权码一次性，refresh 轮换；
8. 用 OAuth `access_token` 调 `/mcp` 的 `tools/list` 成功；
9. 原 `DSH_MCP_TOKEN` 兼容、`dsh_list_workspaces`、`dsh_dispatch` dry-run、`ws_glob` 只读冒烟；
10. `ops/dsh_mcp_audit.jsonl` 有本次对应审计记录，且 `ws_glob` 只记 1 条（`ws_tools` 自审计，server 不重复记录）。

---

## 五、内置 OAuth 2.1 授权层（给 Gemini 等远程 MCP 客户端）

Gemini 的自定义 MCP 连接器**不支持手填静态 Bearer Token**，要求标准 OAuth。本网关已内置，
无需接任何第三方 IdP。

### 端点

| 端点 | 说明 |
|---|---|
| `GET /.well-known/oauth-protected-resource` | RFC 9728 受保护资源元数据（`/mcp` 变体也响应） |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 授权服务器元数据 |
| `POST /oauth/register` | RFC 7591 动态客户端注册（DCR） |
| `GET/POST /oauth/authorize` | 极简授权确认页，口令 = `DSH_MCP_TOKEN` |
| `POST /oauth/token` | `authorization_code`（PKCE S256）/ `refresh_token` |

### 流程

1. 客户端 `POST /mcp` 不带凭证 -> `401` + `WWW-Authenticate: Bearer resource_metadata="<origin>/.well-known/oauth-protected-resource"`；
2. 客户端读资源元数据 → 授权服务器元数据；
3. 客户端 `POST /oauth/register` 拿到 `client_id`；
4. 浏览器打开 `<origin>/oauth/authorize?...`，页面显示客户端名与回调地址；
   用户粘贴 `DSH_MCP_TOKEN` 点「允许授权」；
5. 302 回跳 `redirect_uri?code=...&state=...`；客户端用 `code + code_verifier` 换
   `access_token`（24h）+ `refresh_token`（30d，刷新时轮换）；
6. 客户端带 `Authorization: Bearer <access_token>` 调 `/mcp`。

### Origin 推导

元数据里的所有 URL 都按请求动态生成：

- 优先环境变量 `DSH_MCP_PUBLIC_URL`（如 `https://fixed.example.com`）；
- 否则用 `X-Forwarded-Proto`（cloudflared 会带 `https`）+ `X-Forwarded-Host` / `Host` 拼接。

所以 `trycloudflare.com` 域名每次重启变化时**不需要改任何配置**。

### 状态与有效期

- 状态文件：`runtime/dsh_mcp_oauth.json`（可用 `DSH_MCP_OAUTH_FILE` 覆盖；只存 token 的 SHA-256）。
- 授权码 **一次性、5 分钟**；access_token **24h**；refresh_token **30d** 且刷新时轮换。
- OAuth 签发的令牌与 `DSH_MCP_TOKEN` 并存——本地脚本仍可用原 Token，无需重新授权。

---

## 六、接入 Cloudflare Tunnel + Gemini「+ 自定义」关联应用

### 1. 快速隧道（单行命令）

```powershell
cloudflared tunnel --url http://127.0.0.1:8795
```

输出里会出现 `https://<random>.trycloudflare.com`。MCP 端点即：

```
https://<random>.trycloudflare.com/mcp
```

> 正式长期使用建议 named tunnel：在 `config.yml` 的 `ingress` 里把域名指到
> `http://127.0.0.1:8795`，然后 `cloudflared tunnel run <tunnel-name>`。
> 网关侧不需要改任何配置（Host 头校验默认关闭，鉴权走 OAuth/Bearer）。

### 2. Gemini 自定义关联应用

- 类型：**MCP / 自定义连接器 / Streamable HTTP**
- URL：`https://<tunnel-host>/mcp`（末尾必须带 `/mcp`）
- 鉴权：**不需要填静态 Token**；点「下一步」后：
  1. Gemini 自动走 OAuth 发现 + 动态注册；
  2. 浏览器跳到 `https://<tunnel-host>/oauth/authorize?...` 授权页；
  3. 在页面粘贴 `DSH_MCP_TOKEN` 点「允许授权」；
  4. 授权完成，回到 Gemini 保存即可。
- 连通性探测可直接访问 `https://<tunnel-host>/health`，应返回 `{"ok": true}`。

> 注意：`/health` 免鉴权只返回 `{"ok": true}`；其余路径无有效 Bearer 一律 401，
> 且 401 会带 `resource_metadata` 让客户端自动开始 OAuth。
> Cloudflare 侧如需额外访问策略（Access / Service Token），在隧道或 WAF 层加，
> 网关不感知。

---

## 七、运维与排障

| 现象 | 处理 |
|---|---|
| 启动立即退出、提示 `DSH_MCP_TOKEN 未设置` | 先 `$env:DSH_MCP_TOKEN=...`，再启动 |
| `--check` 报端口被占用 | 已有实例在跑；停掉后重试，或改 `--port`（地址仍固定 127.0.0.1） |
| Gemini 卡在 OAuth / 授权页打不开 | 看网关终端里 `[oauth]` 日志；确认 `X-Forwarded-Proto` 推导出的公网 URL 是 `https://` |
| 授权页口令一直不对 | 口令就是启动网关时的 `DSH_MCP_TOKEN`；重启网关时若换了 Token，要在授权页用新的 |
| 派活后 `dsh_status` 一直是 `failed`，`error` 含 `MISSING_CREDENTIAL` | headless 凭据 patch 未生效 / 账号登录失效；重新登录 DSH 或改用 API Key |
| `dsh_status` 显示 `failed` 且 `runner.log` 有报错 | 看 `runtime/dsh_jobs/<job_id>/runner.log` |
| 想急停 | `dsh_cancel(job_id)`；服务重启后遗留 job 会按 PID/exit.json 自动回收 |
| 审计文件太大 | 自行轮转 `ops/dsh_mcp_audit.jsonl`（网关只追加） |
| 本机代理导致 httpx 报 `Invalid port ':1]'` | 本机 `NO_PROXY` 含 `[::1]`，httpx 0.28 解析会崩；网关 `--check` 内部已用 `trust_env=False`，外部脚本自建客户端时同样处理 |

### 已知限制

- **安全边界**：目录白名单与敏感文件黑名单约束网关自己的 MCP 工具（`dsh_read_file`、
  `dsh_dispatch` 的 workspace 参数，以及全部 `ws_*`）。`dsh_dispatch` 拉起的是**完整 DSH 子代理**，它拥有
  DSH 自身的工具集与沙箱权限（默认 `workspace-write` 限制的是写，不是读/执行）。
  `ws_run_cmd` 同样不是沙箱：白名单内的解释器/构建工具可执行任意代码。
  若需要更强隔离，应把子代理改成只读权限或裁剪其工具集，而不是只依赖本网关。
- OAuth 的 `access_token`/`refresh_token` 持久化在 `runtime/dsh_mcp_oauth.json`；
  删除该文件会让已连接的客户端重新走一次授权（不影响 `DSH_MCP_TOKEN` 本地调用）。
- 授权页的“客户端名/回调地址”来自客户端的 DCR 请求，这里会原样展示；它是用户判断
  是否授权的依据，别在不可信环境下盲目点「允许授权」。
- job 的实时状态由本机文件（`exit.json` / PID）判定；网关进程重启后，正在跑的 job
  仍会被正确识别为 running（子进程不随 server 退出），完成后也能落 `exit.json`。
- `dsh_cancel` 依赖 Windows `taskkill /T /F`；被杀进程不会写 `exit.json`，
  由 server 直接标 `cancelled`。
- `dsh_result` 的 git diff（`git diff HEAD`）只覆盖已跟踪文件；未跟踪文件看 `status_short`。
- 子代理任务本身就是一次真实的 LLM 调用，会产生 token 消耗；`timeout_sec` 只限制墙钟时间。
- 本仓库的 `.gitignore` 已加入 `/ops/dsh_mcp_audit.jsonl`、`/runtime/dsh_jobs/`、
  `/runtime/dsh_mcp_oauth*.json`。
