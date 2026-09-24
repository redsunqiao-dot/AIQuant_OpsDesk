@echo off
chcp 65001 >nul
setlocal
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
set APP_ENV=DEV
set OPSDESK=%~dp0..
set LOGDIR=%OPSDESK%\outputs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i
set LOGFILE=%LOGDIR%\run_morning_%TS%.log
cd /d "%OPSDESK%"
echo [%date% %time%] start run_morning_task.py >> "%LOGFILE%"
python run_morning_task.py >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] exit %RC% >> "%LOGFILE%"
exit /b %RC%
