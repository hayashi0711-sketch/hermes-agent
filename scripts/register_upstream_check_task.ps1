<#
HH-Agent-UpstreamCheck: upstream (nousresearch/hermes-agent) との乖離を
1日1回チェックし、しきい値を超えたらntfyで通知する。

scripts/hh_check_upstream.py がgit fetch + rev-listで乖離コミット数を数え、
20コミット以上先行していたら(かつ前回通知から日付が変わっているか、
+200コミット以上増えていたら)通知する。マージ・テスト・push・デプロイは
一切行わない — 通知のみ。同期作業自体はSonnet5が判断して行う
(docs/hh-agent/08_Handoff_Note.md 21セッション目参照)。

No admin rights required (registers under the current logged-in user).
To remove it manually:
    Unregister-ScheduledTask -TaskName "HH-Agent-UpstreamCheck" -Confirm:$false

Safe to re-run (removes any existing task with the same name first).
#>

$ErrorActionPreference = "Stop"

$TaskName = "HH-Agent-UpstreamCheck"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$ScriptPath = Join-Path $RepoRoot "scripts\hh_check_upstream.py"

if (-not (Test-Path $ScriptPath)) {
    throw "hh_check_upstream.py not found: $ScriptPath"
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute "python" -Argument "`"$ScriptPath`"" -WorkingDirectory "$RepoRoot"

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 24) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null

Write-Host "Registered: $TaskName (runs every 24h -> $ScriptPath)"
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State
