<#
H-H Agent: agent_token.json / distill_token.json expire after a 24h TTL by
design. This script registers scripts/hh_issue_agent_token.py as a Windows
Scheduled Task that reissues them every 12 hours, so they never expire
during normal use.

No admin rights required (registers under the current logged-in user).
To remove it manually:
    Unregister-ScheduledTask -TaskName "HH-Agent-TokenRefresh" -Confirm:$false

Safe to re-run (removes any existing task with the same name first).
#>

$ErrorActionPreference = "Stop"

$TaskName = "HH-Agent-TokenRefresh"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$ScriptPath = Join-Path $RepoRoot "scripts\hh_issue_agent_token.py"

if (-not (Test-Path $ScriptPath)) {
    throw "hh_issue_agent_token.py not found: $ScriptPath"
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute "python" -Argument "`"$ScriptPath`"" -WorkingDirectory "$RepoRoot"

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 12) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null

Write-Host "Registered: $TaskName (runs every 12h -> $ScriptPath)"
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State
