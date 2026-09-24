# -*- coding: utf-8 -*-
# 多因子选股运行器（晨会内嵌）
from __future__ import annotations
import math
from datetime import date
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .db_config import execute_query, get_connection


def load_kline_from_db(stock_code: str, lookback_days: int = 200, conn=None) -> pd.DataFrame:
    """从 trade_stock_daily 加载单股最近 N 个交易日的 K 线"""
    sql = """
        SELECT trade_date, open_price, high_price, low_price, close_price,
               volume, amount
        FROM trade_stock_daily
        WHERE stock_code = %s
        ORDER BY trade_date DESC
        LIMIT %s
    """
    rows = execute_query(sql, (stock_code, lookback_days), conn=conn)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows[::-1])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df.set_index("trade_date", inplace=True)
    df.rename(columns={
        "open_price": "open", "high_price": "high",
        "low_price":  "low",  "close_price": "close",
    }, inplace=True)
    for col in ["open", "high", "low", "close", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    return df


def load_klines_batch(stock_codes: List[str], lookback_days: int = 120,
                      conn=None) -> Dict[str, pd.DataFrame]:
    """批量加载多股最近 N 个交易日 K 线（共享连接，一次取窗口内全部分组）。"""
    if not stock_codes:
        return {}

    own = conn is None
    if own:
        conn = get_connection()
    try:
        # 取全市场最近 lookback_days 个交易日作为统一窗口
        date_rows = execute_query(
            "SELECT DISTINCT trade_date FROM trade_stock_daily "
            "ORDER BY trade_date DESC LIMIT %s",
            (lookback_days,),
            conn=conn,
        )
        if not date_rows:
            return {}
        min_date = min(r["trade_date"] for r in date_rows)

        placeholders = ",".join(["%s"] * len(stock_codes))
        rows = execute_query(
            f"""
            SELECT stock_code, trade_date, open_price, high_price, low_price,
                   close_price, volume, amount
            FROM trade_stock_daily
            WHERE stock_code IN ({placeholders}) AND trade_date >= %s
            ORDER BY stock_code ASC, trade_date ASC
            """,
            list(stock_codes) + [min_date],
            conn=conn,
        )

        by_code: Dict[str, list] = {}
        for r in rows:
            by_code.setdefault(r["stock_code"], []).append(r)

        out: Dict[str, pd.DataFrame] = {}
        for code, code_rows in by_code.items():
            df = pd.DataFrame(code_rows)
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            df.set_index("trade_date", inplace=True)
            df.rename(columns={
                "open_price": "open", "high_price": "high",
                "low_price": "low", "close_price": "close",
            }, inplace=True)
            for col in ["open", "high", "low", "close", "amount"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
            # 去掉辅助列
            if "stock_code" in df.columns:
                df = df.drop(columns=["stock_code"])
            out[code] = df.tail(lookback_days)
        return out
    finally:
        if own:
            conn.close()



def _safe_pct_change(prices: pd.Series, periods: int) -> float:
    if len(prices) <= periods:
        return np.nan
    p_now  = prices.iloc[-1]
    p_then = prices.iloc[-1 - periods]
    if p_then <= 0:
        return np.nan
    return p_now / p_then - 1.0


def calc_factors_for_one(df: pd.DataFrame, total_share: float = 0) -> Dict[str, float]:
    if df is None or len(df) < 130:
        return {}

    close  = df["close"].astype(float)
    volume = df["volume"].astype(float) if "volume" in df.columns else None
    amount = df["amount"].astype(float) if "amount" in df.columns else None

    returns = close.pct_change().dropna()
    if len(returns) < 100:
        return {}

    f = {}
    f["MOM_1M"] = _safe_pct_change(close, 21)
    f["MOM_3M"] = _safe_pct_change(close, 63)
    f["MOM_6M"] = _safe_pct_change(close, 126)

    rev_5d = _safe_pct_change(close, 5)
    f["REV_5D"] = -rev_5d if not np.isnan(rev_5d) else np.nan

    vol_20 = returns.tail(20).std() * math.sqrt(250)
    vol_60 = returns.tail(60).std() * math.sqrt(250)
    f["VOL_20"] = -vol_20 if not np.isnan(vol_20) else np.nan
    f["VOL_60"] = -vol_60 if not np.isnan(vol_60) else np.nan

    if amount is not None and len(amount) >= 20:
        liq_20 = amount.tail(20).mean()
        f["LIQ_20"] = -math.log(max(liq_20, 1.0))
    else:
        f["LIQ_20"] = np.nan

    if volume is not None and len(volume) >= 20:
        if total_share > 0:
            turn_20 = (volume.tail(20).mean() / total_share) * 100
        else:
            long_vol = volume.tail(60).mean()
            turn_20 = volume.tail(20).mean() / long_vol if long_vol > 0 else np.nan
        f["TURN_20"] = -turn_20 if not np.isnan(turn_20) else np.nan
    else:
        f["TURN_20"] = np.nan

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi_val = rsi.iloc[-1] if len(rsi) > 0 else np.nan
    f["RSI_14"] = (rsi_val - 50) if not np.isnan(rsi_val) else np.nan

    ma20 = close.rolling(20).mean().iloc[-1]
    bias_20 = (close.iloc[-1] - ma20) / ma20 if ma20 > 0 else np.nan
    f["BIAS_20"] = -bias_20 if not np.isnan(bias_20) else np.nan

    return f


def calc_factors_batch(stock_codes: List[str], lookback_days: int = 200) -> pd.DataFrame:
    klines = load_klines_batch(stock_codes, lookback_days=lookback_days)
    rows = {}
    for code in stock_codes:
        df = klines.get(code)
        if df is None or df.empty:
            continue
        f = calc_factors_for_one(df)
        if f:
            rows[code] = f

    df_result = pd.DataFrame.from_dict(rows, orient="index")
    print(f"  [FACTOR] {len(df_result)} 只有效, "
          f"{len(stock_codes) - len(df_result)} 只数据不足被剔除")
    return df_result


def winsorize_mad(series: pd.Series, n: float = 3.0) -> pd.Series:
    s = series.copy()
    median = s.median()
    mad = (s - median).abs().median()
    if mad == 0 or np.isnan(mad):
        return s
    upper = median + n * 1.4826 * mad
    lower = median - n * 1.4826 * mad
    return s.clip(lower=lower, upper=upper)


def zscore(series: pd.Series) -> pd.Series:
    s = series.copy()
    mean = s.mean()
    std  = s.std(ddof=1)
    if std == 0 or np.isnan(std):
        return s * 0.0
    return (s - mean) / std


def industry_neutralize(factor_series: pd.Series, industry_map: dict) -> pd.Series:
    df = pd.DataFrame({
        "factor":   factor_series,
        "industry": pd.Series(industry_map),
    })
    df = df.dropna(subset=["industry"])
    return df.groupby("industry")["factor"].transform(zscore)


def preprocess_factors(factor_df: pd.DataFrame,
                        industry_map: Optional[dict] = None,
                        winsorize_n: float = 3.0,
                        neutralize: bool = True) -> pd.DataFrame:
    result = pd.DataFrame(index=factor_df.index)
    for col in factor_df.columns:
        s = factor_df[col].dropna()
        if len(s) == 0:
            result[col] = factor_df[col]
            continue
        s_w = winsorize_mad(s, n=winsorize_n)
        s_z = zscore(s_w)
        if neutralize and industry_map:
            s_z = industry_neutralize(s_z, industry_map)
            s_z = zscore(s_z)
        result[col] = s_z
    return result


MIN_AVG_AMOUNT_20 = 50_000_000  # 近20日日均成交额下限，单位元


def rank_momentum_stocks(stock_codes: List[str], industry_map: dict) -> pd.DataFrame:
    """在强势板块成分里挑顺势票。

    硬过滤: 近20日日均成交额不低于 5000 万，且最新收盘站上 MA20。
    打分: 全池里 MOM_1M、MOM_3M 各自标准化后相加。
    不把 5 日反转、乖离、低流动性再加进同一个分数。
    """
    rows = []
    short = 0
    illiquid = 0
    below_ma = 0
    # 批量一次取数，避免每股开一次 MySQL 连接触发 WinError 10048
    klines = load_klines_batch(stock_codes, lookback_days=120)
    for code in stock_codes:
        df = klines.get(code)
        if df is None or len(df) < 70:
            short += 1
            continue
        close = df["close"].astype(float)
        amount = df["amount"].astype(float) if "amount" in df.columns else None
        if amount is None or len(amount) < 20:
            illiquid += 1
            continue
        avg_amt = float(amount.tail(20).mean())
        if not math.isfinite(avg_amt) or avg_amt < MIN_AVG_AMOUNT_20:
            illiquid += 1
            continue
        ma20 = float(close.rolling(20).mean().iloc[-1])
        last = float(close.iloc[-1])
        if not math.isfinite(ma20) or ma20 <= 0 or last <= ma20:
            below_ma += 1
            continue
        mom_1m = _safe_pct_change(close, 21)
        mom_3m = _safe_pct_change(close, 63)
        if np.isnan(mom_1m) or np.isnan(mom_3m):
            short += 1
            continue
        rows.append({
            "code": code,
            "industry": industry_map.get(code, "未分类"),
            "MOM_1M": float(mom_1m),
            "MOM_3M": float(mom_3m),
            "avg_amount": avg_amt,
            "close": last,
        })

    print(
        f"  [MOM] 入池 {len(stock_codes)} 只, 通过 {len(rows)} 只; "
        f"K线不足 {short}, 成交额不足 {illiquid}, 未站上MA20 {below_ma}"
    )
    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame(rows).set_index("code")
    out["score"] = zscore(out["MOM_1M"]) + zscore(out["MOM_3M"])
    return out.sort_values("score", ascending=False)


# ============================================================
# 截面多因子选股 (CASE-C 口径): 10 因子 -> 去极值/标准化/行业中性 -> IC 加权
# ============================================================

# 参与截面合成的因子 (与 calc_factors_for_one 的输出一致)
# 注意: VOL/LIQ/TURN/REV/RSI/BIAS 在 calc_factors_for_one 里已按"越大越好"取过负号
FACTOR_COLS = [
    "MOM_1M", "MOM_3M", "MOM_6M", "REV_5D",
    "VOL_20", "VOL_60", "LIQ_20", "TURN_20",
    "RSI_14", "BIAS_20",
]


def calc_ic(factor_series: pd.Series, future_return: pd.Series,
            method: str = "spearman") -> float:
    """单期 IC: 因子截面与未来收益的秩相关 (CASE-C synthesizer 同口径)"""
    df = pd.DataFrame({"f": factor_series, "r": future_return}).dropna()
    if len(df) < 8:
        return np.nan
    return df["f"].corr(df["r"], method=method)


def ic_weighted_synthesis(factor_df: pd.DataFrame, ic_dict: dict) -> pd.Series:
    """IC 加权合成 alpha: 权重 = IC / sum(|IC|); IC 全为 0 时退化为等权"""
    weights = pd.Series(ic_dict).reindex(factor_df.columns).fillna(0.0)
    if weights.abs().sum() == 0:
        return factor_df.mean(axis=1)
    weights_norm = weights / weights.abs().sum()
    return (factor_df * weights_norm).sum(axis=1)


def _factor_matrix_at(klines: Dict[str, pd.DataFrame], codes: List[str],
                      end_offset: int = 0) -> pd.DataFrame:
    """在"倒数第 end_offset 个交易日"这个截面上算因子矩阵 (0 = 最新截面)"""
    rows = {}
    for code in codes:
        df = klines.get(code)
        if df is None or df.empty:
            continue
        sub = df.iloc[: len(df) - end_offset] if end_offset > 0 else df
        f = calc_factors_for_one(sub)
        if f:
            rows[code] = f
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame.from_dict(rows, orient="index")


def _forward_returns(klines: Dict[str, pd.DataFrame], codes: List[str],
                     end_offset: int, horizon: int) -> pd.Series:
    """该截面往后 horizon 个交易日的收益 (给 IC 用)"""
    out = {}
    for code in codes:
        df = klines.get(code)
        if df is None or df.empty:
            continue
        n = len(df)
        t = n - end_offset - 1          # 截面当日在 df 里的位置
        fwd = t + horizon
        if t < 0 or fwd >= n:
            continue
        c0 = float(df["close"].iloc[t])
        c1 = float(df["close"].iloc[fwd])
        if c0 > 0:
            out[code] = c1 / c0 - 1.0
    return pd.Series(out, dtype=float)


def estimate_factor_ic(klines: Dict[str, pd.DataFrame], codes: List[str],
                       industry_map: Optional[dict] = None,
                       periods: int = 6, horizon: int = 21) -> Dict[str, float]:
    """滚动若干个历史截面，算每个因子的平均 IC，作为合成权重来源"""
    ic_records: Dict[str, List[float]] = {c: [] for c in FACTOR_COLS}
    used = 0
    for k in range(1, periods + 1):
        end_offset = k * horizon
        f_hist = _factor_matrix_at(klines, codes, end_offset=end_offset)
        if f_hist.empty or len(f_hist) < 8:
            continue
        f_hist = preprocess_factors(f_hist, industry_map=industry_map, neutralize=True)
        fut = _forward_returns(klines, list(f_hist.index), end_offset, horizon)
        if fut.empty:
            continue
        used += 1
        for col in FACTOR_COLS:
            if col not in f_hist.columns:
                continue
            ic = calc_ic(f_hist[col], fut)
            if not np.isnan(ic):
                ic_records[col].append(float(ic))

    ic_dict = {}
    for col, vals in ic_records.items():
        ic_dict[col] = float(np.mean(vals)) if vals else 0.0
    print(f"  [IC] 有效截面 {used}/{periods} 期, 平均 IC: "
          + ", ".join(f"{k}={v:+.3f}" for k, v in ic_dict.items() if abs(v) > 1e-9))
    return ic_dict


def rank_multifactor_stocks(stock_codes: List[str], industry_map: dict,
                            lookback_days: int = 260,
                            ic_periods: int = 6,
                            ic_horizon: int = 21) -> pd.DataFrame:
    """截面多因子选股 (CASE-C 完整口径)。

    流程: 批量取日K -> 流动性硬过滤 -> 10 因子矩阵 -> 去极值 + Z-score + 行业中性
          -> 历史截面 IC -> IC 加权合成 alpha -> 按 alpha 排序

    与 rank_momentum_stocks 的区别: 不再只看 MOM + 站上 MA20,
    而是整池做截面比较, 波动/流动性/反转等因子按历史 IC 自动定权。
    """
    if not stock_codes:
        return pd.DataFrame()

    klines = load_klines_batch(stock_codes, lookback_days=lookback_days)

    # 硬过滤: 数据长度 + 近 20 日日均成交额
    codes: List[str] = []
    short = 0
    illiquid = 0
    meta_rows = {}
    for code in stock_codes:
        df = klines.get(code)
        if df is None or len(df) < 130:
            short += 1
            continue
        amount = df["amount"].astype(float) if "amount" in df.columns else None
        if amount is None or len(amount) < 20:
            illiquid += 1
            continue
        avg_amt = float(amount.tail(20).mean())
        if not math.isfinite(avg_amt) or avg_amt < MIN_AVG_AMOUNT_20:
            illiquid += 1
            continue
        codes.append(code)
        meta_rows[code] = {
            "avg_amount": avg_amt,
            "close": float(df["close"].astype(float).iloc[-1]),
        }

    print(f"  [MF] 入池 {len(stock_codes)} 只, 通过硬过滤 {len(codes)} 只; "
          f"K线不足 {short}, 成交额不足 {illiquid}")
    if len(codes) < 5:
        print("  [MF] 截面样本过少, 无法做多因子比较")
        return pd.DataFrame()

    raw = _factor_matrix_at(klines, codes, end_offset=0)
    if raw.empty:
        return pd.DataFrame()
    raw = raw.reindex(columns=[c for c in FACTOR_COLS if c in raw.columns])

    ind_map_used = {c: industry_map.get(c, "未分类") for c in raw.index}
    processed = preprocess_factors(raw, industry_map=ind_map_used, neutralize=True)

    ic_dict = estimate_factor_ic(klines, codes, industry_map=ind_map_used,
                                 periods=ic_periods, horizon=ic_horizon)
    alpha = ic_weighted_synthesis(processed, ic_dict)

    out = pd.DataFrame(index=raw.index)
    out["industry"] = [ind_map_used.get(c, "未分类") for c in raw.index]
    for col in raw.columns:
        out[col] = raw[col]
    out["avg_amount"] = [meta_rows[c]["avg_amount"] for c in raw.index]
    out["close"] = [meta_rows[c]["close"] for c in raw.index]
    out["score"] = alpha
    out = out.dropna(subset=["score"]).sort_values("score", ascending=False)
    return out


def filter_tradable(stock_codes: List[str], min_listed_days: int = 250) -> List[str]:
    if not stock_codes:
        return []

    placeholders = ",".join(["%s"] * len(stock_codes))
    rows = execute_query(
        f"SELECT stock_code, stock_name, list_date FROM trade_stock_status "
        f"WHERE stock_code IN ({placeholders})",
        stock_codes)
    info = {r["stock_code"]: r for r in rows}
    today = date.today()

    keep = []
    for code in stock_codes:
        meta = info.get(code)
        if not meta:
            continue
        name = (meta.get("stock_name") or "")
        if "ST" in name.upper() or "退" in name:
            continue
        listed = meta.get("list_date")
        if listed:
            try:
                ld = listed if isinstance(listed, date) else date.fromisoformat(str(listed))
                if (today - ld).days < min_listed_days:
                    continue
            except Exception:
                pass
        keep.append(code)

    return keep
