# install_maa_tasks.ps1 — 把 config/maa 下的自定义 MAA 任务/模板装进一个 MAA resource。
#
# 用法（本机或目标机）：
#   powershell -ExecutionPolicy Bypass -File tools\install_maa_tasks.ps1 -DataRoot E:\maa-cli-fleet\data_fresh
#
# 只增/覆盖 MAAOrch 目录，不动官方 resource 的其它文件；MAA 升级 resource 后重跑一次即可。
param(
  [Parameter(Mandatory = $true)][string]$DataRoot
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root "config\maa"
if (-not (Test-Path $src)) { throw "没有 $src" }

$dstTasks = Join-Path $DataRoot "resource\tasks"
$dstTpl = Join-Path $DataRoot "resource\template"
New-Item -ItemType Directory -Force -Path (Join-Path $dstTasks "MAAOrch"), (Join-Path $dstTpl "MAAOrch") | Out-Null

$n = 0
Get-ChildItem $src -Recurse -File -Filter *.json | ForEach-Object {
  $rel = $_.FullName.Substring($src.Length).TrimStart('\', '/')
  $to = Join-Path $dstTasks $rel
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
  Copy-Item $_.FullName $to -Force
  $n++
}
Get-ChildItem $src -Recurse -File -Filter *.png | ForEach-Object {
  $rel = $_.FullName.Substring($src.Length).TrimStart('\', '/')
  $to = Join-Path $dstTpl $rel
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
  Copy-Item $_.FullName $to -Force
  $n++
}
Write-Output "installed $n files -> $DataRoot\resource"
