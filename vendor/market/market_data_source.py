# -*- coding: utf-8 -*-
"""
行情数据源（替代 MiniQMT）

主源: akshare（东财封装，前复权日线）
备源: baostock（证券宝，前复权日线）

统一输出 DataFrame 索引为日期，列:
  open, high, low, close, volume(股), amount(元), turnover_rate(%)
股票代码统一为交易所后缀格式: 600519.SH / 000001.SZ
"""
from __future__ import annotations

import time
from datetime import datetime

import pandas as pd

# 主源失败后再试备源（baostock 相对稳；akshare 偶发断连）
PRIMARY = "baostock"
FALLBACK = "akshare"
# 单票请求间隔，降低封禁风险（秒）
REQUEST_SLEEP = 0.05

# baostock 登录会话（全市场采集时复用，避免每票 login/logout）
_BS_LOGGED_IN = False


def to_std_code(code: str) -> str:
    """任意常见写法 -> 600519.SH / 000001.SZ。"""
    c = str(code).strip().upper().replace(" ", "")
    if "." in c:
        left, right = c.split(".", 1)
        # baostock 写法: SH.600519 / SZ.000001
        if left in ("SH", "SZ", "SS", "XSHG", "XSHE") and right.isdigit():
            mkt = "SH" if left in ("SH", "SS", "XSHG") else "SZ"
            return f"{right}.{mkt}"
        # 标准写法: 600519.SH
        num, mkt = left, right
        mkt = "SH" if mkt in ("SH", "SS", "XSHG") else ("SZ" if mkt in ("SZ", "XSHE") else mkt)
        return f"{num}.{mkt}"
    if c.startswith(("SH", "SZ")) and len(c) >= 8:
        return f"{c[2:]}.{c[:2]}"
    if c.startswith(("6", "9")):  # 沪市主板/科创等
        return f"{c}.SH"
    if c.startswith(("0", "1", "2", "3")):
        return f"{c}.SZ"
    return c


def to_pure_code(code: str) -> str:
    """600519.SH -> 600519。"""
    return to_std_code(code).split(".", 1)[0]


def to_baostock_code(code: str) -> str:
    """600519.SH -> sh.600519。"""
    std = to_std_code(code)
    num, mkt = std.split(".", 1)
    return f"{mkt.lower()}.{num}"


def is_hs_a_stock(code: str) -> bool:
    """是否沪深 A 股（排除北交所等）。"""
    std = to_std_code(code)
    num, mkt = std.split(".", 1)
    if mkt == "SH":
        return num.startswith(("60", "68"))
    if mkt == "SZ":
        return num.startswith(("00", "30"))
    return False


