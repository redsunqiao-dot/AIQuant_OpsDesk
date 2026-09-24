# -*- coding: utf-8 -*-
"""
全仓库统一公开行情入口（替代 xtquant / xtdata）。

优先级:
  1) MySQL trade_stock_daily（02 采集入库）
  2) baostock / akshare（02_行情数据采集/market_data_source.py）

股票代码统一: 600519.SH / 000001.SZ
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

# OpsDesk vendor: vendor/shared + vendor/market
_SHARED_DIR = Path(__file__).resolve().parent
_VENDOR_DIR = _SHARED_DIR.parent
_MARKET_DIR = _VENDOR_DIR / "market"
_OPSDESK_ROOT = _VENDOR_DIR.parent if (_VENDOR_DIR.parent / "app.py").exists() else None

_SRC02 = _MARKET_DIR
if not (_SRC02 / "market_data_source.py").exists():
    for _p in _SHARED_DIR.parents:
        _cand = _p / "02_行情数据采集"
        if (_cand / "market_data_source.py").exists():
            _SRC02 = _cand
            break
if str(_SRC02) not in sys.path:
    sys.path.insert(0, str(_SRC02))

from market_data_source import (  # noqa: E402
    baostock_login,
    baostock_logout,
    fetch_daily,
    get_stock_list,
    to_baostock_code,
    to_std_code,
)


def _ymd(s: Optional[str], default: str) -> str:
    if not s:
        return default
    return str(s).replace("-", "").replace("/", "")[:8]


def _load_mysql_daily(
    stock_code: str,
    start_ymd: Optional[str] = None,
    end_ymd: Optional[str] = None,
) -> pd.DataFrame:
    """从 trade_stock_daily 读取日线。失败抛异常。"""
    # 延迟导入，避免无 pymysql 时影响纯 baostock 场景
    import pymysql
    from dotenv import dotenv_values

    env_maps = []
    if _OPSDESK_ROOT is not None:
        env_maps.append(dotenv_values(_OPSDESK_ROOT / ".env") or {})
        env_maps.append(dotenv_values(_OPSDESK_ROOT / "vendor" / "db_env" / ".env") or {})
    env_maps.append(dotenv_values(_SRC02 / ".env") or {})

    def g(*keys: str, default: str = "") -> str:
        for env in env_maps:
            for k in keys:
                v = env.get(k) if isinstance(env, dict) else None
                if v and str(v).strip():
                    return str(v).strip()
        return default

    cfg = {
        "host": g("QUANT_TRADE_CHARLES_DB_HOST", "WUCAI_SQL_HOST", default="localhost"),
        "user": g("QUANT_TRADE_CHARLES_DB_USER", "WUCAI_SQL_USERNAME", default="root"),
        "password": g("QUANT_TRADE_CHARLES_DB_PASSWORD", "WUCAI_SQL_PASSWORD"),
        "database": g("QUANT_TRADE_CHARLES_DB_NAME", "WUCAI_SQL_DB", default="quant_trade"),
        "port": int(g("QUANT_TRADE_CHARLES_DB_PORT", "WUCAI_SQL_PORT", default="3306")),
        "charset": "utf8mb4",
        "connect_timeout": 10,
    }
    code = to_std_code(stock_code)
    cond = ["stock_code = %s"]
    params: list[Any] = [code]
    if start_ymd:
        cond.append("trade_date >= %s")
        params.append(f"{start_ymd[:4]}-{start_ymd[4:6]}-{start_ymd[6:8]}")
    if end_ymd and end_ymd < "20990101":
        cond.append("trade_date <= %s")
        params.append(f"{end_ymd[:4]}-{end_ymd[4:6]}-{end_ymd[6:8]}")
    sql = f"""
        SELECT trade_date, open_price, high_price, low_price, close_price, volume, amount
        FROM trade_stock_daily
        WHERE {' AND '.join(cond)}
        ORDER BY trade_date ASC
    """
    conn = pymysql.connect(**cfg)
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    if not rows:
        raise ValueError(f"MySQL 无数据: {code}")
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.set_index("trade_date").sort_index()
    out = pd.DataFrame(
        {
            "open": pd.to_numeric(df["open_price"], errors="coerce"),
            "high": pd.to_numeric(df["high_price"], errors="coerce"),
            "low": pd.to_numeric(df["low_price"], errors="coerce"),
            "close": pd.to_numeric(df["close_price"], errors="coerce"),
            "volume": pd.to_numeric(df["volume"], errors="coerce"),
            "amount": pd.to_numeric(df.get("amount"), errors="coerce")
            if "amount" in df.columns
            else 0.0,
        },
        index=df.index,
    )
    out = out.dropna(subset=["close"])
    out = out[out["close"] > 0]
    if out.empty:
        raise ValueError(f"MySQL 数据无效: {code}")
    return out


def load_daily_kline(
    stock_code: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    count: Optional[int] = None,
) -> pd.DataFrame:
    """
    日线 OHLCV。索引 DatetimeIndex，列 open/high/low/close/volume[/amount]。
    先 MySQL，再公开源。
    """
    code = to_std_code(stock_code)
    start = _ymd(start_date, "20150101")
    end = _ymd(end_date, datetime.now().strftime("%Y%m%d"))
    try:
        df = _load_mysql_daily(code, start, end)
    except Exception:
        df = fetch_daily(code, start, end)
        # fetch_daily 已带 amount/turnover
        keep = [c for c in ("open", "high", "low", "close", "volume", "amount") if c in df.columns]
        df = df[keep].copy()
    if count and count > 0 and len(df) > count:
        df = df.iloc[-count:]
    return df


def load_daily_bars_records(
    stock_code: str,
    start_date: str = "",
    end_date: str = "",
    count: int = 100,
) -> dict:
    """兼容原 get_kline.py 的 JSON 结构。仅支持日线。"""
    code = to_std_code(stock_code)
    try:
        df = load_daily_kline(code, start_date or None, end_date or None, count=count or None)
        records = []
        for ts, row in df.iterrows():
            records.append(
                {
                    "date": pd.Timestamp(ts).strftime("%Y-%m-%d"),
                    "open": round(float(row["open"]), 3),
                    "high": round(float(row["high"]), 3),
                    "low": round(float(row["low"]), 3),
                    "close": round(float(row["close"]), 3),
                    "volume": int(row["volume"]) if pd.notna(row["volume"]) else 0,
                    "amount": float(row["amount"]) if "amount" in row and pd.notna(row["amount"]) else 0.0,
                }
            )
        return {
            "stock_code": code,
            "period": "1d",
            "data_count": len(records),
            "data": records[-50:] if len(records) > 50 else records,
            "source": "mysql_or_public",
        }
    except Exception as e:
        return {"error": f"获取失败: {e}"}


def get_latest_price(stock_code: str) -> dict:
    """用日线最新收盘近似「现价」（无 Level-1 实时源）。"""
    code = to_std_code(stock_code)
    df = load_daily_kline(code, count=5)
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    close = float(last["close"])
    pre = float(prev["close"])
    return {
        "stock_code": code,
        "lastPrice": close,
        "lastClose": pre,
        "open": float(last["open"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "volume": int(last["volume"]) if pd.notna(last["volume"]) else 0,
        "amount": float(last["amount"]) if "amount" in last and pd.notna(last["amount"]) else 0.0,
    }


# 分钟 K 进程内短缓存: (code, period) -> (expire_ts, df)；降低直播循环频控压力
_MINUTE_CACHE: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
_MINUTE_CACHE_TTL_SEC = 45.0

_PERIOD_TO_SINA = {
    "1m": "1", "1min": "1", "1": "1",
    "5m": "5", "5min": "5", "5": "5",
    "15m": "15", "15min": "15", "15": "15",
    "30m": "30", "30min": "30", "30": "30",
    "60m": "60", "60min": "60", "1h": "60", "60": "60",
}


def to_sina_symbol(stock_code: str) -> str:
    """600519.SH -> sh600519；000001.SZ -> sz000001。"""
    code = to_std_code(stock_code)
    num, mkt = code.split(".", 1)
    prefix = "sh" if mkt.upper() == "SH" else "sz"
    return f"{prefix}{num}"


def load_minute_kline(
    stock_code: str,
    period: str = "5m",
    count: Optional[int] = 100,
) -> pd.DataFrame:
    """
    分钟 OHLCV（新浪 stock_zh_a_minute）。

    索引 DatetimeIndex，列 open/high/low/close/volume[/amount]。
    period: 1m / 5m / 15m / 30m / 60m。
    """
    import time
    import akshare as ak

    code = to_std_code(stock_code)
    pkey = str(period).strip().lower()
    sina_period = _PERIOD_TO_SINA.get(pkey)
    if not sina_period:
        raise ValueError(f"不支持的分钟周期: {period}")

    cache_key = (code, sina_period)
    now = time.time()
    hit = _MINUTE_CACHE.get(cache_key)
    if hit and hit[0] > now:
        df = hit[1]
    else:
        symbol = to_sina_symbol(code)
        raw = ak.stock_zh_a_minute(symbol=symbol, period=sina_period, adjust="")
        if raw is None or len(raw) == 0:
            raise ValueError(f"分钟线为空: {code} period={period}")
        df = raw.copy()
        # 列名兼容: day / 时间
        time_col = "day" if "day" in df.columns else ("时间" if "时间" in df.columns else None)
        if time_col is None:
            raise ValueError(f"分钟线无时间列: {list(df.columns)}")
        df.index = pd.to_datetime(df[time_col])
        rename = {}
        for src, dst in (
            ("open", "open"), ("high", "high"), ("low", "low"), ("close", "close"),
            ("volume", "volume"), ("amount", "amount"),
            ("开盘", "open"), ("最高", "high"), ("最低", "low"), ("收盘", "close"),
            ("成交量", "volume"), ("成交额", "amount"),
        ):
            if src in df.columns:
                rename[src] = dst
        df = df.rename(columns=rename)
        keep = [c for c in ("open", "high", "low", "close", "volume", "amount") if c in df.columns]
        df = df[keep].apply(pd.to_numeric, errors="coerce").dropna(subset=["close"])
        df = df[df["close"] > 0].sort_index()
        if df.empty:
            raise ValueError(f"分钟线无效: {code}")
        _MINUTE_CACHE[cache_key] = (now + _MINUTE_CACHE_TTL_SEC, df)

    if count and count > 0 and len(df) > count:
        return df.iloc[-count:].copy()
    return df.copy()


def get_hs300_codes() -> list[str]:
    """沪深300成分股。"""
    import akshare as ak

    try:
        df = ak.index_stock_cons(symbol="000300")
        col = "品种代码" if "品种代码" in df.columns else df.columns[0]
        return [to_std_code(str(c)) for c in df[col].tolist()]
    except Exception:
        df = ak.index_stock_cons_csindex(symbol="000300")
        col = "成分券代码" if "成分券代码" in df.columns else "股票代码"
        return [to_std_code(str(c)) for c in df[col].tolist()]


def get_stock_name_map(codes: list[str] | None = None) -> dict[str, str]:
    """代码 -> 名称。"""
    import akshare as ak

    raw = ak.stock_info_a_code_name()
    mapping = {to_std_code(str(r["code"])): str(r["name"]) for _, r in raw.iterrows()}
    if codes is None:
        return mapping
    return {c: mapping.get(to_std_code(c), to_std_code(c)) for c in codes}


def _industry_from_baostock() -> dict[str, dict[str, str]]:
    """证监会行业分类（baostock），用作申万接口不可用时的兜底。"""
    import re
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")
    try:
        rs = bs.query_stock_industry()
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        fields = rs.fields
    finally:
        bs.logout()

    out: dict[str, dict[str, str]] = {}
    for row in rows:
        rec = dict(zip(fields, row))
        raw = str(rec.get("industry") or "").strip()
        if not raw:
            continue
        # C39计算机... -> 门类字母作一级，去掉代码后的名称作二级
        m = re.match(r"^([A-Z]\d*)(.+)$", raw)
        if m:
            sector_1 = m.group(1)[:1]  # 门类：C/I/J...
            sector_2 = m.group(2).strip() or raw
        else:
            sector_1, sector_2 = "其他", raw
        code = to_std_code(str(rec.get("code") or ""))
        if not code:
            continue
        out[code] = {
            "sector_1": sector_1,
            "sector_2": sector_2,
            "stock_name": str(rec.get("code_name") or code),
        }
    print(f"  [baostock] 行业分类 {len(out)} 只股票")
    return out


_SW_LIST_URL = "https://www.swsresearch.com/institute-sw/api/index_publish/current/"
_SW_CONS_URL = "https://www.swsresearch.com/institute-sw/api/index_publish/details/component_stocks/"
_SW_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _sw_get_json(session, url: str, params: dict) -> dict:
    """申万官网接口。证书链不完整，关闭校验。"""
    resp = session.get(url, params=params, headers=_SW_HEADERS, timeout=20, verify=False)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("申万接口返回不是 JSON 对象")
    return payload


def _sw_index_list(session, level_name: str) -> list[tuple[str, str]]:
    """拉取申万一级或二级指数代码与名称。"""
    import math

    first = _sw_get_json(session, _SW_LIST_URL, {
        "page": "1", "page_size": "50", "indextype": level_name,
    })
    data = first.get("data") or {}
    results = list(data.get("results") or [])
    total = int(data.get("count") or len(results))
    pages = max(1, math.ceil(total / 50)) if total else 1
    for page in range(2, pages + 1):
        payload = _sw_get_json(session, _SW_LIST_URL, {
            "page": str(page), "page_size": "50", "indextype": level_name,
        })
        results.extend((payload.get("data") or {}).get("results") or [])
    rows: list[tuple[str, str]] = []
    for item in results:
        code = str(item.get("swindexcode") or "").strip()
        name = str(item.get("swindexname") or "").strip()
        if code and name:
            rows.append((code, name))
    return rows


def _sw_parent_name(l2_code: str, level1: list[tuple[str, str]]) -> str:
    """二级代码归属不超过它的最大一级代码。"""
    code = int(str(l2_code).split(".")[0])
    parent = ""
    parent_code = -1
    for raw_code, name in level1:
        current = int(str(raw_code).split(".")[0])
        if parent_code < current <= code:
            parent_code = current
            parent = name
    return parent


def _sw_constituents(session, index_code: str) -> list[tuple[str, str]]:
    """拉取某个申万行业指数的成分股 (代码, 名称)。"""
    import math

    code = str(index_code).split(".")[0]
    page_size = 10000
    first = _sw_get_json(session, _SW_CONS_URL, {
        "swindexcode": code, "page": "1", "page_size": str(page_size),
    })
    data = first.get("data") or {}
    results = list(data.get("results") or [])
    total = int(data.get("count") or len(results))
    pages = max(1, math.ceil(total / page_size)) if total else 1
    for page in range(2, pages + 1):
        payload = _sw_get_json(session, _SW_CONS_URL, {
            "swindexcode": code, "page": str(page), "page_size": str(page_size),
        })
        results.extend((payload.get("data") or {}).get("results") or [])
    out: list[tuple[str, str]] = []
    for item in results:
        stock = str(item.get("stockcode") or "").strip()
        name = str(item.get("stockname") or "").strip()
        if stock:
            out.append((stock, name or stock))
    return out


def fetch_industry_classification() -> dict[str, dict[str, str]]:
    """
    行业分类。优先申万（官网指数发布），失败则证监会行业（baostock）。
    返回 {code: {sector_1, sector_2, stock_name}}
    """
    import time
    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        session = requests.Session()
        level1: list[tuple[str, str]] | None = None
        level2: list[tuple[str, str]] | None = None
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                level1 = _sw_index_list(session, "一级行业")
                level2 = _sw_index_list(session, "二级行业")
                if level1 and level2:
                    break
                level1, level2 = None, None
            except Exception as e:
                last_err = e
                print(f"  [SW] 行业列表第 {attempt + 1} 次失败: {type(e).__name__}", flush=True)
                time.sleep(2)
        if not level1 or not level2:
            raise RuntimeError(last_err or "申万行业列表为空")
        print(f"  [SW] 一级 {len(level1)} 个, 二级 {len(level2)} 个", flush=True)
        out: dict[str, dict[str, str]] = {}
        total = len(level2)
        failed = 0
        for i, (l2_code, l2_name) in enumerate(level2):
            l1_name = _sw_parent_name(l2_code, level1)
            cons: list[tuple[str, str]] | None = None
            for attempt in range(3):
                try:
                    cons = _sw_constituents(session, l2_code)
                    break
                except Exception as e:
                    cons = None
                    print(
                        f"  [SW] {l2_name} {l2_code} 第 {attempt + 1} 次失败: {type(e).__name__}",
                        flush=True,
                    )
                    time.sleep(1)
            if not cons:
                failed += 1
                print(f"  [SW] 跳过 {l2_name} {l2_code}", flush=True)
                continue
            for stock, name in cons:
                code = to_std_code(stock)
                if not code:
                    continue
                out[code] = {
                    "sector_1": l1_name,
                    "sector_2": l2_name,
                    "stock_name": name,
                }
            if (i + 1) % 10 == 0 or (i + 1) == total:
                print(f"  [SW] {i + 1}/{total} 二级行业, 累计股票 {len(out)}", flush=True)
        if failed:
            print(f"  [SW] 成分股失败 {failed} 个二级行业", flush=True)
        if out:
            return out
        print("  [SW] 结果为空，回退 baostock")
    except Exception as e:
        print(f"  [SW] 不可用 ({type(e).__name__}: {e})，回退 baostock")
    return _industry_from_baostock()


def ensure_shared_on_path() -> Path:
    """把 shared/ 与仓库根加入 sys.path。"""
    shared = Path(__file__).resolve().parent
    if str(shared) not in sys.path:
        sys.path.insert(0, str(shared))
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    return shared
