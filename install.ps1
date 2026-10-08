# Compatibility wrapper; all detection and writes live in the Python installer.
[CmdletBinding()]
param([switch]$DryRun, [string]$TargetHome)
$ErrorActionPreference = 'Stop'
$relayArgs = @((Join-Path $PSScriptRoot 'handback.py'), 'install-skills')
if ($DryRun) { $relayArgs += '--dry-run' }
if ($PSBoundParameters.ContainsKey('TargetHome')) { $relayArgs += @('--target-home', $TargetHome) }
& python @relayArgs
exit $LASTEXITCODE
