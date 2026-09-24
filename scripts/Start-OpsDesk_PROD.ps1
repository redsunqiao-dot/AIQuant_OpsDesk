# Start-OpsDesk_PROD.ps1 -- 启动生产环境作战台 (7866, 禁止自动换端口)
$ErrorActionPreference = "Stop"
$DevScripts = $PSScriptRoot
# 本脚本发布后也存在于 PROD\scripts
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$env:APP_NAME = "AIQuant_OpsDesk"
$env:APP_ENV = "PROD"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
Write-Host "启动 PROD: $Root"
python -u app.py --port 7866 --no-auto-port
