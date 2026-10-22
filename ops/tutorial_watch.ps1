# tutorial_watch.ps1 - resident service: close Arknights tutorial popups on rogue-farming emulators.
# Watches every running maa.exe with "--batch run rogue_*", captures the device screen,
# template-matches the top-left [X close] button, and taps it. Safe: only taps when matched.
# Modes:
#   (default)        run service loop
#   -SelfTest -SelfTestImage <png>   score one image against the close-button template
#   -Probe           capture each live rogue device once and print match scores (verification)
# Deployed by ops\boot_tutorial_watch.ps1 (scheduled tasks FleetTutorialWatch-*).
param(
  [string]$Root = (Split-Path -Parent (Split-Path -Parent $PSCommandPath)),
  [int]$IntervalMs = 800,
  [double]$Threshold = 10.0,
  [switch]$SelfTest,
  [string]$SelfTestImage = '',
  [switch]$Probe
)
$ErrorActionPreference = 'Continue'
$CfgPath = Join-Path $Root 'runner\runner.json'
$Log     = Join-Path $Root 'runner\tutorial_watch.log'
$ShotDir = Join-Path $Root 'runner\tutorial_shots'
$StopFile= Join-Path $Root 'runner\tutorial_watch.stop'
$HbFile  = Join-Path $Root 'runner\tutorial_watch.hb'
$TplPath = Join-Path $Root 'ops\assets\tutorial_tpl_close.png'
if (-not (Test-Path $TplPath)) { $TplPath = Join-Path $Root 'runner\tutorial_tpl_close.png' }
$BaseX = 95; $BaseY = 85; $TapX = 152; $TapY = 109
$RefWidth = 1280

if (-not (Test-Path $TplPath)) { Write-Output ('TEMPLATE MISSING: ' + $TplPath); exit 2 }

Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies 'System.Drawing.dll' -TypeDefinition @"
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
public static class TutMatcher
{
    public static double Score(Bitmap frame, Bitmap tpl, int baseX, int baseY)
    {
        if (frame == null || tpl == null) { return 999.0; }
        double scale = (double)frame.Width / 1280.0;
        int w = (int)Math.Round(tpl.Width * scale);
        int h = (int)Math.Round(tpl.Height * scale);
        int x = (int)Math.Round(baseX * scale);
        int y = (int)Math.Round(baseY * scale);
        if (x < 0 || y < 0 || x + w > frame.Width || y + h > frame.Height) { return 999.0; }
        using (Bitmap crop = frame.Clone(new Rectangle(x, y, w, h), PixelFormat.Format24bppRgb))
        using (Bitmap scaled = new Bitmap(tpl.Width, tpl.Height, PixelFormat.Format24bppRgb))
        {
            using (Graphics g = Graphics.FromImage(scaled))
            {
                g.InterpolationMode = InterpolationMode.HighQualityBilinear;
                g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                g.DrawImage(crop, new Rectangle(0, 0, tpl.Width, tpl.Height));
            }
            return MeanDiff(scaled, tpl);
        }
    }
    static double MeanDiff(Bitmap a, Bitmap b)
    {
        BitmapData ad = a.LockBits(new Rectangle(0, 0, a.Width, a.Height), ImageLockMode.ReadOnly, PixelFormat.Format24bppRgb);
        BitmapData bd = b.LockBits(new Rectangle(0, 0, b.Width, b.Height), ImageLockMode.ReadOnly, PixelFormat.Format24bppRgb);
        try
        {
            long sum = 0; long n = 0;
            int cols = a.Width * 3;
            for (int yy = 0; yy < a.Height; yy++)
            {
                IntPtr ap = IntPtr.Add(ad.Scan0, yy * ad.Stride);
                IntPtr bp = IntPtr.Add(bd.Scan0, yy * bd.Stride);
                for (int xx = 0; xx < cols; xx++)
                {
                    int d = Marshal.ReadByte(ap, xx) - Marshal.ReadByte(bp, xx);
                    sum += d < 0 ? -d : d; n++;
                }
            }
            return (double)sum / n;
        }
        finally { a.UnlockBits(ad); b.UnlockBits(bd); }
    }
}
"@

$Tpl = [System.Drawing.Bitmap]::FromFile($TplPath)

function Get-EffThreshold([int]$width)
{
    if ($width -eq $RefWidth) { return $script:Threshold }
    return [Math]::Max($script:Threshold, 25.0)
}
function LogLine([string]$msg)
{
    $line = '[' + (Get-Date -Format 'MM-dd HH:mm:ss') + '] ' + $msg
    try { Add-Content -Path $Log -Value $line -Encoding UTF8 } catch { }
}
function Rotate-IfBig([string]$path, [long]$limitMB)
{
    try {
        if (Test-Path $path) {
            if ((Get-Item $path).Length -gt ($limitMB * 1MB)) {
                Move-Item -LiteralPath $path -Destination ($path + '.old') -Force
            }
        }
    } catch { }
}

if ($SelfTest) {
    if (-not $SelfTestImage) { Write-Output 'SELFTEST needs -SelfTestImage'; exit 2 }
    $frame = [System.Drawing.Bitmap]::FromFile($SelfTestImage)
    $score = [TutMatcher]::Score($frame, $Tpl, $BaseX, $BaseY)
    $eff = Get-EffThreshold $frame.Width
    Write-Output ('selftest image=' + $SelfTestImage + ' size=' + $frame.Width + 'x' + $frame.Height + ' score=' + [Math]::Round($score, 2) + ' threshold=' + $eff)
    if ($score -lt $eff) { Write-Output 'SELFTEST=POPUP' } else { Write-Output 'SELFTEST=NORMAL' }
    exit 0
}

