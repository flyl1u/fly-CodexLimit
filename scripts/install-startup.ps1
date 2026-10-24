param(
    [string]$StateDirectory = "",
    [string]$PythonExecutable = "python",
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$taskName = 'CodexLimitGuard'
if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output 'Removed the CodexLimitGuard logon task.'
    exit 0
}
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskEntry = Join-Path $taskRoot 'guard.py'
$taskPython = (Get-Command $PythonExecutable -ErrorAction Stop).Source
if (-not $StateDirectory) {
    if ($env:CODEX_LIMIT_GUARD_HOME) { $StateDirectory = $env:CODEX_LIMIT_GUARD_HOME }
    elseif ($env:CODEX_HOME) { $StateDirectory = Join-Path $env:CODEX_HOME 'limit-guard' }
    else { $StateDirectory = Join-Path $env:USERPROFILE '.codex\limit-guard' }
}
$taskState = [System.IO.Path]::GetFullPath($StateDirectory)
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskScript = "& '" + $taskPython.Replace("'", "''") + "' '" + $taskEntry.Replace("'", "''") + "' --home '" + $taskState.Replace("'", "''") + "' daemon"
$taskEncoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($taskScript))
$taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $taskEncoded"
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Description 'Local Codex quota guard' -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Force | Out-Null
Write-Output 'Installed the current-user CodexLimitGuard logon task.'
