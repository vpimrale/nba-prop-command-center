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
    [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
  }
}
$logDir = Join-Path $backend "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
& (Join-Path $backend ".venv\Scripts\python.exe") -m nba_edge run-scheduler *>> (Join-Path $logDir "scheduler.log")
exit $LASTEXITCODE
