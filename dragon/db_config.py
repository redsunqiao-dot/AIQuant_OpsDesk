# -*- coding: utf-8 -*-
"""
数据库配置：优先作战台根 .env / vendor/db_env/.env 的 QUANT_TRADE_*；
兼容课程仓 02_行情数据采集/.env 与旧键 WUCAI_SQL_*。
"""
from pathlib import Path
import sys
import pymysql

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.db_env_loader import get_env, mysql_config  # noqa: E402

DB_CONFIG = mysql_config(start=_HERE)
INITIAL_CASH = int(get_env(["BACKTEST_INITIAL_CASH"], "1000000", start=_HERE) or "1000000")
COMMISSION = float(get_env(["BACKTEST_COMMISSION"], "0.0002", start=_HERE) or "0.0002")
POSITION_PCT = int(get_env(["BACKTEST_POSITION_PCT"], "95", start=_HERE) or "95")


def get_connection():
    return pymysql.connect(**DB_CONFIG)


def execute_query(sql, params=None):
    conn = get_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        try:
            cur.execute(sql, params or ())
            return cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()


def execute_update(sql, params=None):
    """执行写操作，返回影响行数。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            n = cur.execute(sql, params or ())
            conn.commit()
            return n
        finally:
            cur.close()
    finally:
        conn.close()


def execute_many(sql, seq_of_params):
    """批量写操作，返回影响行数。"""
    if not seq_of_params:
        return 0
    conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            n = cur.executemany(sql, seq_of_params)
            conn.commit()
            return n if n is not None else len(seq_of_params)
        finally:
            cur.close()
    finally:
        conn.close()
