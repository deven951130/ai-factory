# AI Factory launcher (Windows PowerShell). ASCII-only on purpose:
# Windows PowerShell 5.1 reads BOM-less files with the system code page.
param([int]$Port = 8000, [switch]$Fake)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual env .venv ..."
    python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

if ($Fake) { $env:FACTORY_FAKE = "1" } else { Remove-Item Env:FACTORY_FAKE -ErrorAction SilentlyContinue }

Start-Job { param($p) Start-Sleep 3; Start-Process "http://localhost:$p" } -ArgumentList $Port | Out-Null
Write-Host "AI Factory -> http://localhost:$Port  (Ctrl+C to stop)"
& .\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port $Port
