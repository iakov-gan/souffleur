param(
    [switch]$SkipInstall,
    [switch]$RequireInstaller
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

$isccCandidates = @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) }

$iscc = $isccCandidates | Select-Object -First 1
if ($iscc) {
    $version = python -c "import souffleur; print(souffleur.__version__)"
    & $iscc "/DMyAppVersion=$version" /Qp installer.iss
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup build failed."
    }
    Write-Host "Packaged $(Join-Path $PSScriptRoot 'dist\souffleur-setup.exe')"
} elseif ($RequireInstaller) {
    throw "Inno Setup 6 is required to build the installer."
} else {
    Write-Warning "Inno Setup 6 not found; skipped souffleur-setup.exe."
}
