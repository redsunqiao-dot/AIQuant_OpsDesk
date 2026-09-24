@echo off
chcp 65001 >nul
setlocal
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
set CASEA=E:\AI_quant\12_投资晨会与作战系统\晨会与多因子\CASE-A-板块数据准备
set LOGDIR=%CASEA%\outputs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i
set LOGFILE=%LOGDIR%\run_daily_%TS%.log
cd /d "%CASEA%"
echo [%date% %time%] start run_daily.py >> "%LOGFILE%"
python run_daily.py --level 2 --days 60 >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] exit %RC% >> "%LOGFILE%"
exit /b %RC%
