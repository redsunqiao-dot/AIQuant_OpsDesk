# -*- coding: utf-8 -*-
# 实盘委托被券商接受后的副作用: Kris 仓位登记 + 海龟状态
"""dry-run / MiniQMT / 文件桥回执共用, 拒单路径不调用."""

from __future__ import annotations

from typing import Optional


def after_real_order_accepted(
    code: str,
    side: str,
    price: float,
    strategy: str = "",
    quantity: int = 100,
    capital: Optional[float] = None,
) -> None:
    """券商已接受委托后调用 (非拒单).

    - buy: Kris register_position (ATR 2N)
    - sell: Kris remove_position
    - strategy=turtle_donchian: 写 turtle_state (金字塔单元)
    """
    code = str(code or "").strip()
    side = str(side or "").strip().lower()
    strategy = str(strategy or "").strip()
    fill_price = float(price or 0)
    if not code or side not in ("buy", "sell"):
        return

    if fill_price <= 0:
        try:
            from public_market import get_latest_price, to_std_code
            fill_price = float(
                (get_latest_price(to_std_code(code)) or {}).get("lastPrice") or 0
            )
        except Exception:
            fill_price = 0.0

    if capital is None:
        try:
            from lib.paths import OUTPUTS_LIVE_STATE
            import json
            if OUTPUTS_LIVE_STATE.exists():
                st = json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
                capital = float(st.get("capital") or 1_000_000)
            else:
                capital = 1_000_000.0
        except Exception:
            capital = 1_000_000.0

    if side == "buy" and fill_price > 0:
        try:
            from lib.kris_adapter import get_kris, _fetch_atr
            atr_v = _fetch_atr(code)
            if atr_v > 0:
                get_kris(float(capital)).register_position(code, fill_price, atr_v)
        except Exception as e:
            print(f"[WARN] register_position 失败: {e}", flush=True)
    elif side == "sell":
        try:
            from lib.kris_adapter import get_kris
            get_kris(float(capital)).circuit_breaker.remove_position(code)
        except Exception as e:
            print(f"[WARN] remove_position 失败: {e}", flush=True)

    if strategy == "turtle_donchian" and fill_price > 0:
        try:
            from lib.kris_adapter import _fetch_atr
            from lib.turtle_state import apply_fill
            apply_fill(
                code, side, fill_price,
                atr=_fetch_atr(code, period=20),
                unit_shares=int(quantity or 100),
            )
        except Exception as e:
            print(f"[WARN] turtle apply_fill 失败: {e}", flush=True)
