param(
    [string]$StateDirectory = "$env:USERPROFILE\.codex\limit-guard",
    [string]$PythonExecutable = "python"
)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskEntry = Join-Path $taskRoot 'guard.py'
$taskState = [System.IO.Path]::GetFullPath($StateDirectory)
New-Item -ItemType Directory -Path $taskState -Force | Out-Null
$taskPython = (Get-Command $PythonExecutable -ErrorAction Stop).Source
$taskArgs = '"' + $taskEntry + '" --home "' + $taskState + '" daemon'
$taskProcess = Start-Process -FilePath $taskPython -ArgumentList $taskArgs -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskState 'daemon.stdout.log') -RedirectStandardError (Join-Path $taskState 'daemon.stderr.log')
Write-Output "守护程序已启动：PID=$($taskProcess.Id)，状态目录=$taskState"
Write-Output "查看状态：python `"$taskEntry`" --home `"$taskState`" status"
