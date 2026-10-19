<#
.SYNOPSIS  Silent install / verify / uninstall of the handback setup.exe inside a sandboxed profile.
.PARAMETER Setup    Path to handback-<ver>-setup.exe.
.PARAMETER Version  Expected version.
.PARAMETER Sandbox  Optional sandbox root (default: new temp folder). USERPROFILE/APPDATA/etc. are redirected into it.
.PARAMETER AllowRealProfile
    Run even though handback is installed for this user. Setup writes its uninstall entry and the user
    PATH in the real HKCU whatever the sandbox, so the smoke would replace and then delete an existing
    install's entry. With this switch it backs them up and restores them afterwards; without it, it
    refuses. (Shortcuts follow the redirected USERPROFILE into the sandbox; the real ones are still compared.)
#>
param(
    [Parameter(Mandatory)][string]$Setup,
    [Parameter(Mandatory)][string]$Version,
    [string]$Sandbox,
    [switch]$AllowRealProfile
)
$ErrorActionPreference = 'Stop'

# Locations outside the sandbox: the first two are written by setup and uninstall, the shortcuts are
# a guard in case Inno stops following USERPROFILE. Resolve the folders before the profile variables
# are redirected so they name the real profile.
$realParts = @(
    @{ Name = 'uninstall entry'; Key = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F1C2A58-3B7E-4D0A-9C41-7A5E8B2D1F63}_is1' }
    @{ Name = 'user PATH'; Key = 'Environment'; Value = 'Path' }
    @{ Name = 'Start menu shortcut'; File = Join-Path ([Environment]::GetFolderPath('Programs')) 'handback.lnk' }
    @{ Name = 'desktop shortcut'; File = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'handback.lnk' }
)

function Format-RegValue($kind, $value) {
    if ($value -is [array]) { $value = ($value | ForEach-Object { "$_" }) -join ',' }
    return "${kind}:$value"
}

function Read-RealPart($part) {
    # Returns @{ Data = <what Write-RealPart needs, $null when absent>; Text = <comparable form> }.
    if ($part.File) {
        if (-not (Test-Path -LiteralPath $part.File)) { return @{ Data = $null; Text = '(absent)' } }
        $bytes = [IO.File]::ReadAllBytes($part.File)
        $hash = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes))
        return @{ Data = $bytes; Text = $hash }
    }
    $k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($part.Key)
    if (-not $k) { return @{ Data = $null; Text = '(absent)' } }
    try {
        # Keep the key's own value order for restoring; compare sorted, since only the values matter.
        $names = if ($part.Value) { @($k.GetValueNames() | Where-Object { $_ -eq $part.Value }) } else { @($k.GetValueNames()) }
        if ($part.Value -and -not $names) { return @{ Data = $null; Text = '(absent)' } }
        $values = foreach ($n in $names) {
            [pscustomobject]@{ Name = $n; Kind = $k.GetValueKind($n)
                Value = $k.GetValue($n, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) }
        }
        return @{ Data = @($values); Text = (@($values | ForEach-Object { "$($_.Name)=$(Format-RegValue $_.Kind $_.Value)" } | Sort-Object) -join "`n") }
    } finally { $k.Close() }
}

function Write-RealPart($part, $data) {
    if ($part.File) {
        if ($null -eq $data) { Remove-Item -LiteralPath $part.File -Force -ErrorAction SilentlyContinue; return }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $part.File) | Out-Null
        [IO.File]::WriteAllBytes($part.File, $data)
        return
    }
    $cu = [Microsoft.Win32.Registry]::CurrentUser
    if (-not $part.Value) { $cu.DeleteSubKeyTree($part.Key, $false) }
    if ($null -eq $data -and $part.Value) {
        $k = $cu.OpenSubKey($part.Key, $true)
        if ($k) { try { $k.DeleteValue($part.Value, $false) } finally { $k.Close() } }
        return
    }
    if ($null -eq $data) { return }
    $k = $cu.CreateSubKey($part.Key)
    try { foreach ($v in $data) { $k.SetValue($v.Name, $v.Value, $v.Kind) } } finally { $k.Close() }
}

$before = @{}
foreach ($part in $realParts) { $before[$part.Name] = Read-RealPart $part }
if ($null -ne $before['uninstall entry'].Data -and -not $AllowRealProfile) {
    throw ('handback is installed for this user. Setup writes its uninstall entry outside the sandbox, so the ' +
        'smoke would repoint it at the sandbox and then delete it, leaving the installed copy unregistered. ' +
        'Rerun with -AllowRealProfile to back it up and restore it afterwards.')
}

