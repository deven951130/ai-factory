# Build the Windows desktop app (PyInstaller) and its installer (Inno Setup).
# ASCII-only on purpose: Windows PowerShell 5.1 reads BOM-less files with the system code page.
#   .\build.ps1                 -> dist\AIFactory\AIFactory.exe + dist\AIFactory-Setup-<version>.exe
#   .\build.ps1 -Version 0.2.0
param([string]$Version = "0.1.0")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = ".\.venv-build\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating build env .venv-build ..."
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv-build } else { python -m venv .venv-build }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $py)) { throw "Could not create .venv-build (need Python 3.11+)" }
}
& $py -m pip install -q --disable-pip-version-check -r packaging\requirements-desktop.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# Absolute paths: with --specpath, relative paths would resolve against build\.
$r = $PSScriptRoot
& $py -m PyInstaller --noconfirm --clean --windowed --name AIFactory `
    --icon "$r\packaging\app.ico" --paths "$r" `
    --add-data "$r\frontend;frontend" --add-data "$r\nodes.json;." `
    --distpath "$r\dist" --workpath "$r\build" --specpath "$r\build" `
    "$r\packaging\desktop.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "App: dist\AIFactory\AIFactory.exe"

$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
foreach ($d in "$env:LOCALAPPDATA\Programs\Inno Setup 6", "${env:ProgramFiles(x86)}\Inno Setup 6", "$env:ProgramFiles\Inno Setup 6") {
    if (-not $iscc -and (Test-Path "$d\ISCC.exe")) { $iscc = "$d\ISCC.exe" }
}
if (-not $iscc) {
    Write-Host "Inno Setup not found, installer skipped (winget install JRSoftware.InnoSetup)."
    exit 0
}
& $iscc /Q "/DAppVersion=$Version" packaging\installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Write-Host "Installer: dist\AIFactory-Setup-$Version.exe"
