# AI Factory 一鍵啟動（Windows PowerShell）
param([int]$Port = 8000, [switch]$Fake)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "建立虛擬環境 .venv ..."
    python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt

if ($Fake) { $env:FACTORY_FAKE = "1" } else { Remove-Item Env:FACTORY_FAKE -ErrorAction SilentlyContinue }

Start-Job { param($p) Start-Sleep 2; Start-Process "http://localhost:$p" } -ArgumentList $Port | Out-Null
Write-Host "AI Factory → http://localhost:$Port  （Ctrl+C 結束）"
& .\.venv\Scripts\python.exe -m uvicorn backend.main:app --port $Port
