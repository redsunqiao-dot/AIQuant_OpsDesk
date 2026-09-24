# -*- coding: utf-8 -*-
# Kris 风控适配层 -- 统一 RiskManager 单例 + ATR/组合构建 + 审批入口
"""
供 live_loop / live 授权链 / routes/kris 共用。

设计:
    - 单笔上限默认 = 总资金 10% (与原 live_loop 硬编码一致, 迁入 Kris 规则1)
    - 日熔断默认 2% (与原 control.max_daily_loss 一致)
    - Phase 2: approve 买入时空 news 自动拉 trade_stock_news;
      ensure_macro_vix 按日缓存灌入 50ETF QVIX
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from lib.paths import OUTPUTS_DIR, setup_sys_path
from lib.risk_engine import Decision, Order, RiskDecision, RiskManager

setup_sys_path()

OUTPUTS_KRIS_AUDIT = OUTPUTS_DIR / "kris_audit.json"

_lock = threading.Lock()
_KRIS: Optional[RiskManager] = None
_STARTED_DAY: str = ""
_VIX_CACHE_DAY: str = ""
_VIX_CACHE_VALUE: Optional[float] = None
_VIX_CACHE_SOURCE: str = ""


def _default_config(capital: float) -> dict:
    """按账户资金生成 Kris 配置 (保留原 10% 单笔 / 2% 日熔断)."""
    cap = float(capital or 1_000_000)
    return {
        "pre_trade": {
            "max_order_amount": max(cap * 0.10, 10_000),
            "price_collar_pct": 0.05,
            "blacklist": [],
            "atr_risk_pct": 0.01,
            "atr_overshoot_ratio": 2.0,
        },
        "circuit_breaker": {
            "max_daily_loss_pct": 0.02,
            "atr_stop_multiplier": 2.0,
        },
    }


def get_kris(capital: float = 1_000_000) -> RiskManager:
    """进程内单例; 跨日自动 start_day."""
    global _KRIS, _STARTED_DAY
    with _lock:
        if _KRIS is None:
            _KRIS = RiskManager(config=_default_config(capital))
        today = datetime.now().strftime("%Y-%m-%d")
        if _STARTED_DAY != today:
            _KRIS.start_day(float(capital or 1_000_000))
            _KRIS.pre_trade.max_order_amount = max(float(capital or 1_000_000) * 0.10, 10_000)
            _STARTED_DAY = today
        return _KRIS


def ensure_macro_vix(capital: float = 1_000_000, force: bool = False) -> dict:
    """按日缓存灌入 50ETF QVIX; force=True 强制重拉.

    返回 {ok, vix, date, source, coefficient, risk_level, message?}.
    """
    global _VIX_CACHE_DAY, _VIX_CACHE_VALUE, _VIX_CACHE_SOURCE
    today = datetime.now().strftime("%Y-%m-%d")
    kris = get_kris(capital)

    if (not force) and _VIX_CACHE_DAY == today and _VIX_CACHE_VALUE is not None:
        coef = kris.macro.update_vix(float(_VIX_CACHE_VALUE))
        return {
            "ok": True,
            "vix": float(_VIX_CACHE_VALUE),
            "date": _VIX_CACHE_DAY,
            "source": _VIX_CACHE_SOURCE or "cache",
            "coefficient": coef,
            "risk_level": kris.macro.risk_level,
        }

    from lib.qvix_fetcher import fetch_latest_qvix
    info = fetch_latest_qvix()
    vix = float(info["vix"])
    coef = kris.macro.update_vix(vix)
    with _lock:
        _VIX_CACHE_DAY = today
        _VIX_CACHE_VALUE = vix
        _VIX_CACHE_SOURCE = f"qvix@{info.get('date')}"
    return {
        "ok": True,
        "vix": vix,
        "date": info.get("date"),
        "source": _VIX_CACHE_SOURCE,
        "coefficient": coef,
        "risk_level": kris.macro.risk_level,
        "count": info.get("count"),
    }


def calc_atr(df: pd.DataFrame, period: int = 14) -> float:
    """TR 的 rolling mean 近似 ATR. 缺列或不足返回 0."""
    if df is None or len(df) < period + 1:
        return 0.0
    need = {"high", "low", "close"}
    if not need.issubset(set(df.columns)):
        return 0.0
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.rolling(period).mean().iloc[-1])
    if np.isnan(atr) or atr <= 0:
        return 0.0
    return atr


def _fetch_atr(code: str, period: int = 14) -> float:
    """用 MySQL 日 K 算 ATR (与回测 / 晨会同源, 不走公开行情源)."""
    try:
        from datetime import datetime, timedelta
        from lib.backtest_data import load_daily_kline
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=max(160, period * 5))).strftime("%Y-%m-%d")
        df = load_daily_kline(str(code).strip(), start_date=start, end_date=end, prefer="mysql")
        return calc_atr(df, period=period)
    except Exception as e:
        print(f"[WARN] _fetch_atr {code}: {type(e).__name__}: {e}", flush=True)
        return 0.0


def _fetch_last_price(code: str) -> float:
    try:
        from public_market import get_latest_price, to_std_code
        info = get_latest_price(to_std_code(code)) or {}
        return float(info.get("lastPrice") or 0)
    except Exception:
        return 0.0


def build_portfolio(code: str, price: float, capital: float,
                    atr_value: Optional[float] = None,
                    market_price: Optional[float] = None) -> dict:
    """构造 Kris.approve 所需 portfolio.

    prices[code] 必须是市价 (做价格偏离检查), 不能填委托价.
    """
    atr = float(atr_value) if atr_value is not None else _fetch_atr(code)
    mkt = float(market_price) if market_price is not None else _fetch_last_price(code)
    if mkt <= 0:
        mkt = float(price or 0)
    return {
        "total_asset": float(capital or 0),
        "prices": {code: mkt},
        "atr": {code: atr} if atr > 0 else {},
    }


def decision_to_dict(d: RiskDecision) -> dict:
    return {
        "decision": d.decision.value,
        "reason": d.reason,
        "rule_name": d.rule_name,
        "max_position_pct": float(d.max_position_pct),
        "is_approved": bool(d.is_approved),
        "timestamp": d.timestamp,
    }


def _append_audit(entry: dict) -> None:
    OUTPUTS_KRIS_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    rows: List[dict] = []
    if OUTPUTS_KRIS_AUDIT.exists():
        try:
            rows = json.loads(OUTPUTS_KRIS_AUDIT.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                rows = []
        except Exception:
            rows = []
    rows.append(entry)
    rows = rows[-500:]
    OUTPUTS_KRIS_AUDIT.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def approve_order(
    code: str,
    side: str,
    quantity: int,
    price: float,
    capital: float,
    news_text: str = "",
) -> Tuple[RiskDecision, int, dict]:
    """审批一笔订单.

    Returns:
        (decision, adjusted_quantity, portfolio)
        WARN 时按 max_position_pct 缩量 (100 股一手).
    """
    code = str(code or "").strip()
    side = str(side or "").strip().lower()
    quantity = int(quantity or 0)
    price = float(price or 0)
    capital = float(capital or 0)

    if quantity <= 0 or price <= 0:
        d = RiskDecision(
            decision=Decision.REJECT,
            reason=f"数量/价格非法 qty={quantity} price={price}",
            rule_name="入参校验",
        )
        return d, 0, {}

    amount = quantity * price
    atr = _fetch_atr(code)
    market_price = _fetch_last_price(code)
    portfolio = build_portfolio(
        code, price, capital, atr_value=atr, market_price=market_price,
    )
    order = Order(
        stock_code=code,
        direction=side,
        amount=amount,
        price=price,
        quantity=quantity,
    )
    # 买入且未显式传新闻: 强制从 trade_stock_news 拉取
    news_status = "caller_provided"
    resolved_news = news_text or ""
    if side == "buy" and not str(news_text or "").strip():
        from lib.news_for_kris import load_news_text
        resolved_news, news_status = load_news_text(code)

    # 按日确保宏观 QVIX 已灌入 (人工灌入过则仍会刷新当日缓存逻辑由 ensure 控制)
    try:
        ensure_macro_vix(capital, force=False)
    except Exception as e:
        # 宏观拉取失败不阻断审批, 但写入审计由下方 entry 记录
        news_status = f"{news_status}; qvix_fail:{type(e).__name__}:{e}"

    kris = get_kris(capital)
    decision = kris.approve(
        order, portfolio, context={"news_text": resolved_news or ""},
    )
    # 无新闻时 EventKeywordChecker 会跳过; 在 reason 里补数据状态, 避免误以为已检查
    if side == "buy" and not resolved_news and "无近期新闻" in (decision.reason or ""):
        decision = RiskDecision(
            decision=decision.decision,
            reason=f"{decision.reason} | {news_status}",
            rule_name=decision.rule_name,
            max_position_pct=decision.max_position_pct,
        )

    adj_qty = quantity
    if decision.decision == Decision.WARN and decision.max_position_pct < 1.0:
        # max_position_pct 是 0~1 比例, 向下取整到 100 股一手
        adj_qty = (int(quantity * float(decision.max_position_pct)) // 100) * 100
        if adj_qty <= 0 and quantity >= 100:
            adj_qty = 100
        if adj_qty <= 0:
            decision = RiskDecision(
                decision=Decision.REJECT,
                reason=decision.reason + "; WARN 缩量后不足 1 手",
                rule_name="综合审批",
            )

    _append_audit({
        "time": decision.timestamp,
        "stock": code,
        "direction": side,
        "amount": round(amount, 2),
        "quantity": quantity,
        "adjusted_quantity": adj_qty,
        "price": price,
        "market_price": market_price,
        "decision": decision.decision.value,
        "rule": decision.rule_name,
        "reason": decision.reason,
        "max_position_pct": decision.max_position_pct,
        "news_status": news_status,
        "news_chars": len(resolved_news or ""),
    })
    return decision, adj_qty, portfolio


def suggest_quantity(code: str, price: float, capital: float) -> int:
    """建议下单股数: min(资金10%, ATR 仓位), 至少 1 手试探."""
    if price <= 0:
        return 100
    capital = float(capital or 0)
    max_by_pct = capital * 0.10
    qty = int(max_by_pct / price / 100) * 100

    atr = _fetch_atr(code)
    if atr > 0 and capital > 0:
        suggested_amt = (capital * 0.01) / atr * price
        qty_atr = int(suggested_amt / price / 100) * 100
        if qty_atr > 0:
            qty = min(qty, qty_atr) if qty > 0 else qty_atr

    return qty if qty > 0 else 100


def load_audit(limit: int = 50) -> List[dict]:
    if not OUTPUTS_KRIS_AUDIT.exists():
        return []
    try:
        rows = json.loads(OUTPUTS_KRIS_AUDIT.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            return []
        return rows[-int(limit):]
    except Exception:
        return []


def get_summary(capital: float = 1_000_000) -> dict:
    kris = get_kris(capital)
    s = kris.get_summary()
    s["max_order_amount"] = kris.pre_trade.max_order_amount
    s["audit_file"] = str(OUTPUTS_KRIS_AUDIT)
    s["audit_tail"] = load_audit(20)
    return s
