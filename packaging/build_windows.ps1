# Build standalone Expense Automator on Windows (run from repo root in PowerShell)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$ver = python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$ver -lt [version]"3.10") {
    Write-Error "Python 3.10+ is required for this build. Found $ver"
}

$Venv = if ($env:VENV) { $env:VENV } else { Join-Path $Root ".venv-build" }
if (-not (Test-Path $Venv)) {
    python -m venv $Venv
}
$Activate = Join-Path $Venv "Scripts\Activate.ps1"
. $Activate

python -m pip install -q -U pip
python -m pip install -q -r requirements.txt -r requirements-build.txt

python (Join-Path $Root "packaging\generate_icons.py")

$BundleDir = Join-Path $Root "packaging\build\ms-playwright"
$env:PLAYWRIGHT_BROWSERS_PATH = $BundleDir
if (Test-Path $BundleDir) { Remove-Item -Recurse -Force $BundleDir }
New-Item -ItemType Directory -Force -Path $BundleDir | Out-Null
python -m playwright install chromium

if (Test-Path (Join-Path $Root "build")) { Remove-Item -Recurse -Force (Join-Path $Root "build") }
if (Test-Path (Join-Path $Root "dist")) { Remove-Item -Recurse -Force (Join-Path $Root "dist") }

pyinstaller (Join-Path $Root "packaging\expense_automator.spec") --noconfirm --clean

$Out = Join-Path $Root "dist\ExpenseAutomator\ExpenseAutomator.exe"
if (-not (Test-Path $Out)) {
    Write-Error "Expected $Out"
}

$WinBrowsers = Join-Path $Root "dist\ExpenseAutomator\ms-playwright"
if (Test-Path $WinBrowsers) { Remove-Item -Recurse -Force $WinBrowsers }
Copy-Item -Recurse -Force $BundleDir $WinBrowsers

Write-Host "Built: $Out"
Write-Host "Optional: install Inno Setup and run: iscc packaging\ExpenseAutomator.iss"