$Setup = (Resolve-Path -LiteralPath $Setup).Path
if (-not $Sandbox) { $Sandbox = Join-Path ([IO.Path]::GetTempPath()) ('hb-inst-' + [guid]::NewGuid().ToString('N').Substring(0, 8)) }
$Sandbox = [IO.Path]::GetFullPath($Sandbox)
$prof = Join-Path $Sandbox 'profile'
$app = Join-Path $Sandbox 'app'
$bin = Join-Path $Sandbox 'bin'
foreach ($d in $prof, "$prof\AppData\Roaming", "$prof\AppData\Local", "$prof\.handback", "$prof\.claude", "$prof\.codex", $bin) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
# The installer's `setup --yes` installs skills only when Claude is detected (`claude --version`
# exits 0). CI runners have no Claude, so point detection at a stand-in that only answers --version.
$fakeClaude = Join-Path $bin 'claude.cmd'
[IO.File]::WriteAllText($fakeClaude, "@echo off`r`necho 0.0.0 (handback smoke stand-in)`r`n", [Text.Encoding]::ASCII)
$saved = @{}
$vars = @{
    USERPROFILE = $prof; APPDATA = "$prof\AppData\Roaming"; LOCALAPPDATA = "$prof\AppData\Local"
    HANDBACK_HOME = "$prof\.handback"; CLAUDE_CONFIG_DIR = "$prof\.claude"; CODEX_HOME = "$prof\.codex"
    GEMINI_HOME = "$prof\.gemini"; HANDBACK_CLAUDE = $fakeClaude
}
foreach ($k in $vars.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process'); [Environment]::SetEnvironmentVariable($k, $vars[$k], 'Process') }

function Wait-Exit([Diagnostics.Process]$process, [string]$what, [int]$seconds) {
    # A [Run]/[UninstallRun] child that blocks (e.g. waiting for a key on a hidden console)
    # would otherwise hang the job until its global timeout; fail fast with a clear message.
    if (-not $process.WaitForExit($seconds * 1000)) {
        Get-Process -Name 'handback*' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "FAIL: $what did not finish within ${seconds}s"
    }
    return $process.ExitCode
}

try {
    Write-Host '== silent install'
    $p = Start-Process -FilePath $Setup -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=$app", '/TASKS=""') -PassThru
    $code = Wait-Exit $p 'installer' 600
    if ($code -ne 0) { throw "FAIL: installer exit $code" }
    $exe = Join-Path $app 'handback.exe'
    if (-not (Test-Path -LiteralPath $exe)) { throw 'FAIL: handback.exe missing' }
    if (-not (Test-Path -LiteralPath (Join-Path $app 'handback-dashboard.exe'))) { throw 'FAIL: handback-dashboard.exe missing' }
    $out = (& $exe --version | Out-String).Trim()
    if ($out -notmatch [regex]::Escape($Version)) { throw "FAIL: version '$out' != '$Version'" }
    Write-Host "PASS: installed ($out)"
    # Confirm setup wrote the entry this script restores (so that check is not vacuous) and put the
    # shortcut in the sandbox profile rather than the real one.
    $appPath = @((Read-RealPart $realParts[0]).Data | Where-Object { $_.Name -eq 'Inno Setup: App Path' }).Value
    if ($appPath -ne $app) { throw "FAIL: uninstall entry points at '$appPath', not $app" }
    $shortcut = "$prof\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\handback.lnk"
    if (-not (Test-Path -LiteralPath $shortcut)) { throw "FAIL: Start menu shortcut not created in the sandbox at $shortcut" }
    Write-Host 'PASS: uninstall entry points at the sandbox and the shortcut stayed in it'
    $skill = Join-Path $prof '.claude\skills\handback\SKILL.md'
    if (-not (Test-Path -LiteralPath $skill)) { throw "FAIL: skill not created at $skill" }
    $text = [IO.File]::ReadAllText($skill, [Text.Encoding]::UTF8)
    if ($text.IndexOf($exe, [StringComparison]::OrdinalIgnoreCase) -lt 0 -and $text.IndexOf($exe.Replace('\', '/'), [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw "FAIL: SKILL.md does not reference $exe"
    }
    Write-Host 'PASS: skill installed and points at the installed exe'
    Write-Host '== upgrade while a Lead watcher holds handback.exe'
    # Restart Manager cannot close a Monitor's `inbox watch`; PrepareToInstall must stop
    # it or the locked exe aborts the upgrade.
    $watchRoot = Join-Path $Sandbox 'watch-root'
    New-Item -ItemType Directory -Force -Path $watchRoot | Out-Null
    # Start it with no console at all (DETACHED_PROCESS, as a Monitor's host shell or
    # `send --no-wait` does). With any console, even a hidden one, Restart Manager can
    # close it and the old installer would pass; detached, it aborted with exit 5.
    $spawn = 'import subprocess, sys; f = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP; ' +
        'print(subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, ' +
        'stderr=subprocess.DEVNULL, creationflags=f).pid)'
    $watchPid = [int](& python -c $spawn $exe inbox watch --root $watchRoot --for claude:smoke --no-collect --idle-exit 600)
    $watch = Get-Process -Id $watchPid
    Start-Sleep -Seconds 3
    if ($watch.HasExited) { throw 'FAIL: smoke watcher exited early' }
    $other = Join-Path $Sandbox 'elsewhere\handback.exe'
    $kept = (& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'stop-relay.ps1') -Exe $other | Out-String).Trim()
    if ($watch.HasExited -or $kept -notmatch '^stopped 0 ') { throw "FAIL: stop-relay touched a watcher of another install ($kept)" }
    $log = Join-Path $Sandbox 'upgrade.log'
    $p = Start-Process -FilePath $Setup -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS',
        "/DIR=$app", '/TASKS=""', "/LOG=`"$log`"") -PassThru
    $code = Wait-Exit $p 'upgrade installer' 600
    if (-not $watch.WaitForExit(5000)) { Stop-Process -Id $watch.Id -Force; throw "FAIL: watcher still running after upgrade (setup exit $code)" }
    if ($code -ne 0) { throw "FAIL: upgrade installer exit $code" }
    $text = [IO.File]::ReadAllText($log)
    # The log shows whether Restart Manager still met a process that holds our files.
    foreach ($bad in 'RestartManager found an application', 'could not be shut down', 'DeleteFile failed', 'MoveFile failed') {
        if ($text.Contains($bad)) { throw "FAIL: upgrade log reports '$bad':`n$text" }
    }
    if (-not $text.Contains('stop-relay.ps1 exit code 0')) { throw "FAIL: stop-relay.ps1 did not run:`n$text" }
    $out = (& $exe --version | Out-String).Trim()
    if ($out -notmatch [regex]::Escape($Version)) { throw "FAIL: version after upgrade '$out' != '$Version'" }
    Write-Host 'PASS: upgrade stopped the watcher and replaced handback.exe'
    $unins = Join-Path $app 'unins000.exe'
    if (-not (Test-Path -LiteralPath $unins)) { throw 'FAIL: unins000.exe missing' }
    Write-Host '== silent uninstall'
    $p = Start-Process -FilePath $unins -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -PassThru
    $code = Wait-Exit $p 'uninstaller' 300
    if ($code -ne 0) { throw "FAIL: uninstaller exit $code" }
    # The uninstaller removes its own files via a helper process; allow a moment.
    for ($i = 0; $i -lt 30 -and (Test-Path -LiteralPath $app); $i++) { Start-Sleep -Milliseconds 500 }
    if (Test-Path -LiteralPath $app) { throw 'FAIL: app dir still present after uninstall' }
    Write-Host 'PASS: uninstalled'
    if ($null -ne (Read-RealPart $realParts[0]).Data) { throw 'FAIL: uninstall entry still present after uninstall' }
    if (Test-Path -LiteralPath $shortcut) { throw "FAIL: shortcut still present after uninstall: $shortcut" }
    $pathText = "$(@((Read-RealPart $realParts[1]).Data).Value)"
    if ((';' + $pathText + ';').IndexOf(';' + $app + ';', [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        throw "FAIL: user PATH still contains $app"
    }
    Write-Host 'PASS: uninstall removed its entry, shortcut and PATH entry'
    if (Test-Path -LiteralPath $skill) { throw "FAIL: skill still present after uninstall: $skill" }
    Write-Host 'PASS: uninstall removed the skill'
}
finally {
    foreach ($k in $vars.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
    # Put back whatever this run changed outside the sandbox (an existing install's entry and
    # shortcut, or leftovers of a run that failed half way), then check nothing differs.
    $changed = @()
    foreach ($part in $realParts) {
        if ((Read-RealPart $part).Text -ne $before[$part.Name].Text) {
            try { Write-RealPart $part $before[$part.Name].Data } catch { Write-Warning "could not restore $($part.Name): $_" }
            $changed += $part.Name
        }
    }
    $restoreErrors = @($realParts | Where-Object { (Read-RealPart $_).Text -ne $before[$_.Name].Text } | ForEach-Object { $_.Name })
    if ($changed) { Write-Host "restored: $($changed -join ', ')" }
    if ($restoreErrors) { Write-Warning "FAIL: real profile differs from before the smoke: $($restoreErrors -join ', ')" }
    Remove-Item -LiteralPath $Sandbox -Recurse -Force -ErrorAction SilentlyContinue
}
if ($restoreErrors) { throw "FAIL: real profile differs from before the smoke: $($restoreErrors -join ', ')" }
Write-Host 'PASS: real uninstall entry, shortcuts and user PATH match the state before the smoke'
Write-Host 'INSTALLER SMOKE OK'
