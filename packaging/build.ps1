<#
.SYNOPSIS  Build the handback Windows onedir bundle, compile the Inno Setup installer, and write SHA256SUMS.txt.
.PARAMETER OutDir  Output directory (default: <repo>\dist-exe). Receives dist\, handback-<ver>-setup.exe and SHA256SUMS.txt.
.PARAMETER Python  Python interpreter used to create the build venv (default: python on PATH).
#>
param(
    [string]$OutDir = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist-exe'),
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
function Run([string]$exe, [string[]]$a) {
    # Native stderr (pip/PyInstaller warnings) must not become terminating errors in Windows PowerShell 5.1.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $exe @a 2>&1 | ForEach-Object { "$_" } } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$exe $($a -join ' ') failed ($LASTEXITCODE)" }
}

$OutDir = [IO.Path]::GetFullPath($OutDir)
$work = Join-Path $OutDir 'work'
if (Test-Path -LiteralPath $OutDir) { Remove-Item -LiteralPath $OutDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $work | Out-Null

$initText = [IO.File]::ReadAllText((Join-Path $repo 'handback\__init__.py'), [Text.Encoding]::UTF8)
$m = [regex]::Match($initText, '__version__\s*=\s*"([^"]+)"')
if (-not $m.Success) { throw 'Could not read version' }
$version = $m.Groups[1].Value

$venv = Join-Path $work 'venv'
Run $Python @('-m', 'venv', $venv)
$py = Join-Path $venv 'Scripts\python.exe'
Run $py @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', (Join-Path $PSScriptRoot 'requirements-build.txt'))

$vdir = Join-Path $work 'version'
New-Item -ItemType Directory -Force -Path $vdir | Out-Null
Run $py @((Join-Path $PSScriptRoot 'version_info.py'), (Join-Path $vdir 'cli.txt'), 'handback', 'handback command line')
Run $py @((Join-Path $PSScriptRoot 'version_info.py'), (Join-Path $vdir 'dashboard.txt'), 'handback-dashboard', 'handback dashboard')

$env:HB_VERSION_DIR = $vdir
$dist = Join-Path $OutDir 'dist'
Run $py @('-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', $dist, '--workpath', (Join-Path $work 'pyi'),
          (Join-Path $PSScriptRoot 'handback.spec'))

$bundle = Join-Path $dist 'handback'
foreach ($n in 'handback.exe', 'handback-dashboard.exe') {
    if (-not (Test-Path -LiteralPath (Join-Path $bundle $n))) { throw "Missing $n in bundle" }
}

# Compile the installer with Inno Setup 6 (ISCC on PATH, Program Files, or a per-user winget install).
$iscc = $null
$cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($cmd) { $iscc = $cmd.Source }
foreach ($c in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
    if (-not $iscc -and (Test-Path -LiteralPath $c)) { $iscc = $c }
}
if (-not $iscc) { throw 'ISCC.exe (Inno Setup 6) not found. Install it: winget install JRSoftware.InnoSetup' }
$images = Join-Path $work 'wizard'
& (Join-Path $PSScriptRoot 'make-wizard-images.ps1') -OutDir $images
Run $iscc @("/DAppVersion=$version", "/DWizardImages=$images", "/DSourceDir=$bundle", "/DOutDir=$OutDir", (Join-Path $PSScriptRoot 'installer.iss'))
$setup = Join-Path $OutDir "handback-$version-setup.exe"
if (-not (Test-Path -LiteralPath $setup)) { throw "Installer not produced: $setup" }

$hash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText((Join-Path $OutDir 'SHA256SUMS.txt'), "$hash  $(Split-Path $setup -Leaf)`n", (New-Object Text.UTF8Encoding($false)))

Write-Host "version=$version"
Write-Host "bundle=$bundle"
Write-Host "setup=$setup"
Write-Host "sha256=$hash"
