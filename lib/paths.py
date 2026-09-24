# -*- coding: utf-8 -*-
# AIQuant OpsDesk 路径常量（见 lib/ 旁目录树；唯一 .env 见 ENV_FILE）
import sys
from pathlib import Path

# 工作台根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 同级子目录
LIVE_TRADING_DIR    = PROJECT_ROOT / "live_trading"
ALERTING_DIR        = PROJECT_ROOT / "alerting"
DRAGON_DIR          = PROJECT_ROOT / "dragon"
BRIEFING_DIR        = PROJECT_ROOT / "briefing"
ML_DIR              = PROJECT_ROOT / "ml"
# 旧常量名兼容
DRAGON_STRATEGY_DIR = DRAGON_DIR
MORNING_BRIEF_DIR   = BRIEFING_DIR
PAGES_DIR           = PROJECT_ROOT / "pages"
LIB_DIR             = PROJECT_ROOT / "lib"
CONFIG_DIR          = PROJECT_ROOT / "config"
OUTPUTS_DIR         = PROJECT_ROOT / "outputs"
DATA_DIR            = PROJECT_ROOT / "data"
VENDOR_DIR          = PROJECT_ROOT / "vendor"
VENDOR_DB_ENV_DIR   = VENDOR_DIR / "db_env"
VENDOR_MARKET_DATA_PREP_DIR = VENDOR_DIR / "market_data_prep"
VENDOR_CASE_A_DIR   = VENDOR_MARKET_DATA_PREP_DIR
VENDOR_CHARLES_DIR  = VENDOR_DIR / "charles"
CHARLES_AGENT_PY    = VENDOR_CHARLES_DIR / "charles-nanobot" / "agent.py"
VENDOR_CHAN_DIR     = VENDOR_DIR / "chan"
VENDOR_QUANTSTATS_DIR = VENDOR_DIR / "quantstats"
VENDOR_RL_DQN_DIR   = VENDOR_DIR / "rl_dqn"
VENDOR_MARKET_DIR   = VENDOR_DIR / "market"
VENDOR_SHARED_DIR   = VENDOR_DIR / "shared"
VENDOR_RADAR_DIR    = VENDOR_DIR / "radar"
VENDOR_TEAM_WORKFLOW_DIR = VENDOR_DIR / "team_workflow"

# 复盘归因 (review) 子模块: attribution / parameter_tuning / strategy_lifecycle
# 都是本项目内置子包, 通过 setup_sys_path() 把 PROJECT_ROOT 加到 sys.path 即可 import.
ATTRIBUTION_DIR        = PROJECT_ROOT / "attribution"
PARAMETER_TUNING_DIR   = PROJECT_ROOT / "parameter_tuning"
STRATEGY_LIFECYCLE_DIR = PROJECT_ROOT / "strategy_lifecycle"

# 关键产出
OUTPUTS_LIVE_STATE      = OUTPUTS_DIR / "live_state.json"
OUTPUTS_RESEARCH        = BRIEFING_DIR / "outputs" / "reports"
OUTPUTS_EVOLVE_REGISTRY = OUTPUTS_DIR / "strategy_registry.json"

# 全项目唯一环境变量文件
ENV_FILE = PROJECT_ROOT / ".env"


def setup_sys_path() -> None:
    """把工作台子模块加入 sys.path, 让 import 直接用模块名."""
    for p in (PROJECT_ROOT, LIVE_TRADING_DIR, ALERTING_DIR,
              DRAGON_DIR, BRIEFING_DIR, ML_DIR, LIB_DIR):
        sp = str(p)
        if p.exists() and sp not in sys.path:
            sys.path.insert(0, sp)


# 确保 outputs 与 data 目录存在
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
