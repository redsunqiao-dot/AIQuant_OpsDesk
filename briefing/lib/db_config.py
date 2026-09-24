# -*- coding: utf-8 -*-
"""
数据库配置：优先作战台根 .env / vendor/db_env/.env 的 QUANT_TRADE_*；
兼容课程仓 02_行情数据采集/.env 与旧键 WUCAI_SQL_*。
"""
from pathlib import Path
import sys
import time
import pymysql

_HERE = Path(__file__).resolve().parent
# briefing/lib -> 作战台根
_ROOT = _HERE.parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.db_env_loader import get_env, mysql_config  # noqa: E402

DB_CONFIG = mysql_config(start=_HERE)
# 去掉 connect_timeout 以外业务方若只传 host 等也可；保留完整字典
INITIAL_CASH = int(get_env(["BACKTEST_INITIAL_CASH"], "1000000", start=_HERE) or "1000000")
COMMISSION = float(get_env(["BACKTEST_COMMISSION"], "0.0002", start=_HERE) or "0.0002")
POSITION_PCT = int(get_env(["BACKTEST_POSITION_PCT"], "95", start=_HERE) or "95")


def get_connection(retries: int = 8, wait_sec: float = 2.0):
    """建立连接；遇 Windows 10048 端口耗尽时短暂重试。"""
    last_err = None
    for i in range(max(1, retries)):
        try:
            return pymysql.connect(**DB_CONFIG)
        except Exception as e:
            last_err = e
            msg = str(e)
            if "10048" not in msg and "Can't connect" not in msg:
                raise
            time.sleep(wait_sec * (i + 1))
    raise last_err


def execute_query(sql, params=None, conn=None):
    """查询；conn 可选，传入时复用外部连接（调用方负责 close）。"""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        try:
            if params is None:
                cur.execute(sql)
            else:
                cur.execute(sql, params)
            return cur.fetchall()
        finally:
            cur.close()
    finally:
        if own:
            conn.close()


def execute_update(sql, params=None, conn=None):
    """执行写操作，返回影响行数。conn 可选，复用时由调用方 commit/close。"""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            if params is None:
                n = cur.execute(sql)
            else:
                n = cur.execute(sql, params)
            if own:
                conn.commit()
            return n
        finally:
            cur.close()
    finally:
        if own:
            conn.close()


def execute_many(sql, seq_of_params, conn=None):
    """批量写操作，返回影响行数。conn 可选，复用时由调用方 commit/close。"""
    if not seq_of_params:
        return 0
    own = conn is None
    if own:
        conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            n = cur.executemany(sql, seq_of_params)
            if own:
                conn.commit()
            return n if n is not None else len(seq_of_params)
        finally:
            cur.close()
    finally:
        if own:
            conn.close()
