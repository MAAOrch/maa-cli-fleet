# 把 MaaPiCli + 依赖链接/复制到本目录。
# MaaPiCli 以 exe 所在目录为项目根，所以它必须和 interface.json / resource / config 同目录。
param(
    [string]$MaaBin = "$env:USERPROFILE\Downloads\MAA-win-x86_64-v5.10.4\bin"
)

$ErrorActionPreference = 'Stop'
$dest = $PSScriptRoot

if (-not (Test-Path (Join-Path $MaaBin 'MaaPiCli.exe'))) {
    throw "MaaPiCli.exe not found under $MaaBin; pass -MaaBin <path-to-bin>"
}

$linked = 0
$copied = 0
Get-ChildItem $MaaBin -File | ForEach-Object {
    $target = Join-Path $dest $_.Name
    if (Test-Path $target) { Remove-Item $target -Force }
    try {
        New-Item -ItemType HardLink -Path $target -Target $_.FullName -ErrorAction Stop | Out-Null
        $linked++
    }
    catch {
        Copy-Item $_.FullName $target -Force
        $copied++
    }
}

# MaaAgentBinary 可能不在 bin/，而在 MAA 包的 share/。
$agentCandidates = @(
    (Join-Path $MaaBin 'MaaAgentBinary'),
    (Join-Path (Split-Path $MaaBin -Parent) 'share\MaaAgentBinary')
)
$agentCopied = $false
foreach ($agent in $agentCandidates) {
    if (Test-Path $agent) {
        Copy-Item $agent (Join-Path $dest 'MaaAgentBinary') -Recurse -Force
        $agentCopied = $true
        break
    }
}
if (-not $agentCopied) {
    Write-Warning "MaaAgentBinary not found under $MaaBin or its ../share; ADB controller may fail to create."
}

Write-Host "Linked $linked files, copied $copied files from $MaaBin"
Write-Host "Next: copy config/maa_pi_config.example.json to config/maa_pi_config.json and edit adb_path/address."
