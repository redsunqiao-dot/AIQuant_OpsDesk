# -*- coding: utf-8 -*-
# 21-CASE-A: 申万行业元数据 (写入 trade_stock_status)
"""
IndustryMeta -- 申万行业元数据采集与查询

把"股票 -> 申万一级/二级行业"映射写入 trade_stock_status 表
本 CASE 主用 sector_2 (申万二级), 但同时落库 sector_1 方便后续做宏观分析
"""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_config import execute_query, execute_update, execute_many


# ============================================================
# xtdata 板块名归类
# ============================================================

# 申万 2014 旧版板块, 已合并/拆分, xtdata 可能仍返回, 需要排除
SW2014_EXCLUDE = frozenset([
    "SW1银行",       # 已拆分为国有大型银行II/股份制银行II/城商行II/农商行II
    "饮料制造",
    "电子制造",
    "家用轻工",
])


def is_sw_level_1(name: str) -> bool:
    """xtdata 板块名是否申万一级"""
    return (name.startswith("SW1")
            and not name.endswith("加权")
            and name not in SW2014_EXCLUDE)


def is_sw_level_2(name: str) -> bool:
    """xtdata 板块名是否申万二级
    兼容两种命名:
      1. 旧版 / 部分 xtdata: 以 "II" 结尾, 不带 SW 前缀, 例如 "白酒II"
      2. 新版 miniQMT/xtdata: 以 "SW2" 前缀开头, 例如 "SW2白酒"
    无论哪种, 带"加权"后缀的副本都要排除
    """
    if name.endswith("加权"):
        return False
    if name in SW2014_EXCLUDE:
        return False
    if name.startswith("SW2"):
        return True
    if name.endswith("II") and not name.startswith("SW"):
        return True
    return False


def display_name(sector_xt_name: str, level: int) -> str:
    """xtdata 板块名 -> 展示名 (去前后缀)
    一级: 去掉 "SW1" 前缀
    二级: 去掉 "SW2" 前缀, 或去掉 "II" 后缀
    """
    if level == 1 and sector_xt_name.startswith("SW1"):
        return sector_xt_name[3:]
    if level == 2:
        if sector_xt_name.startswith("SW2"):
            return sector_xt_name[3:]
        if sector_xt_name.endswith("II"):
            return sector_xt_name[:-2]
    return sector_xt_name


# ============================================================
# 从 xtdata 拉申万分类
# ============================================================

def fetch_sw_classification_from_xtdata() -> Dict[str, Dict[str, str]]:
    """兼容旧函数名：改为 akshare 申万二级分类。"""
    import sys
    from pathlib import Path as _Path
    _p = _Path(__file__).resolve()
    for _parent in _p.parents:
        if (_parent / "shared" / "public_market.py").exists():
            sys.path.insert(0, str(_parent / "shared"))
            break
    from public_market import fetch_industry_classification
    print("[META] 从申万官网拉取行业分类 ...")
    return fetch_industry_classification()



# ============================================================
# 落库
# ============================================================

def upsert_stock_status(classification: Dict[str, Dict[str, str]]) -> int:
    """把分类结果写入 trade_stock_status (UPSERT 模式, 不动其他字段)"""
    if not classification:
        return 0

    rows = []
    for code, info in classification.items():
        rows.append((
            code,
            info.get("stock_name"),
            info.get("sector_1"),
            info.get("sector_2"),
        ))

    sql = """
        INSERT INTO trade_stock_status
            (stock_code, stock_name, sector_1, sector_2)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            stock_name = VALUES(stock_name),
            sector_1   = COALESCE(VALUES(sector_1), sector_1),
            sector_2   = COALESCE(VALUES(sector_2), sector_2)
    """
    return execute_many(sql, rows)


def sync_sw_classification() -> int:
    """完整同步申万分类: 从公开源拉 + 写库"""
    print(f"\n{'='*70}")
    print(f"  同步申万分类到 trade_stock_status")
    print(f"{'='*70}\n")

    classification = fetch_sw_classification_from_xtdata()
    sectors_1 = {str(v.get("sector_1") or "") for v in classification.values()}
    sectors_2 = {str(v.get("sector_2") or "") for v in classification.values()}
    # 证监会门类是单个字母。不完整或退回证监会时不写库，避免覆盖已有申万分类。
    if (not classification
            or not sectors_1
            or all(len(name) <= 1 for name in sectors_1)
            or len(sectors_2) < 100
            or len(classification) < 3000):
        raise RuntimeError(
            f"申万分类不完整，拒绝写入: 股票 {len(classification)}, "
            f"一级 {len(sectors_1)}, 二级 {len(sectors_2)}"
        )
    n = upsert_stock_status(classification)
    print(f"\n[OK] 写入 trade_stock_status, 影响 {n} 行 ({len(classification)} 只股票)")
    return len(classification)


# ============================================================
# 查询接口 (CASE-B / CASE-D 用)
# ============================================================

def list_sectors_from_db(level: int = 2) -> List[str]:
    """
    列出当前 trade_stock_status 中所有出现过的板块名

    参数:
        level: 1=申万一级, 2=申万二级
    返回:
        按字典序排序的板块名列表
    """
    field = "sector_1" if level == 1 else "sector_2"
    rows = execute_query(
        f"SELECT DISTINCT {field} AS name FROM trade_stock_status WHERE {field} IS NOT NULL ORDER BY {field}")
    return [r["name"] for r in rows]


def get_sector_member_codes(sector_name: str, level: int = 2) -> List[str]:
    """取某板块当前的成分股代码列表"""
    field = "sector_1" if level == 1 else "sector_2"
    rows = execute_query(
        f"SELECT stock_code FROM trade_stock_status WHERE {field} = %s ORDER BY stock_code",
        (sector_name,))
    return [r["stock_code"] for r in rows]


def get_industry_map(level: int = 2) -> Dict[str, str]:
    """取股票 -> 行业名映射 (CASE-C 多因子选股做行业中性化用)"""
    field = "sector_1" if level == 1 else "sector_2"
    rows = execute_query(
        f"SELECT stock_code, {field} AS name FROM trade_stock_status WHERE {field} IS NOT NULL")
    return {r["stock_code"]: r["name"] for r in rows}


def list_all_stocks_with_sector(level: int = 2) -> List[Dict]:
    """列出所有有该级别板块归类的股票 (含 stock_name)"""
    field = "sector_1" if level == 1 else "sector_2"
    rows = execute_query(
        f"SELECT stock_code, stock_name, {field} AS sector_name "
        f"FROM trade_stock_status WHERE {field} IS NOT NULL ORDER BY stock_code")
    return rows


# ============================================================
# CLI
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="同步申万分类到 trade_stock_status")
    parser.add_argument("--show", action="store_true",
                        help="只展示当前数据库已有的板块")
    parser.add_argument("--level", type=int, choices=[1, 2], default=2,
                        help="--show 模式下展示哪一级板块 (默认二级)")
    args = parser.parse_args()

    if args.show:
        sectors = list_sectors_from_db(level=args.level)
        print(f"\n申万 {'一' if args.level == 1 else '二'} 级板块共 {len(sectors)} 个:")
        for i, s in enumerate(sectors, 1):
            members = get_sector_member_codes(s, level=args.level)
            print(f"  [{i:>3}] {s:<20s} 成分股 {len(members)} 只")
        return

    sync_sw_classification()


if __name__ == "__main__":
    main()
