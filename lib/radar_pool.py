# -*- coding: utf-8 -*-
# 精选池读取 -- 供晨会章节与 adopt_radar
"""从 vendor/radar（或课程 04_TALib/outputs）读取最新偏向一买精选池 CSV."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from lib.ext_bootstrap import course_root
from lib.paths import VENDOR_RADAR_DIR


def radar_outputs_dir() -> Path:
    """精选池目录：优先 vendor/radar，其次课程 04_TALib指标形态/outputs。"""
    if VENDOR_RADAR_DIR.is_dir() and any(VENDOR_RADAR_DIR.glob("精选池_*.csv")):
        return VENDOR_RADAR_DIR
    root = course_root()
    if root is not None:
        course_out = root / "04_TALib指标形态" / "outputs"
        if course_out.is_dir():
            return course_out
    if VENDOR_RADAR_DIR.is_dir():
        return VENDOR_RADAR_DIR
    raise FileNotFoundError(f"精选池目录不存在: {VENDOR_RADAR_DIR}")


def find_latest_elite_csv(prefer_buy1: bool = True) -> Path:
    """找最新精选池 CSV: 优先 *_偏向一买.csv, 否则 精选池_YYYY-MM-DD.csv."""
    out = radar_outputs_dir()
    if not out.is_dir():
        raise FileNotFoundError(f"精选池目录不存在: {out}")

    buy1 = sorted(out.glob("精选池_*_偏向一买.csv"))
    if prefer_buy1 and buy1:
        return buy1[-1]

    plain = []
    for p in out.glob("精选池_*.csv"):
        name = p.name
        if "_偏向" in name or "_入选理由" in name:
            continue
        plain.append(p)
    if plain:
        return sorted(plain)[-1]
    if buy1:
        return buy1[-1]
    raise FileNotFoundError(f"未找到精选池 CSV: {out}")


def load_elite_pool(csv_path: Optional[Path] = None, top_n: int = 5) -> Dict[str, Any]:
    """加载精选池, 返回 {path, scan_date, items:[{code, score, ...}], codes}."""
    path = Path(csv_path) if csv_path else find_latest_elite_csv(prefer_buy1=True)
    df = pd.read_csv(path, encoding="utf-8-sig")
    if df is None or len(df) == 0:
        raise RuntimeError(f"精选池为空: {path}")

    code_col = "代码" if "代码" in df.columns else None
    if code_col is None:
        raise RuntimeError(f"精选池缺少「代码」列: {path}")

    m = re.search(r"精选池_(\d{4}-\d{2}-\d{2})", path.name)
    scan_date = m.group(1) if m else ""

    score_col = "综合分" if "综合分" in df.columns else None
    work = df
    if score_col:
        work = df.sort_values(score_col, ascending=False)

    items: List[dict] = []
    for _, row in work.head(int(top_n)).iterrows():
        code = str(row[code_col]).strip()
        items.append({
            "code": code,
            "score": float(row[score_col]) if score_col and pd.notna(row.get(score_col)) else None,
            "signal": str(row.get("信号等级") or ""),
            "chan_tag": str(row.get("缠论标签") or ""),
            "ml_tag": str(row.get("ML标签") or ""),
            "close": float(row["收盘价"]) if "收盘价" in df.columns and pd.notna(row.get("收盘价")) else None,
            "chg_pct": float(row["涨跌幅%"]) if "涨跌幅%" in df.columns and pd.notna(row.get("涨跌幅%")) else None,
        })
    return {
        "path": str(path),
        "scan_date": scan_date,
        "items": items,
        "codes": [x["code"] for x in items],
    }
