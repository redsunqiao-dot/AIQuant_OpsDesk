# -*- coding: utf-8 -*-
# 23-CASE-A: 盘中全自动交易闭环主循环
"""
LiveLoop -- 盘中全自动交易闭环主循环

每隔 N 分钟跑一遍, 完成: 拉行情 -> 评估持仓 -> 跑信号 -> 风控审批 -> 下单 -> 推送

核心架构 (LangGraph 风格, 但简化为顺序循环, 因为每分钟级延迟比 LangGraph 启动开销重要):

    每分钟循环:
        1. health_check()          检查 miniQMT 连接 + 行情数据完整性
        2. update_positions()      拉最新持仓 + 当日盈亏
        3. check_circuit_breaker() 当日亏损是否触发熔断
        4. evaluate_stop_loss()    持仓股是否触发止损
        5. evaluate_signals()      候选股是否出现新信号
        6. risk_check()            风控审批 (Kris 规则)
        7. place_orders()          下单 (本 CASE live_trading.miniqmt_trader_v2)
        8. push_summary()          推送告警 (alert_router)
        9. save_state()            落盘 state (供 CEO 控制台读)

异常处理金字塔:
    L1 数据层异常 -> 跳过本轮, 下轮继续, 不告警 (网络抖动)
    L2 风控否决   -> 不下单, INFO 推送
    L3 订单失败   -> WARN 推送, 重试 1 次
    L4 系统级异常 -> CRITICAL 推送 + 暂停所有交易 (state.trading_status = "HALTED")
    L5 不可恢复   -> FATAL 推送 + 进程退出 + 等人工

注意:
    - 真正的实盘需要接 miniQMT, 在 dry-run 下用模拟数据 (适合教学/演示)
    - 信号评估这里可用 MACD/RSI 占位，实战可替换为自有选股 / 路由输出。
"""

from __future__ import annotations
import sys
from pathlib import Path as _Path
# 公开行情: 优先本树 vendor/shared
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from lib.ext_bootstrap import ensure_public_market
ensure_public_market()
from public_market import load_daily_kline, load_minute_kline, get_latest_price, to_std_code
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from alerting.alert_router import AlertRouter
from live_trading.state_store import StateStore


# ============================================================
# 数据来源 (日线 MySQL/公开源 + 分钟线新浪)
# ============================================================

class MarketDataProvider:
    """市场数据提供者 -- 日线走 MySQL/公开源；分钟线走新浪。"""

    def __init__(self):
        self._connected = False

    def connect(self):
        self._connected = True
        return True

    def get_latest_tick(self, stock_code: str) -> dict:
        """优先用最近一根 1 分钟收盘作现价；拿不到再用日线收盘。"""
        if not self._connected:
            self.connect()
        code = to_std_code(stock_code)
        try:
            df = load_minute_kline(code, period="1m", count=3)
            last = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else last
            return {
                "lastPrice": float(last["close"]),
                "lastClose": float(prev["close"]),
                "open": float(last["open"]),
                "high": float(last["high"]),
                "low": float(last["low"]),
                "volume": int(last["volume"]) if "volume" in last and pd.notna(last["volume"]) else 0,
                "amount": float(last["amount"]) if "amount" in last and pd.notna(last["amount"]) else 0.0,
            }
        except Exception:
            info = get_latest_price(code)
            return {
                "lastPrice": info["lastPrice"],
                "lastClose": info["lastClose"],
                "open": info["open"],
                "high": info["high"],
                "low": info["low"],
                "volume": info["volume"],
                "amount": info["amount"],
            }

    def get_recent_kline(self, stock_code: str, period: str = "5m",
                        count: int = 50) -> Optional[Any]:
        """按 period 拉 K 线: 1d 走日线；1m/5m/... 走分钟线（失败返回 None，不日线冒充）。"""
        if not self._connected:
            self.connect()
        code = to_std_code(stock_code)
        p = str(period or "5m").strip().lower()
        try:
            if p in ("1d", "day", "daily", "d"):
                df = load_daily_kline(code, count=count)
            else:
                df = load_minute_kline(code, period=p, count=count)
            if df is None or len(df) == 0:
                return None
            df = df.copy()
            df.index = pd.to_datetime(df.index)
            return df
        except Exception as e:
            print(f"[WARN] get_recent_kline {code} period={period}: {e}", flush=True)
            return None


# ============================================================
# 信号评估 (简化版 MACD)
# ============================================================

