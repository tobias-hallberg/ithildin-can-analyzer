# Local Windows build script.
#
# Usage from anywhere:
#     <repo>\packaging\build-windows.ps1
#
# Or, from inside the repo root:
#     .\packaging\build-windows.ps1
#
# Optional flags:
#     -BundleOnly   Skip the Inno Setup step (just produces dist\ithildin\).
#     -NoClean      Incremental rebuild (faster, but can mask cache bugs).

[CmdletBinding()]
param(
    [switch]$BundleOnly,
    [switch]$NoClean
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host ">> Repo:    $RepoRoot" -ForegroundColor Cyan

# Parse version from pyproject.toml -- same source of truth CI uses.
$versionLine = Select-String -Path "pyproject.toml" -Pattern '^version = "(.*)"' | Select-Object -First 1
if (-not $versionLine) {
    throw "Could not find version in pyproject.toml"
}
$Version = $versionLine.Matches[0].Groups[1].Value
Write-Host ">> Version: $Version" -ForegroundColor Cyan

Write-Host ">> uv sync --group packaging" -ForegroundColor Cyan
uv sync --group packaging
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

$pyiArgs = @("pyinstaller", "packaging/ithildin.spec", "--noconfirm")
if (-not $NoClean) { $pyiArgs += "--clean" }
Write-Host ">> uv run $($pyiArgs -join ' ')" -ForegroundColor Cyan
& uv run @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host ">> Bundle:  dist\ithildin\ithildin.exe" -ForegroundColor Green

if ($BundleOnly) {
    Write-Host ">> Done (bundle only)." -ForegroundColor Green
    return
}

# Locate Inno Setup. Skip the installer step if it's not installed --
# the bundle alone is already useful for smoke testing.
$ProgramFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
$IsccCandidates = @(
    (Join-Path $ProgramFilesX86 "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
)
$Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
    Write-Host ">> Inno Setup not found -- skipping installer step." -ForegroundColor Yellow
    Write-Host "   Install with:  winget install JRSoftware.InnoSetup" -ForegroundColor Yellow
    return
}

Write-Host ">> ISCC: $Iscc" -ForegroundColor Cyan
& $Iscc "/DAppVersion=$Version" "packaging\windows\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

$Installer = "dist\installer\Ithildin-$Version-Setup.exe"
Write-Host ">> Installer: $Installer" -ForegroundColor Green
