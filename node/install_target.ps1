# ============================================================================
# install_target.ps1 —— 在【目标机】上布点机端（fleet.exe）
#
# 机端只有三件事：管理环境、执行任务、上报（2026-10-02 收敛定稿）。
# 常驻进程 = fleet.exe runner（读 runner\runner.json，调度 daily/ann/rogue 并上报 center）。
#
# 本脚本做四件事：
#   ① 拉项目包（GitHub zip：脚本/配置/文档）
#   ② 放 fleet.exe（项目根目录；没有就从 GitHub Release 下载）
#   ③ 写 runner\runner.json + runner\center.token（已有配置默认不覆盖，-Force 才重写）
#   ④ 注册计划任务（FleetRunner-AutoStart / FleetRunner-Watchdog）+ 立即拉起
#
# 用法（在目标机上跑；也可 ssh 进来跑）：
#   .\install_target.ps1 -NodeId node-01 -CenterUrl http://<后端>:8790 -Token <token>
#   .\install_target.ps1 ... -SkipStart        # 只布点，不起进程
#
# 注意：本脚本写于 2026-10-02 收敛时，未在全新机器上实测（UNVERIFIED）——
#       首次使用请按输出逐步核对；旧机重跑只会补缺，不会覆盖已有 runner.json。
# ============================================================================
param(
  [Parameter(Mandatory=$true)][string]$NodeId,
  [Parameter(Mandatory=$true)][string]$CenterUrl,
  [Parameter(Mandatory=$true)][string]$Token,
  [string]$ProjectDir   = (Join-Path $env:SystemDrive "maa-cli-fleet"),
  [string]$MumuManager  = "",
  [string]$AdbExe       = "",
  [string]$MaaExe       = "",
  [int]$Slots           = 4,
  [string]$TaskUser     = "$env:USERDOMAIN\$env:USERNAME",
  [string]$RepoZip      = "https://codeload.github.com/xiachk083-hub/maa-cli-fleet/zip/refs/heads/master",
  [string]$ExeUrl       = "https://github.com/xiachk083-hub/maa-cli-fleet/releases/latest/download/fleet.exe",
  [switch]$SkipStart,
  [switch]$Force
)
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

function Step($m) { Write-Host ("[install] " + $m) }

# ---- 0. 自动探测 MuMu / adb（也可用 -MumuManager / -AdbExe 显式指定） ----------
if (-not $MumuManager) {
  foreach ($cand in @(
    (Join-Path $env:SystemDrive "MuMu Player 12\nx_main\MuMuManager.exe"),
    (Join-Path $env:ProgramFiles "Netease\MuMuPlayer-12.0\nx_main\MuMuManager.exe")
  )) {
    if ($cand -and (Test-Path $cand)) { $MumuManager = $cand; break }
  }
}
if (-not $AdbExe -and $MumuManager) {
  $AdbExe = Join-Path (Split-Path (Split-Path $MumuManager -Parent) -Parent) "shell\adb.exe"
}
if (-not $MumuManager) { Step "!! 找不到 MuMuManager.exe：请用 -MumuManager <路径> 指定" }
if (-not $AdbExe)     { Step "!! 找不到 adb.exe：请用 -AdbExe <路径> 指定" }

# ---- ① 拉包（覆盖脚本/文档，不动本地配置与运行态） --------------------------
if (-not (Test-Path $ProjectDir)) { New-Item -ItemType Directory -Path $ProjectDir -Force | Out-Null }
$zip = Join-Path $ProjectDir "fleet.zip"
Step "拉包 $RepoZip"
Invoke-WebRequest -Uri $RepoZip -OutFile $zip -UseBasicParsing
Step ("已下载 " + (Get-Item $zip).Length + " 字节")
$x = Join-Path $ProjectDir "_x"
Expand-Archive -Path $zip -DestinationPath $x -Force
$inner = (Get-ChildItem $x -Directory | Select-Object -First 1).FullName
Copy-Item "$inner\*" $ProjectDir -Recurse -Force
Remove-Item $x -Recurse -Force
Remove-Item $zip -Force
Step "已解压到 $ProjectDir"

