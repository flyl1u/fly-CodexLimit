param(
    [string]$StateDirectory = "",
    [string]$PythonExecutable = "python"
)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskEntry = Join-Path $taskRoot 'guard.py'
if (-not $StateDirectory) {
    if ($env:CODEX_LIMIT_GUARD_HOME) { $StateDirectory = $env:CODEX_LIMIT_GUARD_HOME }
    elseif ($env:CODEX_HOME) { $StateDirectory = Join-Path $env:CODEX_HOME 'limit-guard' }
    else { $StateDirectory = Join-Path $env:USERPROFILE '.codex\limit-guard' }
}
$taskState = [System.IO.Path]::GetFullPath($StateDirectory)
New-Item -ItemType Directory -Path $taskState -Force | Out-Null
$taskPython = (Get-Command $PythonExecutable -ErrorAction Stop).Source
& $taskPython $taskEntry --home $taskState enable
if ($LASTEXITCODE -ne 0) { throw 'Guard is not installed; monitor was not started.' }
$taskArgs = '"' + $taskEntry + '" --home "' + $taskState + '" daemon'
$taskProcess = Start-Process -FilePath $taskPython -ArgumentList $taskArgs -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskState 'daemon.stdout.log') -RedirectStandardError (Join-Path $taskState 'daemon.stderr.log')
Write-Output "Guard process started: PID=$($taskProcess.Id), state=$taskState"
Write-Output "Check status: python `"$taskEntry`" --home `"$taskState`" status"