def evaluate_macd_signal(df) -> str:
    """
    评估 MACD 信号
    返回: "buy" / "sell" / "hold"
    """
    import pandas as pd
    if df is None or len(df) < 30:
        return "hold"
    close = df["close"].astype(float)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()

    # 最新两根: 看是否金叉/死叉
    if len(dif) < 2:
        return "hold"
    prev = dif.iloc[-2] - dea.iloc[-2]
    curr = dif.iloc[-1] - dea.iloc[-1]
    if prev <= 0 and curr > 0:
        return "buy"
    if prev >= 0 and curr < 0:
        return "sell"
    return "hold"


# ============================================================
# 持仓与盈亏更新
# ============================================================

def update_positions_from_market(positions: List[dict],
                                 market: MarketDataProvider) -> List[dict]:
    """拉最新价更新持仓的市值 + 浮动盈亏"""
    updated = []
    for pos in positions:
        code = pos["code"]
        tick = market.get_latest_tick(code)
        cur_price = float(tick.get("lastPrice", pos.get("cost", 0)))
        volume = int(pos["volume"])
        cost = float(pos.get("cost", 0))
        mv = volume * cur_price
        pnl = (cur_price - cost) * volume
        pnl_pct = (cur_price - cost) / cost if cost > 0 else 0
        updated.append({
            **pos,
            "cur_price":   round(cur_price, 3),
            "market_value": round(mv, 2),
            "pnl":         round(pnl, 2),
            "pnl_pct":     round(pnl_pct, 4),
        })
    return updated


def calc_today_pnl(positions: List[dict], capital: float) -> tuple:
    """计算当日总盈亏 (元 + 百分比)"""
    total_pnl = sum(p.get("pnl", 0) for p in positions)
    total_pct = total_pnl / capital if capital > 0 else 0
    return round(total_pnl, 2), round(total_pct, 4)


def _position_index(positions: List[dict], code: str) -> int:
    for i, pos in enumerate(positions):
        if pos.get("code") == code:
            return i
    return -1


def _mark_position(pos: dict, volume: int, cost: float, price: float) -> dict:
    """按股数、成本、现价重算市值和浮动盈亏。"""
    volume = int(volume)
    cost = float(cost)
    price = float(price)
    pnl = (price - cost) * volume
    pnl_pct = (price - cost) / cost if cost > 0 else 0.0
    return {
        **pos,
        "volume":       volume,
        "cost":         round(cost, 4),
        "cur_price":    round(price, 4),
        "market_value": round(volume * price, 2),
        "pnl":          round(pnl, 2),
        "pnl_pct":      round(pnl_pct, 4),
    }


def dry_filled_today(state: dict, code: str, side: str) -> bool:
    """同一天、同一只、同一方向已经模拟成交过，或卖出已因无持仓拒绝过。"""
    today = datetime.now().strftime("%Y-%m-%d")
    for order in state.get("orders") or []:
        ts = str(order.get("ts") or "")
        if ts[:10] != today:
            continue
        if order.get("code") != code or order.get("side") != side:
            continue
        if order.get("status") == "dry_run":
            return True
        if order.get("status") == "rejected" and order.get("reason") == "无持仓":
            return True
    return False


def apply_dry_fill(state: dict, code: str, side: str, quantity: int, price: float) -> tuple:
    """把一笔模拟成交写进 state['positions']。

    买入: 没有则新开仓，已有则摊成本。
    卖出: 只卖手里有的，卖完删除该行。没有持仓返回拒绝原因。
    返回 (拒绝原因或 None, 实际成交股数)。
    """
    positions = list(state.get("positions") or [])
    idx = _position_index(positions, code)
    quantity = int(quantity)
    price = float(price)
    if side == "sell":
        if idx < 0 or int(positions[idx].get("volume") or 0) <= 0:
            return "无持仓", 0
        held = int(positions[idx]["volume"])
        quantity = min(quantity, held)
        left = held - quantity
        if left <= 0:
            positions.pop(idx)
        else:
            positions[idx] = _mark_position(
                positions[idx], left, float(positions[idx].get("cost") or price), price,
            )
        state["positions"] = positions
        return None, quantity

    if idx < 0:
        positions.append(_mark_position(
            {"code": code, "name": ""}, quantity, price, price,
        ))
    else:
        held = int(positions[idx].get("volume") or 0)
        old_cost = float(positions[idx].get("cost") or price)
        new_vol = held + quantity
        new_cost = (old_cost * held + price * quantity) / new_vol
        positions[idx] = _mark_position(positions[idx], new_vol, new_cost, price)
    state["positions"] = positions
    return None, quantity


# ============================================================
# 主循环
# ============================================================

