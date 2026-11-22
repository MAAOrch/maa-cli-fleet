#requires -Version 5.1
<#
    Start the DSH remote MCP gateway (Streamable HTTP on 127.0.0.1:8795).

    The server refuses to start when DSH_MCP_TOKEN is empty.
    Usage:
        $env:DSH_MCP_TOKEN = "<at least 32 random chars>"
        .\tools\dsh_mcp\start.ps1            # start the server
        .\tools\dsh_mcp\start.ps1 -Check     # run local smoke checks
        .\tools\dsh_mcp\start.ps1 -Port 8795 # explicit port (address stays 127.0.0.1)
#>
param(
    [switch]$Check,
    [int]$Port = 8795,
    [switch]$NoReload
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$server = Join-Path $here 'server.py'

$py = $null
$cmd = Get-Command python -ErrorAction SilentlyContinue
if ($cmd) { $py = $cmd.Source }
if (-not $py) {
    Write-Host '[ERR] python.exe not found in PATH.' -ForegroundColor Red
    exit 2
}

if (-not $env:DSH_MCP_TOKEN) {
    Write-Host '[ERR] DSH_MCP_TOKEN is not set; refusing to start without auth.' -ForegroundColor Red
    Write-Host '      Example: $env:DSH_MCP_TOKEN = "<random 32+ chars>"' -ForegroundColor Yellow
    exit 2
}

$argv = @($server, '--port', "$Port")
if ($Check) { $argv += '--check' }
elseif (-not $NoReload) { $argv += '--reload' }

& $py @argv
exit $LASTEXITCODE
