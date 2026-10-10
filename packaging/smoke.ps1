<#
.SYNOPSIS  Smoke-test a built handback folder from a path containing spaces and non-ASCII characters.
.PARAMETER Folder       The built folder (containing handback.exe).
.PARAMETER Version      Expected version (checked against `handback.exe --version`).
.PARAMETER SkipVersion  Skip the --version check (code without that flag).
#>
param(
    [Parameter(Mandatory)][string]$Folder,
    [string]$Version,
    [switch]$SkipVersion
)
$ErrorActionPreference = 'Stop'
$korean = -join ([char]0xD55C, [char]0xAE00)
$base = Join-Path ([IO.Path]::GetTempPath()) ("hb test $korean " + [guid]::NewGuid().ToString('N').Substring(0, 8))
$app = Join-Path $base 'handback'
New-Item -ItemType Directory -Force -Path $base | Out-Null
try {
    Copy-Item -LiteralPath (Resolve-Path -LiteralPath $Folder).Path -Destination $app -Recurse
    $exe = Join-Path $app 'handback.exe'
    $dash = Join-Path $app 'handback-dashboard.exe'
    if (-not (Test-Path -LiteralPath $exe)) { throw 'handback.exe missing' }
    if (-not (Test-Path -LiteralPath $dash)) { throw 'handback-dashboard.exe missing' }

    function Step([string]$name, [scriptblock]$body) {
        Write-Host "== $name"
        & $body
        if ($LASTEXITCODE -ne 0) { throw "FAIL: $name (exit $LASTEXITCODE)" }
        Write-Host "PASS: $name"
    }

    if (-not $SkipVersion) {
        Write-Host '== --version'
        $out = (& $exe --version | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { throw "FAIL: --version (exit $LASTEXITCODE)" }
        if ($Version -and ($out -notmatch [regex]::Escape($Version))) { throw "FAIL: version '$out' does not match '$Version'" }
        Write-Host "PASS: --version ($out)"
    }
    Step '--help' { & $exe --help | Out-Null }

    $targetHome = Join-Path $base 'target home'
    Step 'install-skills --dry-run' { & $exe install-skills --dry-run --target-home $targetHome | Out-Null }
    if (Test-Path -LiteralPath $targetHome) { throw 'FAIL: dry-run created the target home' }

    Step 'setup --dry-run' { & $exe setup --dry-run | Out-Null }

    $state = Join-Path $base 'state home'
    Step 'hook (stdin JSON)' { '{}' | & $exe hook --agent claude --event Stop --state-home $state | Out-Null }

    Step 'dashboard --once' { & $exe dashboard --once | Out-Null }
    Write-Host 'SMOKE OK'
}
finally {
    Remove-Item -LiteralPath $base -Recurse -Force -ErrorAction SilentlyContinue
}
