# AI Factory launcher (Windows PowerShell). ASCII-only on purpose:
# Windows PowerShell 5.1 reads BOM-less files with the system code page.
param([int]$Port = 8000, [switch]$Fake)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPy = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    if (Test-Path ".venv") { Remove-Item -Recurse -Force ".venv" }
    Write-Host "Creating virtual env .venv ..."
    # Prefer the py launcher: 'python' may be the Microsoft Store alias stub.
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) {
        if (Test-Path ".venv") { Remove-Item -Recurse -Force ".venv" }
        throw "Could not create .venv. Install Python 3.11+ from python.org (tick 'Add python.exe to PATH')."
    }
}
& $venvPy -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

if ($Fake) { $env:FACTORY_FAKE = "1" } else { Remove-Item Env:FACTORY_FAKE -ErrorAction SilentlyContinue }

Start-Job { param($p) Start-Sleep 3; Start-Process "http://localhost:$p" } -ArgumentList $Port | Out-Null
Write-Host "AI Factory -> http://localhost:$Port  (Ctrl+C to stop)"
& $venvPy -m uvicorn backend.main:app --host 127.0.0.1 --port $Port --timeout-graceful-shutdown 5