class LiveTradingLoop:
    """
    实盘主循环 (默认 dry-run)

    用法:
        loop = LiveTradingLoop(watch_stocks=["600519.SH", "513100.SH"])
        loop.run_once()         # 跑一次
        loop.run_forever(60)    # 每 60 秒跑一次, 直到 Ctrl+C
    """

    def __init__(self,
                 watch_stocks: List[str],
                 capital: float = 1_000_000,
                 state_file: str = "outputs/live_state.json",
                 max_daily_loss_pct: float = -0.02,
                 dry_run: bool = True,
                 signal_evaluator: Optional[Callable[[str, "MarketDataProvider", float], dict]] = None):
        """
        signal_evaluator: 可选的信号评估器, 签名 (code, market, capital) -> dict
            返回字典 {"side": "buy"/"sell"/"hold", "strategy": str, "reason": str (可选)}
            不传 = 沿用默认 5min MACD 金叉/死叉 (兼容历史行为)
        """
        self.watch_stocks = watch_stocks
        self.capital = capital
        self.dry_run = dry_run
        self.max_daily_loss_pct = max_daily_loss_pct
        self.signal_evaluator = signal_evaluator

        self.state_store = StateStore(state_file)
        self.market = MarketDataProvider()
        self.alert = AlertRouter(info_aggregate_seconds=300)

        # 初始化 state
        s = self.state_store.load()
        s["capital"] = capital
        s["watch_stocks"] = watch_stocks
        s["control"]["dry_run"] = dry_run
        s["control"]["max_daily_loss"] = max_daily_loss_pct
        self.state_store.save(s)

    # ------------------------------------------------------------------
    # 单次循环
    # ------------------------------------------------------------------
    def run_once(self) -> dict:
        """跑一次完整循环"""
        cycle_start = time.time()
        s = self.state_store.load()

        # 0) 检查 control.trading_status
        if s.get("trading_status") == "HALTED":
            self.alert.alert("WARN", "交易已熔断, 跳过本轮", source="loop")
            return {"action": "halted_skip"}
        if s.get("trading_status") == "PAUSED":
            self.alert.alert("INFO", "交易已暂停 (CEO 控制台暂停)", source="loop")
            return {"action": "paused_skip"}

        # 1) health_check
        try:
            self.market.connect()
            s["health"]["miniqmt_connected"] = True
            s["health"]["last_heartbeat"] = datetime.now().isoformat(timespec="seconds")
        except Exception as e:
            s["health"]["miniqmt_connected"] = False
            s["health"]["errors_24h"] = s["health"].get("errors_24h", 0) + 1
            self.alert.alert("CRITICAL", "miniQMT 连接失败",
                             message=str(e), source="health")
            self.state_store.save(s)
            return {"action": "health_fail"}

        # 2) 更新持仓 + 当日盈亏
        positions = s.get("positions", [])
        if positions:
            positions = update_positions_from_market(positions, self.market)
            today_pnl, today_pnl_pct = calc_today_pnl(positions, self.capital)
            s["positions"] = positions
            s["today_pnl"] = today_pnl
            s["today_pnl_pct"] = today_pnl_pct
            s["pnl_history"] = s.get("pnl_history", [])
            s["pnl_history"].append({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "pnl": today_pnl, "pnl_pct": today_pnl_pct,
            })
            s["pnl_history"] = s["pnl_history"][-500:]

        # 3) 熔断检查
        if s.get("today_pnl_pct", 0) <= self.max_daily_loss_pct:
            s["trading_status"] = "HALTED"
            self.alert.alert(
                "CRITICAL", "触发当日亏损熔断",
                message=f"今日累计盈亏 {s['today_pnl_pct']:.2%}, "
                        f"已跌破熔断线 {self.max_daily_loss_pct:.2%}",
                source="circuit_breaker",
            )
            self.state_store.save(s)
            return {"action": "circuit_breaker"}

        # 4) 评估信号 (默认对 watch 池每只算 MACD; 注入了 signal_evaluator 则按 evaluator 派发)
        new_signals = []
        for code in self.watch_stocks:
            if self.signal_evaluator is not None:
                # 外部注入的信号路由器: 由 evaluator 自己决定用哪个策略
                try:
                    result = self.signal_evaluator(code, self.market, self.capital)
                except Exception as e:
                    self.alert.alert("WARN", f"signal_evaluator 异常 {code}",
                                     message=str(e), source="zoe")
                    continue
                if not result:
                    continue
                side = result.get("side", "hold")
                if side == "hold":
                    continue
                sig = {
                    "code":     code,
                    "side":     side,
                    "strategy": result.get("strategy", "unknown"),
                    "reason":   result.get("reason", ""),
                }
            else:
                df = self.market.get_recent_kline(code, period="5m", count=50)
                side = evaluate_macd_signal(df)
                if side == "hold":
                    continue
                sig = {"code": code, "side": side, "strategy": "macd_5min"}
            new_signals.append(sig)

        if new_signals:
            for sig in new_signals:
                self.state_store.append_signal(sig)
                self.alert.alert(
                    "INFO", f"信号触发 -> {sig['side']} {sig['code']} [{sig.get('strategy','')}]",
                    source="zoe",
                )
            # append_signal 已落盘; 必须把最新 signals 同步回本轮内存 s,
            # 否则结尾 state_store.save(s) 会用旧 s 把刚追加的信号盖掉,
            # 表现为日志一直刷信号、实盘「待授权」却始终没有。
            try:
                latest = self.state_store.load()
                s["signals"] = latest.get("signals") or s.get("signals") or []
            except Exception:
                pass

        # 5) 风控 + 下单
        for sig in new_signals:
            order_result = self._handle_signal(s, sig)
            if order_result.get("status") == "skipped":
                continue
            # 把触发该订单的策略名一起记录, 便于复盘
            if "strategy" not in order_result and sig.get("strategy"):
                order_result["strategy"] = sig["strategy"]
            # 记在本轮 state 上，避免本轮结束时的整包 save 把订单冲掉
            orders = list(s.get("orders") or [])
            orders.append(order_result)
            s["orders"] = orders[-100:]
            self.state_store.append_order(order_result)

        # 6) 落盘 state (含本轮事件)
        s["events"] = s.get("events", [])
        s["events"].append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "type": "loop_cycle",
            "signal_count": len(new_signals),
            "duration_ms": int((time.time() - cycle_start) * 1000),
        })
        s["events"] = s["events"][-200:]
        self.state_store.save(s)

        return {
            "action":      "cycle_done",
            "duration_ms": int((time.time() - cycle_start) * 1000),
            "new_signals": len(new_signals),
        }

    def _handle_signal(self, state: dict, signal: dict) -> dict:
        """处理一个信号: Kris 风控 -> 下单 -> 推送"""
        code = signal["code"]
        side = signal["side"]
        tick = self.market.get_latest_tick(code)
        price = float(tick.get("lastPrice", 0))
        if price <= 0:
            return {"code": code, "side": side, "status": "rejected",
                    "reason": "拿不到价格", "ts": datetime.now().isoformat()}

        # 基础仓位 + Kris 8 条规则审批 (保留原 10% 上限, 迁入 Kris)
        from lib.kris_adapter import approve_order, suggest_quantity
        quantity = suggest_quantity(code, price, self.capital)
        decision, quantity, _pf = approve_order(
            code=code, side=side, quantity=quantity, price=price,
            capital=self.capital,
            news_text=str(signal.get("news_text") or ""),
        )
        amount = quantity * price
        kris_info = {
            "decision": decision.decision.value,
            "rule": decision.rule_name,
            "reason": decision.reason,
            "max_position_pct": decision.max_position_pct,
        }
        if not decision.is_approved:
            self.alert.alert(
                "WARN" if decision.decision.value != "halt" else "CRITICAL",
                f"Kris 否决 {side} {code}",
                message=decision.reason,
                source="kris",
            )
            if decision.decision.value == "halt":
                state["trading_status"] = "HALTED"
            return {"code": code, "side": side, "quantity": quantity,
                    "price": price, "amount": amount, "status": "rejected_by_kris",
                    "reason": decision.reason, "kris": kris_info,
                    "ts": datetime.now().isoformat()}

        # control.pause_buying 拦截
        if side == "buy" and state.get("control", {}).get("pause_buying"):
            self.alert.alert("INFO", "买入被 CEO 控制台暂停",
                             message=f"{code} {quantity}股 @ {price:.2f}",
                             source="control")
            return {"code": code, "side": side, "quantity": quantity,
                    "price": price, "status": "paused_by_ceo", "kris": kris_info}

        # 下单 (dry-run / real)
        if self.dry_run:
            if dry_filled_today(state, code, side):
                return {"code": code, "side": side, "status": "skipped",
                        "reason": "当日已处理", "ts": datetime.now().isoformat()}
            reason, quantity = apply_dry_fill(state, code, side, quantity, price)
            if reason:
                return {"code": code, "side": side, "quantity": quantity,
                        "price": price, "status": "rejected",
                        "reason": reason, "kris": kris_info,
                        "ts": datetime.now().isoformat()}
            amount = quantity * price
            # 买入成交后登记 ATR 2N 止损 + 海龟状态 (统一走 post_fill)
            try:
                from lib.post_fill import after_real_order_accepted
                after_real_order_accepted(
                    code=code, side=side, price=price,
                    strategy=str(signal.get("strategy") or ""),
                    quantity=quantity, capital=self.capital,
                )
            except Exception as e:
                print(f"[WARN] post_fill 失败: {e}", flush=True)
            self.alert.alert(
                "INFO", f"[DRY-RUN] 下单 {side} {code} {quantity}股 @ {price:.2f}",
                message=f"Kris={decision.decision.value}",
                source="trader",
            )
            return {"code": code, "side": side, "quantity": quantity,
                    "price": price, "amount": amount, "status": "dry_run",
                    "kris": kris_info, "ts": datetime.now().isoformat()}

        # 真实下单 (本 CASE 内 live_trading/miniqmt_trader_v2)
        try:
            from live_trading.miniqmt_trader_v2 import MiniQMTTraderV2
            trader = MiniQMTTraderV2(
                qmt_path=os.environ["QMT_PATH"],
                account_id=os.environ["ACCOUNT_ID"],
                session_id=int(os.environ.get("SESSION_ID", "10001") or "10001"),
                enable_heartbeat=False,
            )
            trader.connect()
            if side == "buy":
                order_id = trader.buy(code, quantity, price=price,
                                      strategy_name="live_loop")
            else:
                order_id = trader.sell(code, quantity, price=price,
                                       strategy_name="live_loop")
            trader.disconnect()

            if order_id:
                # 实盘委托成功后登记 Kris ATR 止损 + 海龟状态
                try:
                    from lib.post_fill import after_real_order_accepted
                    after_real_order_accepted(
                        code=code, side=side, price=price,
                        strategy=str(signal.get("strategy") or ""),
                        quantity=quantity, capital=self.capital,
                    )
                except Exception as e:
                    print(f"[WARN] post_fill 失败: {e}", flush=True)
                self.alert.alert(
                    "INFO", f"实盘下单成功 {side} {code}",
                    message=f"委托编号 {order_id}, {quantity}股 @ {price:.2f}",
                    source="trader",
                )
                return {"code": code, "side": side, "quantity": quantity,
                        "price": price, "amount": amount, "status": "submitted",
                        "order_id": order_id, "kris": kris_info,
                        "ts": datetime.now().isoformat()}
            else:
                self.alert.alert("WARN", f"实盘下单失败 {code}", source="trader")
                return {"code": code, "side": side, "quantity": quantity,
                        "price": price, "status": "failed", "kris": kris_info,
                        "ts": datetime.now().isoformat()}
        except Exception as e:
            self.alert.alert("CRITICAL", f"下单异常 {code}",
                             message=str(e), source="trader")
            return {"code": code, "side": side, "status": "exception",
                    "reason": str(e), "kris": kris_info,
                    "ts": datetime.now().isoformat()}

    # ------------------------------------------------------------------
    # 长跑模式
    # ------------------------------------------------------------------
    def run_forever(self, interval_seconds: int = 60):
        """每隔 N 秒跑一次, 直到 Ctrl+C"""
        self.alert.alert("INFO", "实盘主循环启动",
                         message=f"watch={self.watch_stocks}, "
                                 f"interval={interval_seconds}s, dry_run={self.dry_run}",
                         source="loop")
        try:
            while True:
                t0 = time.time()
                result = self.run_once()
                # 等到下一次触发
                elapsed = time.time() - t0
                if elapsed < interval_seconds:
                    time.sleep(interval_seconds - elapsed)
        except KeyboardInterrupt:
            self.alert.alert("INFO", "实盘主循环退出 (Ctrl+C)", source="loop")
            self.alert.shutdown()


# ============================================================
# CLI
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘中全自动交易闭环")
    parser.add_argument("--stocks", default="600519.SH,513100.SH",
                        help="监控股票池, 逗号分隔")
    parser.add_argument("--capital", type=float, default=1_000_000)
    parser.add_argument("--interval", type=int, default=60,
                        help="循环间隔秒, 默认 60")
    parser.add_argument("--once", action="store_true", help="只跑一次")
    parser.add_argument("--state-file", default="outputs/live_state.json")
    args = parser.parse_args()

    stocks = [s.strip() for s in args.stocks.split(",") if s.strip()]
    loop = LiveTradingLoop(
        watch_stocks=stocks,
        capital=args.capital,
        state_file=args.state_file,
        dry_run=os.environ.get("TRADER_DRY_RUN", "1") == "1",
    )

    if args.once:
        result = loop.run_once()
        print(f"\n[完成] {result}")
        print(f"\nstate 落盘: {args.state_file}")
    else:
        loop.run_forever(args.interval)


if __name__ == "__main__":
    main()
