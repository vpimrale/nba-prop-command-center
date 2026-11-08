# Registers a per-user scheduled task that starts the scheduler at logon and restarts it if it
# stops. Review before running:  powershell -ExecutionPolicy Bypass -File register-task.ps1
# Remove with:  Unregister-ScheduledTask -TaskName "NBA Edge Scheduler"
# Also: Settings > System > Power — set "Sleep" to Never while plugged in (a sleeping PC
# misses odds slots; the system records them as missed rather than hiding the gap).
$script = Join-Path $PSScriptRoot "run-scheduler.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "NBA Edge Scheduler" -Action $action -Trigger $trigger -Settings $settings `
  -Description "NBA Edge paper-trading scheduler (paper only; no real-money betting)"