def get_stock_list() -> list[str]:
    """沪深 A 股代码列表（带后缀）。"""
    import akshare as ak

    raw = ak.stock_info_a_code_name()
    codes: list[str] = []
    for code in raw["code"].astype(str).tolist():
        std = to_std_code(code)
        if is_hs_a_stock(std):
            codes.append(std)
    # 去重保序
    seen = set()
    out = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _normalize_ohlcv(df: pd.DataFrame, volume_unit: str) -> pd.DataFrame:
    """
    统一列名与成交量单位。
    volume_unit: 'lot'=手, 'share'=股
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "amount", "turnover_rate"])

    work = df.copy()
    rename = {}
    cols = {c.lower(): c for c in work.columns}
    mapping = {
        "open": ["open", "开盘"],
        "high": ["high", "最高"],
        "low": ["low", "最低"],
        "close": ["close", "收盘"],
        "volume": ["volume", "成交量", "vol"],
        "amount": ["amount", "成交额"],
        "turnover_rate": ["turnover_rate", "换手率", "turn"],
        "date": ["date", "日期", "day"],
    }
    for std, cands in mapping.items():
        for name in cands:
            if name in work.columns:
                rename[name] = std
                break
            if name.lower() in cols:
                rename[cols[name.lower()]] = std
                break
    work = work.rename(columns=rename)

    if "date" in work.columns:
        work["date"] = pd.to_datetime(work["date"])
        work = work.set_index("date")
    elif not isinstance(work.index, pd.DatetimeIndex):
        work.index = pd.to_datetime(work.index)

    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col not in work.columns:
            raise ValueError(f"缺少列: {col}")
        work[col] = pd.to_numeric(work[col], errors="coerce")

    if "turnover_rate" in work.columns:
        work["turnover_rate"] = pd.to_numeric(work["turnover_rate"], errors="coerce")
    else:
        work["turnover_rate"] = None

    if volume_unit == "lot":
        work["volume"] = (work["volume"] * 100).round().astype("int64")
    else:
        work["volume"] = work["volume"].round().astype("int64")

    work = work.sort_index()
    work = work[~work.index.duplicated(keep="last")]
    return work[["open", "high", "low", "close", "volume", "amount", "turnover_rate"]]


def _fetch_akshare(code: str, start_ymd: str, end_ymd: str) -> pd.DataFrame:
    import akshare as ak

    pure = to_pure_code(code)
    df = ak.stock_zh_a_hist(
        symbol=pure,
        period="daily",
        start_date=start_ymd,
        end_date=end_ymd,
        adjust="qfq",
    )
    return _normalize_ohlcv(df, volume_unit="lot")


def baostock_is_logged_in() -> bool:
    """当前进程是否已登录 baostock。"""
    return _BS_LOGGED_IN


def baostock_login() -> None:
    """全市场采集开始时调用一次。"""
    global _BS_LOGGED_IN
    import baostock as bs

    if _BS_LOGGED_IN:
        return
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
    _BS_LOGGED_IN = True


def baostock_logout() -> None:
    """全市场采集结束时调用一次。"""
    global _BS_LOGGED_IN
    import baostock as bs

    if not _BS_LOGGED_IN:
        return
    try:
        bs.logout()
    finally:
        _BS_LOGGED_IN = False


def _fetch_baostock(code: str, start_ymd: str, end_ymd: str) -> pd.DataFrame:
    import baostock as bs

    start = f"{start_ymd[:4]}-{start_ymd[4:6]}-{start_ymd[6:8]}"
    end = f"{end_ymd[:4]}-{end_ymd[4:6]}-{end_ymd[6:8]}"
    bs_code = to_baostock_code(code)

    own_session = False
    if not _BS_LOGGED_IN:
        baostock_login()
        own_session = True
    try:
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume,amount,turn",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="2",  # 前复权
        )
        if rs.error_code != "0":
            raise RuntimeError(f"baostock 查询失败: {rs.error_msg}")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return _normalize_ohlcv(pd.DataFrame(), volume_unit="share")
        df = pd.DataFrame(
            rows,
            columns=["date", "open", "high", "low", "close", "volume", "amount", "turn"],
        )
        return _normalize_ohlcv(df, volume_unit="share")
    finally:
        if own_session:
            baostock_logout()


def fetch_daily(
    code: str,
    start_ymd: str,
    end_ymd: str,
    source: str | None = None,
) -> pd.DataFrame:
    """
    拉取前复权日线。
    start_ymd/end_ymd: YYYYMMDD
    """
    src = (source or PRIMARY).lower()
    last_err: Exception | None = None
    order = [src]
    if FALLBACK and FALLBACK not in order:
        order.append(FALLBACK)

    for name in order:
        try:
            if name == "akshare":
                df = _fetch_akshare(code, start_ymd, end_ymd)
            elif name == "baostock":
                df = _fetch_baostock(code, start_ymd, end_ymd)
            else:
                raise ValueError(f"未知数据源: {name}")
            time.sleep(REQUEST_SLEEP)
            return df
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"{code} 取数失败: {last_err}")


def connect_banner() -> str:
    """启动时打印用。"""
    return (
        f"数据源: 主={PRIMARY}, 备={FALLBACK} "
        f"(无需 MiniQMT; 请求间隔 {REQUEST_SLEEP}s)"
    )


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    print(connect_banner())
    codes = get_stock_list()
    print(f"沪深A股数量: {len(codes)}")
    print("样例:", codes[:5])
    today = datetime.now().strftime("%Y%m%d")
    df = fetch_daily("600519.SH", "20260910", today)
    print(df.tail())
