# AIQuant OpsDesk（开发树）

自包含量化作战台：`python app.py`（默认端口见 `.env` 的 `DASHBOARD_PORT`，开发一般为 `7865`）。

配置集中在本目录根 `.env`（样例见 `.env.example`）。投研对话依赖 `vendor/charles/`（原 `third_party/charles_bundle`）。

---

## 目录结构

```
AIQuant_OpsDesk_DEV/
  app.py                 # Web 入口 (FastAPI)
  scheduler.py           # 交易时段调度 + 日更/晨会
  run_morning_task.py    # 晨会单次任务（计划任务调用）
  .env.example           # 环境变量样例（勿提交真实 .env）
  requirements.txt

  routes/                # REST API
  templates/  static/    # Web UI
  pages/                 # Gradio 子应用（投研对话）
  config/                # 运行时 yaml（监控池/策略/模拟持仓）

  live_trading/          # 实盘主循环、QMT 文件桥、大 QMT 策略
  briefing/              # 晨会 LangGraph
  dragon/                # 龙头筛选与回测辅助
  ml/                    # ML 策略
  attribution/           # 交易归因
  parameter_tuning/      # 参数寻优
  strategy_lifecycle/    # 策略生命周期
  alerting/              # 告警路由
  lib/                   # 工作台胶水（路径、回测、风控、db 加载等）
  models/                # 本地模型权重

  vendor/                # 内嵌依赖（随源码包分发）
    charles/             # Charles + nanobot（投研对话）
    market_data_prep/    # 板块/日 K 日更
    db_env/              # MySQL 配置样例
    market/  shared/     # 公开行情入口
    chan/ quantstats/ rl_dqn/ radar/ team_workflow/
  third_party/           # 已弃用占位，见其中 README

  scripts/               # 启停 / Publish / 计划任务注册
  outputs/  data/        # 运行态（默认 gitignore）
```

正式环境：同级 `AIQuant_OpsDesk_PROD`，由 `scripts/Publish-OpsDesk.ps1` 从本树单向同步（保留正式 `.env` / `outputs`）。

---

## 启动

```powershell
cd <本目录>
pip install -r requirements.txt
Copy-Item .env.example .env   # 首次：填写 QUANT_TRADE_* / DASHSCOPE / QMT 桥路径等
.\scripts\Start-OpsDesk_DEV.ps1
```

| 页面 | 路径 |
|------|------|
| 实盘 | `/live` |
| 回测 | `/backtest` |
| 投研对话 | `/chat`（需 `DASHSCOPE_API_KEY` + `vendor/charles`） |
| 晨会 | `/morning` |
| 系统 | `/system` |

---

## 运维脚本

| 脚本 | 用途 |
|------|------|
| `Start-OpsDesk_DEV.ps1` | 启动开发台 7865 |
| `Start-OpsDesk_PROD.ps1` | 启动正式台 7866 |
| `Publish-OpsDesk.ps1` | DEV → PROD 发版 |
| `Register-OpsDesk_PROD_Schtasks.ps1` | 注册正式日更/晨会计划任务 |

日更默认目录：`vendor/market_data_prep`（可用环境变量 `CASE_A_BOARD_DATA_PREP_DIR` 覆盖；亦兼容旧名 `MARKET_DATA_PREP_DIR`）。
