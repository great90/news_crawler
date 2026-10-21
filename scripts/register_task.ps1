# 注册 newmail 计划任务：每日定时 + 登录补跑（幂等由应用保证，同日重复触发不重发）
# 用法：powershell -ExecutionPolicy Bypass -File scripts/register_task.ps1 -Time 08:00
param(
    [string]$Time = "08:00",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$appDir = Split-Path -Parent $PSScriptRoot   # scripts/ 的上一级 = news_crawler 应用根
$pythonExe = (Get-Command $Python).Source

$action = New-ScheduledTaskAction -Execute $pythonExe -Argument "-m news_crawler run" -WorkingDirectory $appDir
# StartWhenAvailable：错过触发点（关机）后开机补跑；配合应用内按日期幂等，不重复发送
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)

$triggerDaily = New-ScheduledTaskTrigger -Daily -At $Time
Register-ScheduledTask -TaskName "news_crawler-daily" -Action $action -Trigger $triggerDaily `
    -Settings $settings -Description "news_crawler 每日 $Time 资讯摘要邮件" -Force | Out-Null

$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "news_crawler-logon" -Action $action -Trigger $triggerLogon `
    -Settings $settings -Description "news_crawler 登录补跑（当天已成功则幂等退出）" -Force | Out-Null

Write-Host "已注册计划任务：news_crawler-daily（每日 $Time）、news_crawler-logon（登录补跑）"
Write-Host "应用目录：$appDir"
