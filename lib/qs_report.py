# -*- coding: utf-8 -*-
# QuantStats HTML 报告胶水 -- 复用 vendor/quantstats/report_engine
"""nav 列表 -> 日收益 -> qs HTML 落盘."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from lib.ext_bootstrap import ensure_on_path, resolve_vendor_or_course
from lib.paths import OUTPUTS_DIR


def _ensure_qs_path() -> Path:
    qs_dir = resolve_vendor_or_course(
        ("quantstats",),
        ("08_QuantStats绩效与RAG",),
        "report_engine.py",
    )
    if qs_dir is None:
        raise FileNotFoundError("找不到 report_engine.py（vendor/quantstats 或课程 08_）")
    ensure_on_path(qs_dir)
    return qs_dir


def navs_to_series(navs: List[dict]) -> pd.Series:
    """[{date, nav}, ...] -> 净值 Series (DatetimeIndex)."""
    if not navs:
        raise RuntimeError("navs 为空, 无法生成 QuantStats 报告")
    rows = []
    for x in navs:
        d = x.get("date") or x.get("ts") or x.get("time")
        n = x.get("nav") if "nav" in x else x.get("total_asset")
        if d is None or n is None:
            continue
        rows.append((pd.to_datetime(str(d)[:10]), float(n)))
    if len(rows) < 3:
        raise RuntimeError(f"有效净值点不足 3 个 (现 {len(rows)})")
    s = pd.Series({d: n for d, n in rows}).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s


def generate_qs_html_from_navs(
    navs: List[dict],
    title: str = "策略绩效报告",
    out_name: Optional[str] = None,
) -> Dict[str, Any]:
    """生成中文优先的 QuantStats HTML, 返回 {path, title, points}."""
    _ensure_qs_path()
    from report_engine import generate_chinese_report, generate_html_report, nav_to_returns

    nav_s = navs_to_series(navs)
    rets = nav_to_returns(nav_s)
    out_dir = OUTPUTS_DIR / "backtest_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = out_name or f"qs_report_{ts}.html"
    out_path = out_dir / fname

    try:
        generate_chinese_report(rets, title=title, output_path=str(out_path))
    except Exception:
        generate_html_report(rets, title=title, output_path=str(out_path))

    if not out_path.exists():
        raise RuntimeError(f"报告未生成: {out_path}")
    return {
        "path": str(out_path),
        "title": title,
        "points": int(len(nav_s)),
        "url": f"/outputs/backtest_reports/{fname}",
    }
