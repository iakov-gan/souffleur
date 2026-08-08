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

$executable = Join-Path $PSScriptRoot "dist\souffleur\souffleur.exe"
if (-not (Test-Path $executable)) {
    throw "Build completed without producing dist\souffleur\souffleur.exe."
}

$startupOutput = & $executable --help 2>&1
if ($LASTEXITCODE -ne 0) {
    $startupText = $startupOutput -join [Environment]::NewLine
    throw "The generated executable failed its startup check: $startupText"
}

$archive = Join-Path $PSScriptRoot "dist\souffleur-windows.zip"
if (Test-Path $archive) {
    Remove-Item $archive
}
Compress-Archive -Path "dist\souffleur" -DestinationPath $archive

Write-Host "Built $executable"
Write-Host "Packaged $archive"
