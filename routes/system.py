# -*- coding: utf-8 -*-
# 系统状态路由 -- REST
"""
GET /api/system/health   -- 健康检查 (MySQL / 行情 / Kris / 文件桥 / 策略 / 模型)
GET /api/system/ping     -- 探活
"""

from __future__ import annotations
import os
import sys

from fastapi import APIRouter

from lib.paths import (
    setup_sys_path, OUTPUTS_LIVE_STATE, OUTPUTS_RESEARCH, PROJECT_ROOT,
)
setup_sys_path()

router = APIRouter()


@router.get("/ping")
def ping():
    return {"ok": True, "module": "system"}


@router.get("/health")
def health():
    rows = []

    app_name = (os.environ.get("APP_NAME") or "AIQuant_OpsDesk").strip()
    app_env = (os.environ.get("APP_ENV") or "DEV").strip().upper()
    rows.append({
        "item": "部署身份",
        "value": f"{app_name} / {app_env} @ {PROJECT_ROOT.name}",
        "status": "OK" if app_env in ("DEV", "PROD") else "WARN",
    })
    rows.append({
        "item": "DASHBOARD_PORT",
        "value": os.environ.get("DASHBOARD_PORT") or "(默认)",
        "status": "OK",
    })
    bridge = (os.environ.get("QMT_BRIDGE_DIR") or "").strip()
    rows.append({
        "item": "QMT_BRIDGE_DIR",
        "value": bridge or "(默认 outputs/qmt_bridge)",
        "status": "OK" if bridge else "WARN",
    })

    rows.append({"item": "Python 版本", "value": sys.version.split()[0], "status": "OK"})

    # MySQL (根 .env / vendor/db_env，兼容 02_行情数据采集/.env)
    try:
        from lib.backtest_data import mysql_available
        ok = mysql_available()
        rows.append({
            "item": "MySQL 行情库",
            "value": "可连 (trade_stock_daily)" if ok else "不可连",
            "status": "OK" if ok else "ERROR",
        })
    except Exception as e:
        rows.append({"item": "MySQL 行情库", "value": str(e)[:80], "status": "ERROR"})

    # 公开行情抽检 (日 K)
    try:
        from lib.backtest_data import load_daily_kline
        from datetime import datetime, timedelta
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        df = load_daily_kline("600519.SH", start_date=start, end_date=end, prefer="mysql")
        n = 0 if df is None else len(df)
        rows.append({
            "item": "日 K 抽检 (600519)",
            "value": f"{n} 根" if n else "无数据",
            "status": "OK" if n >= 5 else "WARN",
        })
    except Exception as e:
        rows.append({"item": "日 K 抽检 (600519)", "value": str(e)[:80], "status": "ERROR"})

    if os.environ.get("DASHSCOPE_API_KEY"):
        rows.append({"item": "DASHSCOPE_API_KEY", "value": "已配置", "status": "OK"})
    else:
        rows.append({"item": "DASHSCOPE_API_KEY",
                     "value": "未配置 (Charles / 团队 LLM 不可用)", "status": "WARN"})

    # 券商模式: 默认 file_bridge
    try:
        from live_trading.qmt_file_bridge import broker_mode, account_id, bridge_root, read_account_snapshot
        mode = broker_mode()
        acc = account_id() or "(未配 ACCOUNT_ID)"
        if mode == "file_bridge":
            snap = read_account_snapshot(max_age_sec=600.0)
            total = float((snap.get("asset") or {}).get("total_asset") or 0)
            age = snap.get("age_sec")
            err = snap.get("error")
            if total > 0:
                val = f"file_bridge 账户={acc} total={total:.0f}"
                if age is not None:
                    val += f" age={age}s"
                st = "OK" if (age is None or float(age) < 120) else "WARN"
            else:
                val = f"file_bridge 无快照 ({err or bridge_root()})"
                st = "WARN"
            rows.append({"item": "券商桥", "value": val, "status": st})
        elif mode == "xtquant":
            qmt = os.environ.get("QMT_PATH") or ""
            rows.append({
                "item": "券商桥",
                "value": f"xtquant QMT_PATH={'已配' if qmt else '未配'} account={acc}",
                "status": "OK" if qmt and acc else "WARN",
            })
        else:
            rows.append({"item": "券商桥", "value": f"BROKER_MODE={mode}", "status": "WARN"})
    except Exception as e:
        rows.append({"item": "券商桥", "value": str(e)[:80], "status": "ERROR"})

    # Kris
    try:
        from lib.kris_adapter import get_summary
        s = get_summary(1_000_000)
        rows.append({
            "item": "Kris 风控",
            "value": f"risk={s.get('macro', {}).get('risk_level')} vix={s.get('macro', {}).get('vix')}",
            "status": "OK",
        })
    except Exception as e:
        rows.append({"item": "Kris 风控", "value": str(e)[:80], "status": "ERROR"})

    # 策略注册表
    try:
        from lib.strategy_registry import list_strategies
        names = sorted(s["name"] for s in list_strategies())
        need = {"chanlun", "turtle_donchian", "dqn", "macd_5min"}
        miss = sorted(need - set(names))
        rows.append({
            "item": "策略注册表",
            "value": f"{len(names)} 个" + (f", 缺 {miss}" if miss else ""),
            "status": "OK" if not miss else "WARN",
        })
    except Exception as e:
        rows.append({"item": "策略注册表", "value": str(e)[:80], "status": "ERROR"})

    # DQN 模型
    dqn = PROJECT_ROOT / "models" / "dqn_best.pth"
    rows.append({
        "item": "DQN 模型",
        "value": str(dqn.name) if dqn.exists() else "models/dqn_best.pth 不存在",
        "status": "OK" if dqn.exists() else "WARN",
    })

    if OUTPUTS_LIVE_STATE.exists():
        import json
        try:
            from live_trading.state_store import read_text_shared
            s = json.loads(read_text_shared(OUTPUTS_LIVE_STATE))
            rows.append({"item": "live_state.json",
                         "value": f"updated_at={s.get('_updated_at', '?')}", "status": "OK"})
        except Exception as e:
            rows.append({"item": "live_state.json",
                         "value": f"解析失败: {e}", "status": "ERROR"})
    else:
        rows.append({"item": "live_state.json",
                     "value": "不存在 (启动模拟盘后会自动创建)", "status": "WARN"})

    if OUTPUTS_RESEARCH.exists():
        n = len(list(OUTPUTS_RESEARCH.glob("morning_brief_*.html")))
        rows.append({"item": "晨会分析 HTML", "value": f"{n} 份",
                     "status": "OK" if n > 0 else "WARN"})
    else:
        rows.append({"item": "晨会分析 HTML",
                     "value": "目录不存在", "status": "WARN"})

    return rows
