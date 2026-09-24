# Start-OpsDesk_DEV.ps1 -- 启动开发环境作战台 (7865)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$env:APP_NAME = "AIQuant_OpsDesk"
$env:APP_ENV = "DEV"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
Write-Host "启动 DEV: $Root"
python -u app.py
