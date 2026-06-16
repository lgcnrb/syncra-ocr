$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

function Get-BuildPython {
    $venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        return $venvPython
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd -and $pythonCmd.Source -and $pythonCmd.Source -notlike "*WindowsApps*") {
        return $pythonCmd.Source
    }

    throw "No usable Python interpreter found. Activate your env or create .venv first."
}

$pythonExe = Get-BuildPython

Write-Host "Using Python: $pythonExe"
Write-Host ""

Write-Host "[1/4] Installing dependencies..."
& $pythonExe -m pip install --upgrade pip
& $pythonExe -m pip install -r requirements.txt
& $pythonExe -m pip install -r build_requirements.txt

Write-Host "[2/4] Building with PyInstaller..."
& $pythonExe -m PyInstaller --clean --noconfirm syncra.spec

$wrongExe = Join-Path $projectRoot "build\syncra\SyncraOCR.exe"
if (Test-Path $wrongExe) {
    Remove-Item $wrongExe -Force
}

Write-Host ""
Write-Host "[3/4] Cleaning build artifacts..."

Write-Host ""
Write-Host "[4/4] Build complete!"
Write-Host ""
Write-Host "  Output: dist\SyncraOCR\SyncraOCR.exe"
Write-Host ""
Write-Host "  Run only the EXE inside dist\SyncraOCR\."
Write-Host "  Do not run anything from the build\ folder."
Write-Host ""
