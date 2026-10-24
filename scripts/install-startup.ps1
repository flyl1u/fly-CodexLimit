param(
    [string]$StateDirectory = "$env:USERPROFILE\.codex\limit-guard",
    [string]$PythonExecutable = "python",
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$taskName = 'CodexLimitGuard'
if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output '已移除 CodexLimitGuard 登录启动任务。'
    exit 0
}
$taskLauncher = Join-Path $PSScriptRoot 'start-guard.ps1'
$taskPython = (Get-Command $PythonExecutable -ErrorAction Stop).Source
$taskState = [System.IO.Path]::GetFullPath($StateDirectory)
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskScript = "& '" + $taskLauncher.Replace("'", "''") + "' -StateDirectory '" + $taskState.Replace("'", "''") + "' -PythonExecutable '" + $taskPython.Replace("'", "''") + "'"
$taskEncoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($taskScript))
$taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $taskEncoded"
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Description '本地 Codex 额度保护守护程序' -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Force | Out-Null
Write-Output '已安装当前用户登录启动任务 CodexLimitGuard。'
