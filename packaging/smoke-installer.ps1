<#
.SYNOPSIS  Silent install / verify / uninstall of the handback setup.exe inside a sandboxed profile.
.PARAMETER Setup    Path to handback-<ver>-setup.exe.
.PARAMETER Version  Expected version.
.PARAMETER Sandbox  Optional sandbox root (default: new temp folder). USERPROFILE/APPDATA/etc. are redirected into it.
#>
param(
    [Parameter(Mandatory)][string]$Setup,
    [Parameter(Mandatory)][string]$Version,
    [string]$Sandbox
)
$ErrorActionPreference = 'Stop'
$Setup = (Resolve-Path -LiteralPath $Setup).Path
if (-not $Sandbox) { $Sandbox = Join-Path ([IO.Path]::GetTempPath()) ('hb-inst-' + [guid]::NewGuid().ToString('N').Substring(0, 8)) }
$Sandbox = [IO.Path]::GetFullPath($Sandbox)
$prof = Join-Path $Sandbox 'profile'
$app = Join-Path $Sandbox 'app'
foreach ($d in $prof, "$prof\AppData\Roaming", "$prof\AppData\Local", "$prof\.handback", "$prof\.claude", "$prof\.codex") {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
$saved = @{}
$vars = @{
    USERPROFILE = $prof; APPDATA = "$prof\AppData\Roaming"; LOCALAPPDATA = "$prof\AppData\Local"
    HANDBACK_HOME = "$prof\.handback"; CLAUDE_CONFIG_DIR = "$prof\.claude"; CODEX_HOME = "$prof\.codex"
    GEMINI_HOME = "$prof\.gemini"
}
foreach ($k in $vars.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process'); [Environment]::SetEnvironmentVariable($k, $vars[$k], 'Process') }
try {
    Write-Host '== silent install'
    $p = Start-Process -FilePath $Setup -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=$app", '/TASKS=""') -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "FAIL: installer exit $($p.ExitCode)" }
    $exe = Join-Path $app 'handback.exe'
    if (-not (Test-Path -LiteralPath $exe)) { throw 'FAIL: handback.exe missing' }
    if (-not (Test-Path -LiteralPath (Join-Path $app 'handback-dashboard.exe'))) { throw 'FAIL: handback-dashboard.exe missing' }
    $out = (& $exe --version | Out-String).Trim()
    if ($out -notmatch [regex]::Escape($Version)) { throw "FAIL: version '$out' != '$Version'" }
    Write-Host "PASS: installed ($out)"
    $skill = Join-Path $prof '.claude\skills\handback\SKILL.md'
    if (-not (Test-Path -LiteralPath $skill)) { throw "FAIL: skill not created at $skill" }
    $text = [IO.File]::ReadAllText($skill, [Text.Encoding]::UTF8)
    if ($text.IndexOf($exe, [StringComparison]::OrdinalIgnoreCase) -lt 0 -and $text.IndexOf($exe.Replace('\', '/'), [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw "FAIL: SKILL.md does not reference $exe"
    }
    Write-Host 'PASS: skill installed and points at the installed exe'
    $unins = Join-Path $app 'unins000.exe'
    if (-not (Test-Path -LiteralPath $unins)) { throw 'FAIL: unins000.exe missing' }
    Write-Host '== silent uninstall'
    $p = Start-Process -FilePath $unins -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "FAIL: uninstaller exit $($p.ExitCode)" }
    # The uninstaller removes its own files via a helper process; allow a moment.
    for ($i = 0; $i -lt 30 -and (Test-Path -LiteralPath $app); $i++) { Start-Sleep -Milliseconds 500 }
    if (Test-Path -LiteralPath $app) { throw 'FAIL: app dir still present after uninstall' }
    Write-Host 'PASS: uninstalled'
    # The per-user PATH entry lives in the real HKCU and cannot be sandboxed, so it is not asserted here.
    if (Test-Path -LiteralPath $skill) { throw "FAIL: skill still present after uninstall: $skill" }
    Write-Host 'PASS: uninstall removed the skill'
    Write-Host 'INSTALLER SMOKE OK'
}
finally {
    foreach ($k in $vars.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
    Remove-Item -LiteralPath $Sandbox -Recurse -Force -ErrorAction SilentlyContinue
}
