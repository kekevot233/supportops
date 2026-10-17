$ErrorActionPreference = "Stop"

$taskName = "HelloAgentDailyBrief"
$projectDirectory = "D:\code\Hello-agent"
$launcherPath = Join-Path $projectDirectory "run_daily_brief_task.cmd"

if (-not (Test-Path -LiteralPath $launcherPath)) {
    throw "Daily brief launcher was not found: $launcherPath"
}

$action = New-ScheduledTaskAction `
    -Execute $env:ComSpec `
    -Argument "/c `"$launcherPath`"" `
    -WorkingDirectory $projectDirectory
$trigger = New-ScheduledTaskTrigger -Daily -At 10:00AM
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Description "Generate and email the HelloAgent AI daily brief at 10:00." `
    -Force | Out-Null

Write-Host "Task created: $taskName"
Write-Host "Schedule: daily at 10:00 while the current user is logged on."
Write-Host "Log file: $projectDirectory\outputs\daily_briefs\scheduled_task.log"
