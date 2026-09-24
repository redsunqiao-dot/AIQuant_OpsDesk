# -*- coding: utf-8 -*-
# Publish-OpsDesk.ps1 -- 从 AIQuant_OpsDesk_DEV 单向同步代码到 AIQuant_OpsDesk_PROD
# 永不覆盖: 目标 .env / outputs / data / qmt_bridge / 审批与 state 类运行态文件
# 用法: powershell -ExecutionPolicy Bypass -File .\scripts\Publish-OpsDesk.ps1
#       可选 -SkipConfig  不同步 config/*.yaml（保留生产监控池与策略绑定）

param(
    [switch]$SkipConfig
)

$ErrorActionPreference = "Stop"
$DevRoot = Split-Path -Parent $PSScriptRoot
$Parent = Split-Path -Parent $DevRoot
$ProdRoot = Join-Path $Parent "AIQuant_OpsDesk_PROD"

if (-not (Test-Path -LiteralPath $ProdRoot)) {
    throw "正式树不存在: $ProdRoot"
}

Write-Host "源: $DevRoot"
Write-Host "目标: $ProdRoot"

# 同步代码与模板（排除运行态）
# 注意: 不排除 vendor；third_party 仅保留 README 占位
$xd = @("__pycache__", ".git", "outputs", "data")
$xf = @("*.pyc", ".env")
$xdArgs = ($xd | ForEach-Object { "/XD"; $_ })
$xfArgs = ($xf | ForEach-Object { "/XF"; $_ })

& robocopy $DevRoot $ProdRoot /E @xdArgs @xfArgs /NFL /NDL /NJH /NJS /nc /ns /np
$code = $LASTEXITCODE
if ($code -ge 8) { throw "robocopy 失败 exit=$code" }

# 清理正式树残留的旧路径（已迁到 vendor/ 或已改名）
$legacy = @(
    (Join-Path $ProdRoot "third_party\charles_bundle"),
    (Join-Path $ProdRoot "vendor\case_a_board_prep"),
    (Join-Path $ProdRoot "morning_brief"),
    (Join-Path $ProdRoot "dragon_strategy"),
    (Join-Path $ProdRoot "ml_strategy")
)
foreach ($p in $legacy) {
    if (Test-Path -LiteralPath $p) {
        # 若为 junction 用 rmdir；普通目录用 Remove-Item
        cmd /c "rmdir /s /q `"$p`"" 2>$null
        if (Test-Path -LiteralPath $p) {
            Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
        }
        Write-Host "已清理旧路径: $p"
    }
}

if ($SkipConfig) {
    Write-Host "已跳过 config/ 同步 (-SkipConfig)"
} else {
    $devCfg = Join-Path $DevRoot "config"
    $prodCfg = Join-Path $ProdRoot "config"
    & robocopy $devCfg $prodCfg /E /NFL /NDL /NJH /NJS /nc /ns /np
}

# 确保正式桥目录存在
$bridge = Join-Path $ProdRoot "outputs\qmt_bridge"
foreach ($s in @("outbox", "processing", "acked", "inbox")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $bridge $s) | Out-Null
}
New-Item -ItemType Directory -Force -Path (Join-Path $ProdRoot "outputs\backtest_reports") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ProdRoot "data\cache") | Out-Null

# 用 Python 改写 BRIDGE_DIR，避免 PowerShell 编码误伤
$fixPy = Join-Path $PSScriptRoot "Fix-ProdBridgeDir.py"
$stratPath = Join-Path $ProdRoot "live_trading\big_qmt_file_bridge_strategy.py"
$bridgeUnix = ($bridge -replace "\\", "/")
python -u $fixPy $stratPath $bridgeUnix
if ($LASTEXITCODE -ne 0) { throw "改写 BRIDGE_DIR 失败" }
Write-Host "发布完成. 正式端口见 PROD .env DASHBOARD_PORT (默认 7866)."
Write-Host "请手动重启正式进程: .\scripts\Start-OpsDesk_PROD.ps1"
