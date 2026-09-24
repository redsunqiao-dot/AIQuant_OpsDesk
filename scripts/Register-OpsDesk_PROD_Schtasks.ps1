# Register-OpsDesk_PROD_Schtasks.ps1
# bat uses %~dp0 relative paths (avoid Chinese abs path cd failure under schtasks)
$ErrorActionPreference = "Stop"
$Parent = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ProdRoot = Join-Path $Parent "AIQuant_OpsDesk_PROD"
if (-not (Test-Path -LiteralPath $ProdRoot)) { throw "PROD missing: $ProdRoot" }
$py = (Get-Command python -ErrorAction Stop).Source
$scriptDir = Join-Path $ProdRoot "scripts"
New-Item -ItemType Directory -Force -Path $scriptDir | Out-Null

$prepDefault = Join-Path $ProdRoot "vendor\market_data_prep"
if (-not (Test-Path -LiteralPath (Join-Path $prepDefault "run_daily.py"))) {
    throw "run_daily.py missing: $prepDefault"
}

$batDaily = Join-Path $scriptDir "Run-PROD_DailyData.bat"
$batMorning = Join-Path $scriptDir "Run-PROD_MorningBrief.bat"

$dailyLines = @(
    "@echo off",
    "chcp 65001 >nul",
    "cd /d `"%~dp0..\vendor\market_data_prep`"",
    "if not exist `"run_daily.py`" (",
    "  echo [ERROR] run_daily.py not found in %CD%",
    "  exit /b 2",
    ")",
    "`"$py`" -u run_daily.py",
    "exit /b %ERRORLEVEL%"
)
$morningLines = @(
    "@echo off",
    "chcp 65001 >nul",
    "cd /d `"%~dp0..`"",
    "set APP_ENV=PROD",
    "set APP_NAME=AIQuant_OpsDesk",
    "set PYTHONUTF8=1",
    "set PYTHONIOENCODING=utf-8",
    "if not exist `"run_morning_task.py`" (",
    "  echo [ERROR] run_morning_task.py not found in %CD%",
    "  exit /b 2",
    ")",
    "`"$py`" -u run_morning_task.py",
    "exit /b %ERRORLEVEL%"
)
[System.IO.File]::WriteAllLines($batDaily, $dailyLines)
[System.IO.File]::WriteAllLines($batMorning, $morningLines)
Write-Host "wrote bats"

function Register-Task([string]$name, [string]$time, [string]$bat) {
    cmd /c "schtasks /Delete /TN `"$name`" /F >nul 2>&1"
    $tr = "`"$bat`""
    cmd /c "schtasks /Create /TN `"$name`" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST $time /TR $tr /RL LIMITED /F"
    if ($LASTEXITCODE -ne 0) { throw "register failed $name" }
    Write-Host "registered $name @ $time"
}

Register-Task "AIQuant_OpsDesk_PROD_DailyData" "07:40" $batDaily
Register-Task "AIQuant_OpsDesk_PROD_MorningBrief" "08:30" $batMorning

foreach ($old in @("AI_quant_CASEA_daily", "AI_quant_morning_brief")) {
    cmd /c "schtasks /Change /TN `"$old`" /DISABLE >nul 2>&1"
}

Write-Host "done"
