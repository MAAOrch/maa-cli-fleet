# ai_duty.ps1 — MAS AI 值班入口：把值班 prompt 交给 headless AI（OpenCode 默认 / dsh 备选）。
#
# 用法（本机，计划任务指向本脚本即可）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\maa-cli-fleet\ops\ai_duty.ps1
#   ... -Runtime dsh -PromptFile 自定义.md -LogFile 自定义.log -DryRun
#
# 说明：
#   * 默认 Runtime=opencode：opencode run --auto --file <prompt> <短消息>；
#     已有 opencode.json 的 auto-mas MCP 与 deepseek-v4.1-flash 凭据。
#     用 --file 而不是 stdin：Windows PowerShell 5.1 管道 stdin 会把中文按 GBK 传给子进程。
#   * Runtime=dsh：headless profile + ops\dsh-opencodego.patch.yml，从 ~/.dsh/.credentials.yaml
#     取 OPENCODE_GO_API_KEY 注入子进程（仅限 dsh 备选路径）。
#   * 输出追加到 dist\ai_daily\duty.log，stderr 追加 .err；失败只记日志并返回非 0。
param(
  [string]$PromptFile = "",
  [string]$LogFile = "",
  [ValidateSet("opencode", "dsh")][string]$Runtime = "opencode",
  [string]$Message = "按附件的值班 prompt 执行；先完整读附件，再行动，不要跳过。",
  [string]$DshExe = "",
  [string]$OpenCodeExe = "",
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $Root
if (-not $PromptFile) { $PromptFile = Join-Path $Root "ops\ai_duty_prompt.md" }
if (-not $LogFile)    { $LogFile    = Join-Path $Root "dist\ai_daily\duty.log" }
$ErrFile = [IO.Path]::ChangeExtension($LogFile, ".err")
$PatchFile = Join-Path $Root "ops\dsh-opencodego.patch.yml"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null

function Stamp { return (Get-Date -Format "yyyy-MM-dd HH:mm:ss") }
function Say([string]$line) { ("[{0}] {1}" -f (Stamp), $line) | Out-File -Append -Encoding utf8 $LogFile }

function Resolve-OpenCode {
  $cands = @(
    (Join-Path $env:APPDATA "npm\opencode.cmd"),
    (Join-Path $env:LOCALAPPDATA "Programs\opencode\opencode.exe")
  )
  foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return $c } }
  $cmd = Get-Command opencode -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  return $null
}

function Resolve-Dsh {
  $cmd = Get-Command dsh -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $cands = @(
    (Join-Path $env:LOCALAPPDATA "Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd"),
    (Join-Path $env:APPDATA "npm\dsh.cmd")
  )
  foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return $c } }
  return $null
}

function Get-OpenCodeKey {
  $p = Join-Path $env:USERPROFILE ".dsh\.credentials.yaml"
  if (-not (Test-Path $p)) { return $null }
  $hit = Select-String -Path $p -Pattern '^\s*OPENCODE_GO_API_KEY:\s*(.+)$' -Encoding UTF8 | Select-Object -First 1
  if (-not $hit) { return $null }
  $v = $hit.Matches[0].Groups[1].Value.Trim()
  return $v.Trim('"').Trim("'")
}

if (-not (Test-Path $PromptFile)) { Say ("PROMPT_MISSING {0}" -f $PromptFile); exit 2 }

# 单实例互斥：防止计划任务重叠触发两次值班
$script:DutyMutex = New-Object System.Threading.Mutex($false, "Global\MaaCliFleetAiDuty")
if (-not $script:DutyMutex.WaitOne(0)) {
  Say "SKIP already running"
  exit 0
}
try {
if ($Runtime -eq "opencode") {
  if (-not $OpenCodeExe) { $OpenCodeExe = Resolve-OpenCode }
  if (-not $OpenCodeExe) { Say "OPENCODE_MISSING"; exit 5 }
  if ($DryRun) { Say ("DRY_RUN runtime=opencode exe={0} prompt={1} msg={2}" -f $OpenCodeExe, $PromptFile, $Message); exit 0 }
  Say ("START runtime=opencode exe={0} prompt={1}" -f $OpenCodeExe, $PromptFile)
  $prev = $ErrorActionPreference
  try {
    # opencode 会往 stderr 打进度；Stop 会把 stderr 当异常，这里临时放宽
    $ErrorActionPreference = "Continue"
    $output = & $OpenCodeExe run --auto --file $PromptFile $Message 2>> $ErrFile
    $rc = $LASTEXITCODE
    $ErrorActionPreference = $prev
    Say ("OK runtime=opencode exit={0} lines={1}" -f $rc, @($output).Count)
    $output | Out-File -Append -Encoding utf8 $LogFile
    if ($rc -ne 0) { exit 1 }
  } catch {
    $ErrorActionPreference = $prev
    Say ("FAIL runtime=opencode {0}" -f $_.Exception.ToString())
    exit 1
  }
  exit 0
}

# ---- dsh 备选 ----
if (-not (Test-Path $PatchFile)) { Say ("PATCH_MISSING {0}" -f $PatchFile); exit 2 }
if (-not $DshExe) { $DshExe = Resolve-Dsh }
if (-not $DshExe) { Say "DSH_MISSING"; exit 3 }
$keySource = "env"
if (-not $env:OPENCODE_GO_API_KEY) {
  $key = Get-OpenCodeKey
  if (-not $key) { Say "CRED_MISSING OPENCODE_GO_API_KEY"; exit 4 }
  $env:OPENCODE_GO_API_KEY = $key
  $keySource = "credentials.yaml"
}
if ($DryRun) { Say ("DRY_RUN runtime=dsh dsh={0} prompt={1} patch={2} key={3}" -f $DshExe, $PromptFile, $PatchFile, $keySource); exit 0 }
Say ("START runtime=dsh dsh={0} prompt={1} key={2}" -f $DshExe, $PromptFile, $keySource)
$prev = $ErrorActionPreference
try {
  $ErrorActionPreference = "Continue"
  $prevOutEnc = [Console]::OutputEncoding
  $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
  $promptText = [System.IO.File]::ReadAllText($PromptFile, [System.Text.Encoding]::UTF8)
  $output = $promptText | & $DshExe --profile headless --patch $PatchFile - 2>> $ErrFile
  $rc = $LASTEXITCODE
  $ErrorActionPreference = $prev
  Say ("OK runtime=dsh exit={0} lines={1}" -f $rc, @($output).Count)
  $output | Out-File -Append -Encoding utf8 $LogFile
  if ($rc -ne 0) { exit 1 }
} catch {
  $ErrorActionPreference = $prev
  Say ("FAIL runtime=dsh {0}" -f $_.Exception.ToString())
  exit 1
}
exit 0
} finally {
  if ($script:DutyMutex) {
    try { $script:DutyMutex.ReleaseMutex() } catch {}
    $script:DutyMutex.Dispose()
  }
}
