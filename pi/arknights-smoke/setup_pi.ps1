# 把 MaaPiCli + MaaFw 运行时链接成一个 PI 项目根，供 `MaaPiCli.exe -d` 直接跑。
# 关键坑：config/maa_pi_config.json 的 adb.name 必须等于 MaaToolkitAdbDeviceFind 返回的名字，
# 否则 MaaPiCli 的 reconfig_adb 匹配失败，config 为空 -> MaaAdbControlUnitCreate 失败并 segfault。
# 本机 MuMu 的探测名：1-MuMuPlayer v5+
param(
    [string]$MaaBin = "D:\Xiach\M-A\MaaEnd\maafw",
    [string]$Out = (Join-Path $PSScriptRoot "..\..\runtime\maa_cli"),
    [string]$AdbName = "1-MuMuPlayer v5+",
    [string]$AdbPath = "D:/Xiach/MuMuPlayer/nx_main/adb.exe",
    [string]$Address = "127.0.0.1:16384"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path (Join-Path $MaaBin "MaaPiCli.exe"))) {
    throw "MaaPiCli.exe not found under $MaaBin; pass -MaaBin <MaaFw bin dir>"
}
New-Item -ItemType Directory -Force $Out, (Join-Path $Out "config"), (Join-Path $Out "debug") | Out-Null

# exe + DLL：同盘硬链接，跨盘退化为复制
Get-ChildItem $MaaBin -File | ForEach-Object {
    $target = Join-Path $Out $_.Name
    if (Test-Path $target) { Remove-Item $target -Force }
    try { New-Item -ItemType HardLink -Path $target -Target $_.FullName -ErrorAction Stop | Out-Null }
    catch { Copy-Item $_.FullName $target -Force }
}

# 运行时目录：MaaAgentBinary / plugins 用 junction
foreach ($name in @("MaaAgentBinary", "plugins")) {
    $src = Join-Path $MaaBin $name
    $dst = Join-Path $Out $name
    if (-not (Test-Path $src)) { continue }
    if (-not (Test-Path $dst)) { New-Item -ItemType Junction -Path $dst -Target $src | Out-Null }
}

# resource 指回 pi/arknights-smoke/resource，避免复制模板
$resourcePath = Join-Path $Out "resource"
if (-not (Test-Path -LiteralPath $resourcePath)) {
    New-Item -ItemType Junction -Path $resourcePath -Target (Join-Path $PSScriptRoot "resource") | Out-Null
}
Copy-Item (Join-Path $PSScriptRoot "interface.json") (Join-Path $Out "interface.json") -Force

$config = @{
    controller = @{ name = "Android" }
    adb        = @{ name = $AdbName; adb_path = $AdbPath; address = $Address }
    resource   = "Arknights"
    task       = @(@{ name = "Observe"; option = @() })
} | ConvertTo-Json -Depth 6
$configPath = Join-Path $Out "config\maa_pi_config.json"
[System.IO.File]::WriteAllText($configPath, $config + "`n", [System.Text.UTF8Encoding]::new($false))

Write-Host "PI project ready: $Out"
Write-Host "Run: cd `"$Out`"; .\MaaPiCli.exe -d"
