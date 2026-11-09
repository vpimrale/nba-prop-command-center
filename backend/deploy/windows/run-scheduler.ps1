# NOT REGISTERED AUTOMATICALLY. Runs the paper-trading scheduler on a Windows PC.
# Loads backend\.env (git-ignored; holds PROPLINE_API_KEY etc.) into this process only, then
# runs the loop. The loop is restart-safe: Task Scheduler restarts it, and it catches up.
$ErrorActionPreference = "Stop"
$backend = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $backend
$envFile = Join-Path $backend ".env"
if (Test-Path $envFile) {
  Get-Content $envFile | Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2
    $v = $v.Trim()
    if ($v.Length -ge 2 -and (($v[0] -eq '"' -and $v[-1] -eq '"') -or ($v[0] -eq "'" -and $v[-1] -eq "'"))) { $v = $v.Substring(1, $v.Length - 2) }
    [Environment]::SetEnvironmentVariable($k.Trim(), $v, "Process")
  }
}
$logDir = Join-Path $backend "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
# Python writes (and rotates) its own log file, so no shell redirection is needed. (In Windows
# PowerShell 5.1, redirecting a native program's stderr with $ErrorActionPreference = "Stop"
# aborts the script on the first log line.)
$ErrorActionPreference = "Continue"
& (Join-Path $backend ".venv\Scripts\python.exe") -m nba_edge --log-file (Join-Path $logDir "scheduler.log") run-scheduler
exit $LASTEXITCODE
