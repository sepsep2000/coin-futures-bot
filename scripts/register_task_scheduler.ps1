# register_task_scheduler.ps1
# Windows 작업 스케줄러로 WSL 봇 실행 트리거를 옮긴다 - crontab은 WSL이
# 이미 깨어있어야만 도는데, WSL이 유휴 상태에서 스스로 잠들면 cron도 같이
# 멈춘다(2026-07-28 실측: 7시간 47분 정지). 작업 스케줄러는 OS 핵심 서비스라
# 컴퓨터가 켜져있는 한 절대 안 잠들고, wsl.exe를 호출하면 WSL을 그때그때
# 깨운다.
#
# 사용법: PowerShell 창에 이 파일 내용을 통째로 붙여넣기 (관리자 권한 불필요)

$ProjectPathWSL = "/mnt/c/Users/조요셉/Desktop/클로드/자동매매"

$wslCmdRunCycle = "cd $ProjectPathWSL && bash scripts/run_cycle.sh >> logs/run_cycle.log 2>&1"
$wslCmdHealth   = "cd $ProjectPathWSL && .venv-wsl/bin/python scripts/healthcheck.py >> logs/healthcheck.log 2>&1"
$wslCmdDaily    = "cd $ProjectPathWSL && .venv-wsl/bin/python scripts/daily_report.py >> logs/daily_report.log 2>&1"

$actionRunCycle = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-e bash -c `"$wslCmdRunCycle`""
$actionHealth   = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-e bash -c `"$wslCmdHealth`""
$actionDaily    = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-e bash -c `"$wslCmdDaily`""

# 15분마다, 10년간(사실상 무기한) 반복
$trigger15 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)

# 매일 08:00 (로컬=KST) = 원래 의도했던 UTC 23:00과 동일 시점(daily_report.py의
# "KST {date} 08:00" 라벨과 실제 실행 시각이 일치하게 됨 - 기존 WSL crontab의
# "0 23 * * *"는 로컬시간(KST) 기준이라 실제로는 23:00 KST=14:00 UTC에 돌고
# 있었던 표기 불일치 버그였음, 이번에 같이 바로잡음)
$triggerDaily = New-ScheduledTaskTrigger -Daily -At "08:00"

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

# S4U: 비밀번호 저장 없이 현재 로그인 계정 권한으로 실행(로그인 세션 없어도 동작)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

Register-ScheduledTask -TaskName "RSIB_run_cycle"    -Action $actionRunCycle -Trigger $trigger15    -Settings $settings -Principal $principal -Force | Out-Null
Register-ScheduledTask -TaskName "RSIB_healthcheck"  -Action $actionHealth   -Trigger $trigger15    -Settings $settings -Principal $principal -Force | Out-Null
Register-ScheduledTask -TaskName "RSIB_daily_report" -Action $actionDaily   -Trigger $triggerDaily -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "`n=== 등록 확인 ===" -ForegroundColor Cyan
Get-ScheduledTask -TaskName "RSIB_*" | Select-Object TaskName, State

Write-Host "`n=== 지금 바로 1회 테스트 실행 (run_cycle) ===" -ForegroundColor Cyan
Start-ScheduledTask -TaskName "RSIB_run_cycle"
Start-Sleep -Seconds 5
Get-ScheduledTask -TaskName "RSIB_run_cycle" | Get-ScheduledTaskInfo | Select-Object LastRunTime, LastTaskResult

Write-Host "`n=== WSL 자체 crontab 정리 (중복 트리거 방지 - 이제 작업 스케줄러가 전담) ===" -ForegroundColor Cyan
wsl.exe -e bash -c "crontab -r 2>/dev/null; echo '(crontab 비움 완료, 현재 등록:)'; crontab -l 2>&1"

Write-Host "`n완료. 15분 뒤 logs\heartbeat.json 이 최신 시각으로 갱신되는지 확인하면 됩니다." -ForegroundColor Green
