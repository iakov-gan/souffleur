param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $SkipInstall) {
    python -m pip install --upgrade ".[build]"
}

python -m PyInstaller --noconfirm --clean souffleur.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

$executable = Join-Path $PSScriptRoot "dist\souffleur.exe"
if (-not (Test-Path $executable)) {
    throw "Build completed without producing dist\souffleur.exe."
}

$startupOutput = & $executable --help 2>&1
if ($LASTEXITCODE -ne 0) {
    $startupText = $startupOutput -join [Environment]::NewLine
    if ($startupText -match "Application Control policy has blocked") {
        Write-Warning (
            "Executable built, but this PC blocks PyInstaller one-file " +
            "temporary extraction. GitHub Actions will run the startup check."
        )
    } else {
        throw "The generated executable failed its startup check: $startupText"
    }
}

Write-Host "Built $executable"
