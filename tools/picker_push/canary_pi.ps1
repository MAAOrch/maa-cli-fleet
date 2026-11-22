# PI 任务真机 canary
# 用法：
#   pwsh -File tools/picker_push/canary_pi.ps1 -Account a01 -Entry "进入S-1详情"
param(
    [string]$Account = "a01",
    [string]$Entry = "进入S-1详情",
    [string]$StateDir = "."
)

$ErrorActionPreference = "Stop"
$q = Join-Path $StateDir "queue.in"
$kind = "pi.arknights-event.$Entry"
$line = @{op="enqueue"; kind=$kind; account=$Account} | ConvertTo-Json -Compress
Add-Content -Path $q -Value $line -Encoding UTF8
Write-Host "[已入队] $line"
Write-Host "请确保 fleet runner 正在跑，且 runner.json 已配置 piExe。"
Write-Host "观察日志确认任务被调度并执行。"
