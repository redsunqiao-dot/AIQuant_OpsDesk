# -*- coding: utf-8 -*-
"""外置课程模块路径引导：优先本树 vendor/，兼容 AI_quant 课程仓布局。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, Optional

from lib.paths import PROJECT_ROOT, VENDOR_DIR


def _insert(p: Path) -> None:
    sp = str(p)
    if p.exists() and sp not in sys.path:
        sys.path.insert(0, sp)


def course_root() -> Optional[Path]:
    """若仍在 AI_quant 课程仓内，返回仓库根。"""
    for p in PROJECT_ROOT.parents:
        if (p / "02_行情数据采集").is_dir() or (p / "shared" / "public_market.py").exists():
            return p
    return None


def ensure_public_market() -> Path:
    """把公开行情模块加入 sys.path，返回 shared 目录。

    优先: vendor/shared + vendor/market
    其次: 课程仓 shared/ + 02_行情数据采集/
    """
    market = VENDOR_DIR / "market"
    shared = VENDOR_DIR / "shared"
    if (shared / "public_market.py").exists():
        _insert(market)
        _insert(shared)
        return shared
    root = course_root()
    if root is not None:
        _insert(root / "02_行情数据采集")
        sh = root / "shared"
        _insert(sh)
        return sh
    # 最后尝试本树根下 shared（兼容）
    local = PROJECT_ROOT / "shared"
    _insert(PROJECT_ROOT / "vendor" / "market")
    _insert(local)
    return local


def resolve_vendor_or_course(
    vendor_parts: Iterable[str],
    course_parts: Iterable[str],
    marker: str,
) -> Optional[Path]:
    """解析目录：先 vendor/<parts>，再 course_root/<parts>，要求 marker 文件存在。"""
    v = VENDOR_DIR.joinpath(*vendor_parts)
    if (v / marker).exists():
        return v
    root = course_root()
    if root is not None:
        c = root.joinpath(*course_parts)
        if (c / marker).exists():
            return c
    return None


def ensure_on_path(dir_path: Optional[Path]) -> Optional[Path]:
    if dir_path is None:
        return None
    _insert(dir_path)
    return dir_path
