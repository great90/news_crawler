# 注册 news_crawler 计划任务：每日定时 + 可选登录补跑（幂等由应用保证，同日重复触发不重发）
# 用法：powershell -ExecutionPolicy Bypass -File scripts/register_task.ps1 -Time 08:00
# 注：本文件须保存为 UTF-8 with BOM——Windows PowerShell 5.1 对无 BOM 的中文脚本会按 ANSI 误读并报语法错误
param(
    [string]$Time = "08:00",
    [string]$Python = ""   # 可选：python.exe 完整路径；默认自动探测
)

$ErrorActionPreference = "Stop"
$appDir = Split-Path -Parent $PSScriptRoot   # scripts/ 的上一级 = news_crawler 应用根

# 解析 python 绝对路径：显式指定优先；探测用 python.exe/py.exe 完整名，
# 避免被 PowerShell profile 里的同名别名/函数遮蔽导致 .Source 为空
$pythonExe = $null
$candidates = if ($Python) { @($Python) } else { @("python.exe", "py.exe") }
foreach ($cand in $candidates) {
    if (Test-Path $cand) { $pythonExe = (Resolve-Path $cand).Path; break }
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and (Test-Path $cmd.Source)) { $pythonExe = $cmd.Source; break }
}
if (-not $pythonExe) { throw "未找到可用的 python.exe，请用 -Python 参数指定完整路径" }

# 用 schtasks 注册（标准用户可创建自己的任务，无需管理员）；经 cmd 包装以设置工作目录
$runCmd = 'cmd /c cd /d "{0}" && "{1}" -m news_crawler run' -f $appDir, $pythonExe
schtasks /Create /F /TN "news_crawler-daily" /SC DAILY /ST $Time /TR $runCmd
if ($LASTEXITCODE -ne 0) { throw "每日定时任务注册失败（exit=$LASTEXITCODE）" }

# 登录补跑：ONLOGON 触发器需要管理员权限，失败不阻塞（每日定时不依赖它）
schtasks /Create /F /TN "news_crawler-logon" /SC ONLOGON /RU $env:USERNAME /IT /TR $runCmd 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "登录补跑任务注册失败（ONLOGON 触发器需管理员）。每日定时已生效。"
    Write-Warning "如需关机漏发补跑：以管理员身份重跑本脚本，或手动注册 ONLOGON 任务。"
    Write-Host "已注册：news_crawler-daily（每日 $Time）；应用目录：$appDir"
} else {
    Write-Host "已注册：news_crawler-daily（每日 $Time）+ news_crawler-logon（登录补跑）；应用目录：$appDir"
}
