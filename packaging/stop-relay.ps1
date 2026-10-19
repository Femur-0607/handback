<#
.SYNOPSIS  Stop long-running relay processes of an installed handback.exe so setup can replace it.
.DESCRIPTION
    A Lead's Monitor runs `handback.exe inbox watch`, and `send --no-wait` starts a
    detached `handback.exe collect`. Started without a window or console of their own,
    they cannot be closed by the installer's Restart Manager, and the locked exe stops
    the upgrade (a silent in-app upgrade aborts).
    Stopping them loses nothing: unread mail stays in the inbox until acknowledged,
    recovery hooks ask the Lead to re-arm `inbox watch`, and a watcher or the router
    resumes collection. `route` (an external delivery that may be mid-send) and short
    hook runs are left alone.
.PARAMETER Exe  Full path of the installed handback.exe.
#>
param([Parameter(Mandatory)][string]$Exe)
$ErrorActionPreference = 'Stop'
$target = [IO.Path]::GetFullPath($Exe)
# The subcommand must follow the program path directly, so a --root path that
# merely contains the word "collect" never matches.
$pattern = '^\s*("[^"]*"|\S+)\s+(inbox\s+watch|collect)(\s|$)'
$stopped = @()
foreach ($process in Get-CimInstance Win32_Process -Filter "Name = 'handback.exe'") {
    if (-not $process.ExecutablePath -or -not $process.CommandLine) { continue }
    if ([IO.Path]::GetFullPath($process.ExecutablePath) -ne $target) { continue }  # -ne ignores case
    if ($process.CommandLine -notmatch $pattern) { continue }
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    $stopped += $process.ProcessId
}
foreach ($id in $stopped) { Wait-Process -Id $id -Timeout 10 -ErrorAction SilentlyContinue }
Write-Output "stopped $($stopped.Count) relay process(es): $($stopped -join ', ')"
exit 0
