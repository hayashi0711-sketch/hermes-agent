<#
HH-Agent-SkillSync: Lane C（Hermesスキル同期）のpull/reconcileを12時間ごとに自動実行する。

scripts/hh_skill_sync.py は Hermes 本体側のスキルとローカルの同期処理
（pull + reconcile）を担う。差分が大きい/小さいにかかわらず、このスクリプトを
Windows タスクスケジューラ経由で 12 時間ごとに走らせることで、手動実行を
忘れないように保つ。

このファイル自体は登録作業だけを担う。実体の `hh_skill_sync.py` がこのマシンに
無くても（別担当が並行作業中のため）構文チェックは通る必要があるため、
`Test-Path` の存在チェックは既存スクリプトと同じ様式で行う。

No admin rights required (registers under the current logged-in user).
To remove it manually:
    Unregister-ScheduledTask -TaskName "HH-Agent-SkillSync" -Confirm:$false

Safe to re-run (removes any existing task with the same name first).
#>

$ErrorActionPreference = "Stop"

$TaskName = "HH-Agent-SkillSync"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$ScriptPath = Join-Path $RepoRoot "scripts\hh_skill_sync.py"

if (-not (Test-Path $ScriptPath)) {
    throw "hh_skill_sync.py not found: $ScriptPath"
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute "python" -Argument "`"$ScriptPath`" --pull --reconcile" -WorkingDirectory "$RepoRoot"

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

Write-Host "Registered: $TaskName (runs every 12h -> $ScriptPath --pull --reconcile)"
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State