# -*- coding: utf-8 -*-
# 25-AI量化系统 回测数据加载层
"""
backtest_data -- 回测页 / score_strategies 用的日 K 加载工具

数据源: MySQL trade_stock_daily，不足时用 baostock/akshare（shared.public_market）。

设计:
    - 同一只股票在同一进程内做了 lru_cache (减少重复 SQL)
    - 不抛异常向上吐, 数据不到位时返回 None, 让回测引擎 fail-soft
"""

from __future__ import annotations
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 接入 vendor/shared/public_market（兼容课程仓 shared/）
from lib.ext_bootstrap import ensure_public_market  # noqa: E402
ensure_public_market()
from public_market import load_daily_kline as _pm_load_daily  # noqa: E402


# ============================================================
# MySQL 配置 (作战台 .env / vendor/db_env，兼容 02_ 课程仓)
# ============================================================

def _db_config() -> dict:
    """读 MySQL 配置: 优先根 .env / vendor/db_env 的 QUANT_TRADE_*。"""
    from lib.db_env_loader import mysql_config
    cfg = mysql_config(start=PROJECT_ROOT)
    # 回测连接不强制长超时字段以外的项
    return {
        "host": cfg["host"],
        "user": cfg["user"],
        "password": cfg["password"],
        "database": cfg["database"],
        "port": cfg["port"],
        "charset": cfg.get("charset", "utf8mb4"),
    }


def mysql_available() -> bool:
    """快速检查 MySQL 是否可连 (回测页 ping 用)"""
    try:
        import pymysql
        cfg = _db_config()
        conn = pymysql.connect(connect_timeout=2, **cfg)
        conn.close()
        return True
    except Exception:
        return False


# ============================================================
# 加载日 K -- MySQL
# ============================================================

def _load_from_mysql(stock_code: str,
                     start_date: Optional[str] = None,
                     end_date: Optional[str] = None) -> pd.DataFrame:
    """从 trade_stock_daily 拉日 K (date/open/high/low/close/volume)"""
    import pymysql

    conditions = ["stock_code = %s"]
    params = [stock_code]
    if start_date:
        conditions.append("trade_date >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("trade_date <= %s")
        params.append(end_date)
    sql = f"""
        SELECT trade_date, open_price, high_price, low_price, close_price, volume
        FROM trade_stock_daily
        WHERE {' AND '.join(conditions)}
        ORDER BY trade_date ASC
    """
    cfg = _db_config()
    conn = pymysql.connect(**cfg)
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()
    if not rows:
        raise ValueError(f"MySQL 无数据: {stock_code} ({start_date} ~ {end_date})")
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df.set_index("trade_date", inplace=True)
    df.columns = ["open", "high", "low", "close", "volume"]
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    valid = (df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)
    df = df.loc[valid]
    if df.empty:
        raise ValueError(f"MySQL 数据全为空: {stock_code}")
    return df


# ============================================================
# 加载日 K -- 公开源 fallback (MySQL 无数据时用)
# ============================================================

def _load_from_public(stock_code: str,
                       start_date: Optional[str] = None,
                       end_date: Optional[str] = None) -> pd.DataFrame:
    """公开源日线（baostock/akshare）。"""
    df = _pm_load_daily(stock_code, start_date, end_date)
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[keep].copy()



# ============================================================
# 对外: 统一加载入口 (带 lru_cache)
# ============================================================

def _cache_key(stock_code: str, start: Optional[str], end: Optional[str]) -> tuple:
    return (stock_code, start or "", end or "")


_kline_cache: dict = {}


def load_daily_kline(stock_code: str,
                     start_date: Optional[str] = None,
                     end_date: Optional[str] = None,
                     prefer: str = "auto") -> pd.DataFrame:
    """加载日 K (统一入口)

    Args:
        stock_code:  '600519.SH' / '002432.SZ'
        start_date:  'YYYY-MM-DD' 或 'YYYYMMDD' 或 None
        end_date:    同上
        prefer:      'auto' (MySQL 优先, 失败用公开源) / 'mysql' (只用 MySQL) / 'public' (只用公开源)

    Returns:
        DataFrame, 索引日期, 列 open/high/low/close/volume
    """
    code = (stock_code or "").strip()
    if not code:
        raise ValueError("stock_code 不能为空")
    s = (start_date or "").replace("/", "-")
    e = (end_date or "").replace("/", "-")
    key = _cache_key(code, s, e)
    if key in _kline_cache:
        return _kline_cache[key].copy()

    last_err: Optional[Exception] = None
    if prefer in ("auto", "mysql"):
        try:
            df = _load_from_mysql(code, s or None, e or None)
            _kline_cache[key] = df
            return df.copy()
        except Exception as err:
            last_err = err
            if prefer == "mysql":
                raise
    if prefer in ("auto", "public"):
        try:
            df = _load_from_public(code, s or None, e or None)
            _kline_cache[key] = df
            return df.copy()
        except Exception as err:
            last_err = err
    raise RuntimeError(f"加载 {code} 失败 (MySQL + 公开源 都不可用): {last_err}")


def clear_kline_cache():
    """手动清缓存 (一般不用调; 进程级常驻)"""
    _kline_cache.clear()


# ============================================================
# 简单的"股票名"查询 (回测页表头展示, 用 trade_stock_basic, 没有就返 code)
# ============================================================

@lru_cache(maxsize=512)
def get_stock_name(stock_code: str) -> str:
    """从 wucai_trade.trade_stock_basic 取股票中文简称, 取不到返 code"""
    try:
        import pymysql
        conn = pymysql.connect(connect_timeout=2, **_db_config())
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT stock_name FROM trade_stock_basic WHERE stock_code=%s LIMIT 1",
                (stock_code,)
            )
            row = cursor.fetchone()
            cursor.close()
        finally:
            conn.close()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    return stock_code