$Cfg = $null
try { $Cfg = Get-Content -Path $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
$Adb = ''
if ($Cfg -and $Cfg.adbExe) { $Adb = [string]$Cfg.adbExe }
if (-not $Adb -or -not (Test-Path $Adb)) {
    $alt = Join-Path $Root 'bin\adb\adb.exe'
    if (Test-Path $alt) { $Adb = $alt }
}
if (-not (Test-Path $Adb)) { LogLine ('FATAL adb not found: ' + $Adb); exit 2 }

function Get-RoguePorts()
{
    try {
        return @(Get-CimInstance Win32_Process -Filter "Name='maa.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match '--batch\s+run\s+rogue_' } |
            ForEach-Object { if ($_.CommandLine -match '-a\s+127\.0\.0\.1:(\d+)') { $Matches[1] } } |
            Sort-Object -Unique)
    } catch { return @() }
}
function Capture-Bmp([string]$port)
{
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $script:Adb
    $psi.Arguments = '-s 127.0.0.1:' + $port + ' exec-out screencap -p'
    $psi.RedirectStandardOutput = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $proc = $null
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
        $ms = New-Object System.IO.MemoryStream
        $proc.StandardOutput.BaseStream.CopyTo($ms)
        $proc.WaitForExit()
        if ($ms.Length -lt 5000) { $ms.Dispose(); return $null }
        $ms.Position = 0
        $img = [System.Drawing.Image]::FromStream($ms)
        $bmp = New-Object System.Drawing.Bitmap($img)
        $img.Dispose(); $ms.Dispose()
        return $bmp
    } catch {
        try { if ($proc -and -not $proc.HasExited) { $proc.Kill() } } catch { }
        return $null
    }
}
function Tap-Close([string]$port, [double]$scale)
{
    $tx = [int][Math]::Round($TapX * $scale)
    $ty = [int][Math]::Round($TapY * $scale)
    & $script:Adb -s ('127.0.0.1:' + $port) shell input tap $tx $ty 2>&1 | Out-Null
}

if ($Probe) {
    $ports = @(Get-RoguePorts)
    Write-Output ('probe devices=' + $ports.Count)
    foreach ($port in $ports) {
        $bmp = Capture-Bmp $port
        if ($bmp -eq $null) { Write-Output ('port=' + $port + ' capture=FAIL'); continue }
        try {
            $score = [TutMatcher]::Score($bmp, $Tpl, $BaseX, $BaseY)
            Write-Output ('port=' + $port + ' size=' + $bmp.Width + 'x' + $bmp.Height + ' score=' + [Math]::Round($score, 2) + ' threshold=' + (Get-EffThreshold $bmp.Width))
        } finally { $bmp.Dispose() }
    }
    exit 0
}

New-Item -ItemType Directory -Force -Path $ShotDir | Out-Null
Rotate-IfBig $Log 8
LogLine ('watcher start pid=' + $PID + ' adb=' + $Adb + ' intervalMs=' + $IntervalMs + ' threshold=' + $Threshold)

$LastTap = @{}
$loop = 0
while (-not (Test-Path $StopFile)) {
    $loop++
    $ports = @(Get-RoguePorts)
    foreach ($port in $ports) {
        $bmp = Capture-Bmp $port
        if ($bmp -eq $null) { continue }
        try {
            $score = [TutMatcher]::Score($bmp, $Tpl, $BaseX, $BaseY)
            $eff = Get-EffThreshold $bmp.Width
            if ($score -lt $eff) {
                $scale = [double]$bmp.Width / [double]$RefWidth
                $now = [DateTime]::UtcNow
                $last = $script:LastTap[$port]
                if ((-not $last) -or (($now - $last).TotalSeconds -gt 2.5)) {
                    $shot = Join-Path $ShotDir ('popup_' + $port + '_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.png')
                    try { $bmp.Save($shot, [System.Drawing.Imaging.ImageFormat]::Png) } catch { }
                    Tap-Close $port $scale
                    $LastTap[$port] = $now
                    LogLine ('CLICK port=' + $port + ' score=' + [Math]::Round($score, 2) + ' shot=' + (Split-Path $shot -Leaf))
                }
            }
        } catch {
            LogLine ('ERROR port=' + $port + ' ' + $_.Exception.Message)
        } finally { $bmp.Dispose() }
    }
    try { Set-Content -Path $HbFile -Value ([DateTime]::UtcNow.ToString('o')) -Encoding ASCII } catch { }
    if ($loop % 200 -eq 0) {
        try {
            $files = @(Get-ChildItem $ShotDir -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
            if ($files.Count -gt 40) { $files | Select-Object -Skip 40 | ForEach-Object { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue } }
        } catch { }
        LogLine ('alive loop=' + $loop + ' devices=' + $ports.Count)
    }
    if ($ports.Count -eq 0) { Start-Sleep -Milliseconds ([Math]::Max($IntervalMs, 2000)) }
    else { Start-Sleep -Milliseconds $IntervalMs }
}
LogLine 'watcher stop (stop file present)'
