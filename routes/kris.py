# -*- coding: utf-8 -*-
# Kris 风控路由 -- REST
"""
GET  /api/kris/summary   -- 审批统计 + 熔断/宏观状态 + 最近审计
GET  /api/kris/audit     -- 最近 N 条审批日志
POST /api/kris/approve   -- 单笔试审批 (不写 outbox)
POST /api/kris/vix       -- 灌入 VIX (宏观门控); source=auto 拉取 QVIX
POST /api/kris/vix/refresh -- 拉取最新 50ETF QVIX
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body

from lib.paths import setup_sys_path
setup_sys_path()

router = APIRouter()


@router.get("/ping")
def kris_ping():
    return {"ok": True, "module": "kris"}


def _capital_from_state() -> float:
    try:
        from lib.live_simulator import load_mock_config
        from routes.live import _load_state
        state = _load_state()
        return float(state.get("capital") or load_mock_config().get("capital") or 1_000_000)
    except Exception:
        return 1_000_000.0


@router.get("/summary")
def kris_summary():
    from lib.kris_adapter import get_summary
    capital = _capital_from_state()
    s = get_summary(capital)
    s["ok"] = True
    s["capital"] = capital
    return s


@router.get("/audit")
def kris_audit(limit: int = 50):
    from lib.kris_adapter import load_audit
    rows = load_audit(limit=max(1, min(int(limit or 50), 500)))
    return {"ok": True, "items": rows, "count": len(rows)}


@router.post("/approve")
def kris_approve_test(payload: Optional[Dict[str, Any]] = Body(None)):
    """试审批, 不真正下单.

    body: {code, side, quantity, price, capital?}
    """
    from lib.kris_adapter import approve_order, decision_to_dict

    payload = payload or {}
    code = str(payload.get("code") or "").strip()
    side = str(payload.get("side") or "buy").strip().lower()
    quantity = int(payload.get("quantity") or 0)
    price = float(payload.get("price") or 0)
    capital = float(payload.get("capital") or _capital_from_state())
    if not code or quantity <= 0 or price <= 0:
        return {"ok": False, "message": "需要 code / quantity>0 / price>0"}

    decision, adj_qty, portfolio = approve_order(
        code=code, side=side, quantity=quantity, price=price, capital=capital,
        news_text=str(payload.get("news_text") or ""),
    )
    return {
        "ok": True,
        "kris": decision_to_dict(decision),
        "adjusted_quantity": adj_qty,
        "portfolio": {
            "total_asset": portfolio.get("total_asset"),
            "atr": (portfolio.get("atr") or {}).get(code),
            "price": (portfolio.get("prices") or {}).get(code),
        },
    }


@router.post("/vix")
def kris_set_vix(payload: Optional[Dict[str, Any]] = Body(None)):
    """灌入 VIX, 更新宏观门控仓位系数.

    body: {vix: number} 手动灌入
    或 {source: "auto"} / {refresh: true} 拉取 50ETF QVIX
    """
    from lib.kris_adapter import ensure_macro_vix, get_kris

    payload = payload or {}
    capital = _capital_from_state()

    # 自动拉取 QVIX
    if payload.get("source") == "auto" or payload.get("refresh"):
        try:
            info = ensure_macro_vix(capital, force=True)
            return {
                "ok": True,
                "vix": info["vix"],
                "date": info.get("date"),
                "source": info.get("source"),
                "coefficient": info["coefficient"],
                "risk_level": info["risk_level"],
            }
        except Exception as e:
            return {"ok": False, "message": f"拉取 QVIX 失败: {type(e).__name__}: {e}"}

    if "vix" not in payload:
        return {"ok": False, "message": "需要 vix 数值, 或 source=auto"}
    vix = float(payload.get("vix"))
    kris = get_kris(capital)
    coef = kris.macro.update_vix(vix)
    return {
        "ok": True,
        "vix": vix,
        "coefficient": coef,
        "risk_level": kris.macro.risk_level,
        "source": "manual",
    }


@router.post("/vix/refresh")
def kris_refresh_qvix():
    """拉取最新 50ETF QVIX 并灌入宏观门控."""
    from lib.kris_adapter import ensure_macro_vix
    try:
        info = ensure_macro_vix(_capital_from_state(), force=True)
        return {"ok": True, **{k: info[k] for k in info if k != "ok"}}
    except Exception as e:
        return {"ok": False, "message": f"拉取 QVIX 失败: {type(e).__name__}: {e}"}
