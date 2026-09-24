#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K 线数据获取（公开源 / MySQL，已替代 xtquant）
输出 JSON，供 Agent Skills 调用。仅支持日线 period=1d。
"""
import json
import sys
from pathlib import Path

# 定位仓库 shared/
_p = Path(__file__).resolve()
for parent in _p.parents:
    if (parent / "shared" / "public_market.py").exists():
        sys.path.insert(0, str(parent / "shared"))
        sys.path.insert(0, str(parent))
        break

from public_market import load_daily_bars_records, to_std_code  # noqa: E402


def get_kline_data(stock_code, period="1d", start_date="", end_date="", count=100):
    if period not in ("1d", "d", "day", "daily"):
        return {"error": f"公开源当前仅支持日线，收到 period={period}"}
    return load_daily_bars_records(
        to_std_code(stock_code),
        start_date=start_date,
        end_date=end_date,
        count=int(count) if count else 100,
    )


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "请提供股票代码"}, ensure_ascii=False))
        sys.exit(1)
    stock_code = sys.argv[1]
    period = sys.argv[2] if len(sys.argv) > 2 else "1d"
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    result = get_kline_data(stock_code, period=period, count=count)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
