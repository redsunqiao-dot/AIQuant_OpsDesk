# -*- coding: utf-8 -*-
# 50ETF QVIX 拉取 -- 供 Kris 宏观门控自动灌入
"""复用课内 ak.index_option_50etf_qvix(), 返回最新收盘与日期."""

from __future__ import annotations

from typing import Any, Dict

import akshare as ak
import pandas as pd


def fetch_latest_qvix() -> Dict[str, Any]:
    """拉取 A 股 50ETF QVIX, 返回 {vix, date, count}.

    缺数据或接口失败时直接抛错, 由调用方展示原因.
    """
    df = ak.index_option_50etf_qvix()
    if df is None or len(df) == 0:
        raise RuntimeError("ak.index_option_50etf_qvix() 返回空表")
    work = df.copy()
    work["date"] = pd.to_datetime(work["date"])
    work = work.sort_values("date").reset_index(drop=True)
    last = work.iloc[-1]
    vix = float(last["close"])
    if vix != vix:  # NaN
        raise RuntimeError("最新 QVIX close 为 NaN")
    return {
        "vix": vix,
        "date": last["date"].strftime("%Y-%m-%d"),
        "count": int(len(work)),
    }