# ---- ② fleet.exe -----------------------------------------------------------
$exe = Join-Path $ProjectDir "fleet.exe"
if ((Test-Path $exe) -and (-not $Force)) {
  Step "已存在 $exe（保留；要换新加 -Force）"
} else {
  Step "下载 fleet.exe（GitHub Release latest）"
  Invoke-WebRequest -Uri $ExeUrl -OutFile $exe -UseBasicParsing
  Step ("fleet.exe " + (Get-Item $exe).Length + " 字节")
}

# ---- ③ 配置：runner\runner.json + center.token ------------------------------
if (-not $MaaExe) {
  foreach ($cand in @("$ProjectDir\bin\maa.exe")) {
    if (Test-Path $cand) { $MaaExe = $cand; break }
  }
}
if (-not $MaaExe) { Step "!! 找不到 maa.exe：先运行 fleet setup，或传 -MaaExe <路径>" }

$runnerDir = Join-Path $ProjectDir "runner"
if (-not (Test-Path $runnerDir)) { New-Item -ItemType Directory -Path $runnerDir -Force | Out-Null }

$tokenFile = Join-Path $runnerDir "center.token"
$Token | Set-Content -Path $tokenFile -Encoding ascii -NoNewline
Step "已写 $tokenFile"

$confPath = Join-Path $runnerDir "runner.json"
if ((Test-Path $confPath) -and (-not $Force)) {
  Step "已存在 runner.json（保留；要重写加 -Force）"
} else {
  $conf = [ordered]@{
    slots           = $Slots
    maxConcurrent   = $Slots
    mumuManager     = ($MumuManager -replace '\\','/')
    adbExe          = ($AdbExe -replace '\\','/')
    maaExe          = ($MaaExe -replace '\\','/')
    configDir       = (Join-Path $ProjectDir "config" -replace '\\','/')
    stateRoot       = (Join-Path $ProjectDir "data" -replace '\\','/')
    cacheDir        = (Join-Path $ProjectDir "data\cache" -replace '\\','/')
    logDir          = (Join-Path $ProjectDir "ops\logs" -replace '\\','/')
    logFile         = (Join-Path $ProjectDir "runner\runner.log" -replace '\\','/')
    stopFile        = (Join-Path $ProjectDir "runner\runner.stop" -replace '\\','/')
    accountsFile    = (Join-Path $ProjectDir "runner\accounts.json" -replace '\\','/')
    stateFile       = (Join-Path $ProjectDir "runner\state.json" -replace '\\','/')
    centerUrl       = $CenterUrl
    centerTokenFile = ($tokenFile -replace '\\','/')
    nodeId          = $NodeId
    taskTimeoutMin  = 75
    keepEmus        = @()
    minFreeRamMB    = 6000
    maxCpuPct       = 80
  }
  $conf | ConvertTo-Json -Depth 4 | Set-Content -Path $confPath -Encoding UTF8
  Step "已写 $confPath"
}

# ---- ④ 计划任务 + 拉起 -------------------------------------------------------
$boot = Join-Path $ProjectDir "ops\boot_runner.ps1"
if (-not (Test-Path $boot)) { Step "!! 缺 $boot（包里应该有）"; exit 1 }

$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
             -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"" + $boot + "`"")
$principal = New-ScheduledTaskPrincipal -UserId $TaskUser -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew

$t1 = New-ScheduledTaskTrigger -AtLogOn -User $TaskUser
Register-ScheduledTask -TaskName "FleetRunner-AutoStart" -Action $action -Trigger $t1 `
  -Principal $principal -Settings $settings -Force | Out-Null
Step "计划任务 FleetRunner-AutoStart 已注册"

$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "FleetRunner-Watchdog" -Action $action -Trigger $t2 `
  -Principal $principal -Settings $settings -Force | Out-Null
Step "计划任务 FleetRunner-Watchdog 已注册（每 10 分钟）"

if (-not $SkipStart) {
  & $boot
  Step "已调用 boot_runner.ps1（拉起 runner）"
}

Step "完成：nodeId=$NodeId -> $CenterUrl（账号表用 fleet gen 生成，任务文件在 config\tasks）"
