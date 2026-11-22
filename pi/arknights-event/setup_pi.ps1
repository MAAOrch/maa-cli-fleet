# 把 MaaPiCli + MaaFw 运行时搭成这个 PI 项目的运行根：runtime\maa_cli_event
# 复用 runtime\maa_cli 里已经验过能跑的那套 DLL（同一台机、同一版 MaaFw）。
# 关键：config\maa_pi_config.json 的 adb.name 必须等于 MaaToolkitAdbDeviceFind 的名字
# （本机 MuMu：1-MuMuPlayer v5+），否则 MaaPiCli 拿不到 controller config 会 segfault。
param(
    [string]$SrcRuntime = (Join-Path $PSScriptRoot "..\..\runtime\maa_cli"),
    [string]$Out = (Join-Path $PSScriptRoot "..\..\runtime\maa_cli_event"),
    [string]$MaaModelDir = "D:\Xiach\M-A\MaaEnd\resource\model",
    [string]$AdbName = "1-MuMuPlayer v5+",
    [string]$AdbPath = "D:/Xiach/MuMuPlayer/nx_main/adb.exe",
    [string]$Address = "127.0.0.1:16384",
    [string]$TaskName = "收结算页",
    [string]$Event = "act46side"
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path (Join-Path $SrcRuntime "MaaPiCli.exe"))) {
    throw "找不到 $SrcRuntime\MaaPiCli.exe；先跑 pi\arknights-smoke\setup_pi.ps1 或指定 -SrcRuntime"
}
New-Item -ItemType Directory -Force $Out, (Join-Path $Out "config"), (Join-Path $Out "debug") | Out-Null

# 1) exe + dll：硬链接（同盘），失败就复制
Get-ChildItem $SrcRuntime -File | ForEach-Object {
    $target = Join-Path $Out $_.Name
    if (Test-Path $target) { Remove-Item $target -Force }
    try { New-Item -ItemType HardLink -Path $target -Target $_.FullName -ErrorAction Stop | Out-Null }
    catch { Copy-Item $_.FullName $target -Force }
}

# 2) 运行时目录 junction
$resourcePath = Join-Path $Out "resource"
if (Test-Path -LiteralPath $resourcePath) { cmd /c rmdir "$resourcePath" | Out-Null }
New-Item -ItemType Junction -Path $resourcePath -Target (Join-Path $PSScriptRoot "resource") | Out-Null
foreach ($name in @("MaaAgentBinary", "plugins")) {
    $src = Join-Path $SrcRuntime $name
    $dst = Join-Path $Out $name
    if (Test-Path $dst) { cmd /c rmdir "$dst" | Out-Null }
    if (Test-Path $src) {
        $real = (Get-Item $src).Target
        if (-not $real) { continue }
        New-Item -ItemType Junction -Path $dst -Target $real | Out-Null
    }
}

# 3) OCR 模型：MaaFw 从 <resource>\model\ocr\{det,rec}.onnx 读，项目里用 junction 复用
$modelDst = Join-Path $PSScriptRoot "resource\model"
if (Test-Path $modelDst) { cmd /c rmdir "$modelDst" | Out-Null }
if (Test-Path $MaaModelDir) {
    New-Item -ItemType Junction -Path $modelDst -Target $MaaModelDir | Out-Null
    Write-Host "OCR 模型 junction: $modelDst -> $MaaModelDir"
} else {
    Write-Warning "没找到 OCR 模型目录 $MaaModelDir；OCR 识别会失败（只剩 TemplateMatch）"
}

# 4) interface.json：先把活动层路径切到 -Event，再复制到运行根
if ($Event -notmatch '^[A-Za-z0-9_.-]+$') {
    throw "-Event 不合法：$Event（只允许字母/数字/下划线/点/连字符）"
}
$eventLayer = Join-Path $PSScriptRoot "resource\events\$Event"
if (-not (Test-Path (Join-Path $eventLayer "pipeline\event_override.json"))) {
    throw "找不到活动层 $eventLayer\pipeline\event_override.json；先运行 python pi\arknights-event\tools\gen_event_override.py --event $Event --prefix XX --s-count 5"
}
$interfacePath = Join-Path $PSScriptRoot "interface.json"
$itf = Get-Content -LiteralPath $interfacePath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $itf.resource -or $itf.resource.Count -lt 1) {
    throw "interface.json 缺 resource[0]，无法设置活动层"
}
$itf.resource[0].path = @("resource", "resource/common", "resource/events/$Event")
$itfJson = $itf | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($interfacePath, $itfJson + "`n", [System.Text.UTF8Encoding]::new($false))
Write-Host "interface.json 活动层: resource/events/$Event"
Copy-Item $interfacePath (Join-Path $Out "interface.json") -Force
$config = @{
    controller = @{ name = "Android" }
    adb        = @{ name = $AdbName; adb_path = $AdbPath; address = $Address }
    resource   = "Arknights"
    task       = @(@{ name = $TaskName; option = @() })
} | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText((Join-Path $Out "config\maa_pi_config.json"), $config + "`n", [System.Text.UTF8Encoding]::new($false))

Write-Host "PI 项目就绪：$Out"
Write-Host "跑默认任务：cd `"$Out`"; .\MaaPiCli.exe -d"

# 5) 自检：ROI 越界 / 模板缺失 / 节点引用错（AI 手写 JSON 最常见的静默错误）
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) {
    Write-Host "`n=== precheck ==="
    & $py.Source (Join-Path $PSScriptRoot "tools\precheck.py")
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "precheck 未通过（详见上面 error）；修完再跑任务，越界节点在运行时只会静默失败"
    }
} else {
    Write-Warning "没找到 python，跳过 precheck；建议手动跑 python pi\arknights-event\tools\precheck.py"
}
