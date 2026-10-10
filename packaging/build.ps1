<#
.SYNOPSIS  Build the handback Windows onedir bundle, zip it, and write SHA256SUMS.txt.
.PARAMETER OutDir  Output directory (default: <repo>\dist-exe). Receives dist\, the zip and SHA256SUMS.txt.
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

# Zip with a top-level handback-<ver>/ folder.
$stage = Join-Path $work 'stage'
$top = Join-Path $stage "handback-$version"
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item -LiteralPath $bundle -Destination $top -Recurse
$zip = Join-Path $OutDir "handback-$version-windows-x64.zip"
Compress-Archive -LiteralPath $top -DestinationPath $zip -CompressionLevel Optimal

$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText((Join-Path $OutDir 'SHA256SUMS.txt'), "$hash  $(Split-Path $zip -Leaf)`n", (New-Object Text.UTF8Encoding($false)))

Write-Host "version=$version"
Write-Host "bundle=$bundle"
Write-Host "zip=$zip"
Write-Host "sha256=$hash"
