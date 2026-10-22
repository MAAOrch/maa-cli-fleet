# boot_tutorial_watch.ps1 - idempotent starter / watchdog for tutorial_watch.ps1
# Called by scheduled tasks FleetTutorialWatch-AutoStart (AtLogon) and FleetTutorialWatch-Watchdog (every 10 min).
# -Status : print state only.
param([string]$Root = (Split-Path -Parent (Split-Path -Parent $PSCommandPath)), [switch]$Status)
$ErrorActionPreference = 'Continue'
$Ops  = Join-Path $Root 'ops'
$Main = Join-Path $Ops 'tutorial_watch.ps1'
$Log  = Join-Path $Ops 'boot_tutorial_watch.log'
$Stop = Join-Path $Root 'runner\tutorial_watch.stop'
$Hb   = Join-Path $Root 'runner\tutorial_watch.hb'

function RLog([string]$m)
{
    try { Add-Content -Path $Log -Value ('[' + (Get-Date -Format 'MM-dd HH:mm:ss') + '] ' + $m) -Encoding UTF8 } catch { }
}
function Get-Watchers()
{
    return @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'tutorial_watch\.ps1' -and $_.CommandLine -notmatch 'boot_tutorial_watch' })
}

if ($Status) {
    $w = @(Get-Watchers)
    Write-Output ('running=' + $w.Count)
    foreach ($p in $w) { Write-Output ('pid=' + $p.ProcessId) }
    if (Test-Path $Hb) { Write-Output ('hb_age_min=' + [Math]::Round(((Get-Date) - (Get-Item $Hb).LastWriteTime).TotalMinutes, 1)) } else { Write-Output 'hb=missing' }
    if (Test-Path $Stop) { Write-Output 'stop_file=present' } else { Write-Output 'stop_file=absent' }
    exit 0
}

if (-not (Test-Path $Main)) { RLog ('main script missing: ' + $Main); exit 2 }
if (Test-Path $Stop) { RLog 'stop file present -> skip'; exit 0 }

$running = @(Get-Watchers)
if ($running.Count -gt 0) {
    $stale = $false
    if (Test-Path $Hb) {
        $age = ((Get-Date) - (Get-Item $Hb).LastWriteTime).TotalMinutes
        if ($age -gt 5) { $stale = $true; RLog ('watcher pid=' + $running[0].ProcessId + ' heartbeat stale ' + [Math]::Round($age,1) + ' min -> restart') }
    } else {
        $stale = $true; RLog ('watcher pid=' + $running[0].ProcessId + ' heartbeat missing -> restart')
    }
    if ($stale) {
        foreach ($p in $running) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 2
    } else {
        RLog ('watcher already running pid=' + $running[0].ProcessId + ' (hb ' + [Math]::Round(((Get-Date) - (Get-Item $Hb).LastWriteTime).TotalMinutes, 1) + ' min), skip')
        exit 0
    }
}
$cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $Main + '"'
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
RLog ('watcher started pid=' + $r.ProcessId + ' rc=' + $r.ReturnValue)
