# -*- coding: utf-8 -*-
"""
大 QMT / xtquant 交易封装（兼容旧名 MiniQMTTraderV2）。

行情仍走公开源与 MySQL；本模块只负责交易通道。
大 QMT 的 QMT_PATH 指向安装目录下的 userdata（不是 userdata_mini）。
客户端需先人工登录，再由本模块 connect / 查询 / 下单。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional


def resolve_userdata_path(qmt_path: str) -> str:
    """把安装根目录或 userdata 路径规范成 xtquant 需要的 userdata 目录。

    大 QMT 优先使用 userdata；仅当没有 userdata 时才用 userdata_mini。
    """
    raw = (qmt_path or "").strip().strip('"')
    if not raw:
        raise RuntimeError("QMT_PATH 为空")
    p = Path(raw).expanduser()
    if not p.exists():
        raise RuntimeError(f"QMT_PATH 不存在: {p}")
    if p.name in ("userdata", "userdata_mini") and p.is_dir():
        return str(p.resolve())
    userdata = p / "userdata"
    if userdata.is_dir():
        return str(userdata.resolve())
    mini = p / "userdata_mini"
    if mini.is_dir():
        return str(mini.resolve())
    raise RuntimeError(f"在 {p} 下找不到 userdata 或 userdata_mini")


class MiniQMTTraderV2:
    """XtQuantTrader 包装。类名保留，供 routes/live_loop 直接 import。"""

    def __init__(
        self,
        qmt_path: str,
        account_id: str,
        session_id: Optional[int] = None,
        enable_heartbeat: bool = False,
        enable_reconnect: bool = False,
        account_type: str = "STOCK",
    ):
        self.qmt_path = resolve_userdata_path(qmt_path)
        self.account_id = str(account_id).strip()
        if not self.account_id:
            raise RuntimeError("ACCOUNT_ID 为空")
        if session_id is None:
            session_id = int(os.environ.get("SESSION_ID", "10001") or "10001")
        self.session_id = int(session_id)
        self.account_type = account_type
        self.enable_heartbeat = bool(enable_heartbeat)
        self.enable_reconnect = bool(enable_reconnect)
        self.connected = False
        self._trader = None
        self._account = None

    def connect(self, retries: int = 3, wait_sec: float = 1.0) -> bool:
        """连接并订阅账户。客户端必须已登录。"""
        from xtquant.xttrader import XtQuantTrader
        from xtquant.xttype import StockAccount

        last_err: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                if self._trader is not None:
                    try:
                        self._trader.stop()
                    except Exception:
                        pass
                    self._trader = None

                trader = XtQuantTrader(self.qmt_path, self.session_id)
                trader.start()
                time.sleep(0.3)
                rc = trader.connect()
                if rc != 0:
                    raise RuntimeError(
                        f"XtQuantTrader.connect 返回 {rc}。"
                        f"请确认大 QMT 已登录，且 QMT_PATH={self.qmt_path}"
                    )
                account = StockAccount(self.account_id, self.account_type)
                sub = trader.subscribe(account)
                if sub != 0:
                    raise RuntimeError(
                        f"subscribe 返回 {sub}。"
                        f"请确认账号 {self.account_id} 已在客户端登录"
                    )
                self._trader = trader
                self._account = account
                self.connected = True
                return True
            except Exception as e:
                last_err = e
                self.connected = False
                time.sleep(wait_sec * (attempt + 1))
        raise RuntimeError(f"连接大 QMT 失败: {last_err}")

    def disconnect(self) -> None:
        if self._trader is not None:
            try:
                self._trader.stop()
            except Exception:
                pass
        self._trader = None
        self._account = None
        self.connected = False

    def _ensure(self) -> None:
        if not self.connected or self._trader is None or self._account is None:
            self.connect()

    def query_asset(self) -> dict[str, float]:
        self._ensure()
        asset = self._trader.query_stock_asset(self._account)
        if asset is None:
            return {}
        return {
            "total_asset": float(getattr(asset, "total_asset", 0) or 0),
            "cash": float(getattr(asset, "cash", 0) or 0),
            "market_value": float(getattr(asset, "market_value", 0) or 0),
            "frozen_cash": float(getattr(asset, "frozen_cash", 0) or 0),
        }

    def query_positions(self) -> list[dict[str, Any]]:
        self._ensure()
        rows = self._trader.query_stock_positions(self._account) or []
        out: list[dict[str, Any]] = []
        for p in rows:
            code = str(getattr(p, "stock_code", "") or "")
            if not code:
                continue
            out.append({
                "stock_code": code,
                "volume": int(getattr(p, "volume", 0) or 0),
                "can_use_volume": int(getattr(p, "can_use_volume", 0) or 0),
                "open_price": float(getattr(p, "open_price", 0) or 0),
                "market_value": float(getattr(p, "market_value", 0) or 0),
            })
        return out

    def buy(
        self,
        code: str,
        quantity: int,
        price: float = 0.0,
        strategy_name: str = "",
        remark: str = "",
    ) -> Optional[int]:
        return self._order(code, quantity, price, side="buy",
                           strategy_name=strategy_name, remark=remark)

    def sell(
        self,
        code: str,
        quantity: int,
        price: float = 0.0,
        strategy_name: str = "",
        remark: str = "",
    ) -> Optional[int]:
        return self._order(code, quantity, price, side="sell",
                           strategy_name=strategy_name, remark=remark)

    def _order(
        self,
        code: str,
        quantity: int,
        price: float,
        side: str,
        strategy_name: str = "",
        remark: str = "",
    ) -> Optional[int]:
        from xtquant import xtconstant

        self._ensure()
        qty = int(quantity)
        if qty <= 0:
            raise RuntimeError(f"下单数量无效: {quantity}")
        order_type = xtconstant.STOCK_BUY if side == "buy" else xtconstant.STOCK_SELL
        px = float(price or 0)
        if px > 0:
            price_type = xtconstant.FIX_PRICE
        else:
            price_type = xtconstant.LATEST_PRICE
            px = 0.0
        order_id = self._trader.order_stock(
            self._account,
            str(code).strip(),
            order_type,
            qty,
            price_type,
            px,
            strategy_name or "",
            remark or "",
        )
        if order_id is None or int(order_id) < 0:
            return None
        return int(order_id)

    def cancel(self, order_id: int) -> int:
        self._ensure()
        return int(self._trader.cancel_order_stock(self._account, int(order_id)))


# 旧名兼容
MiniQMTTrader = MiniQMTTraderV2


def connect_trader(qmt_path: str, session_id: int, account_id: str):
    trader = MiniQMTTraderV2(
        qmt_path=qmt_path,
        account_id=account_id,
        session_id=session_id,
    )
    trader.connect()
    return trader._trader, trader._account


def place_order(*args, **kwargs):
    raise RuntimeError("请使用 MiniQMTTraderV2.buy / sell")
