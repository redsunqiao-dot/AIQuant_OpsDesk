# -*- coding: utf-8 -*-
"""
数据库配置（内嵌于作战台 vendor/market_data_prep）。

优先读作战台根 .env / vendor/db_env/.env 的 QUANT_TRADE_*；
兼容课程仓 02_行情数据采集/.env 与旧键 WUCAI_SQL_*。
"""
from pathlib import Path
import sys
import time
import pymysql
from dotenv import dotenv_values

_HERE = Path(__file__).resolve().parent


def _find_opsdesk_root() -> Path | None:
    for p in [_HERE, *_HERE.parents]:
        if (p / "app.py").exists() and (p / "lib").is_dir():
            return p
    return None


def _load(path: Path) -> dict:
    if path.exists():
        return dict(dotenv_values(path) or {})
    return {}


def _env_maps() -> list[dict]:
    maps: list[dict] = []
    root = _find_opsdesk_root()
    if root is not None:
        maps.append(_load(root / ".env"))
        maps.append(_load(root / "vendor" / "db_env" / ".env"))
        # 可复用作战台统一加载器
        lib = root / "lib"
        if str(lib) not in sys.path:
            sys.path.insert(0, str(root))
    for p in [_HERE, *_HERE.parents]:
        cand = p / "02_行情数据采集" / ".env"
        if cand.exists():
            maps.append(_load(cand))
            break
    for p in [_HERE, *_HERE.parents]:
        maps.append(_load(p / ".env"))
        name = p.name
        if len(name) >= 3 and name[:2].isdigit() and name[2] == "_":
            break
    return maps


def _get(keys, default=None):
    if isinstance(keys, str):
        keys = [keys]
    for env in _env_maps():
        for k in keys:
            v = env.get(k)
            if v is not None and str(v).strip() != "":
                return str(v).strip()
    return default


def _require_db(keys_primary, keys_fallback):
    v = _get(keys_primary)
    if not v:
        v = _get(keys_fallback)
    if not v:
        raise RuntimeError(
            f"缺少数据库配置 {keys_primary}，"
            f"请在作战台根 .env 或 vendor/db_env/.env 中设置"
        )
    return v


DB_CONFIG = {
    "host": _require_db(["QUANT_TRADE_CHARLES_DB_HOST"], ["WUCAI_SQL_HOST"]),
    "user": _require_db(["QUANT_TRADE_CHARLES_DB_USER"], ["WUCAI_SQL_USERNAME"]),
    "password": _require_db(["QUANT_TRADE_CHARLES_DB_PASSWORD"], ["WUCAI_SQL_PASSWORD"]),
    "database": _require_db(["QUANT_TRADE_CHARLES_DB_NAME"], ["WUCAI_SQL_DB"]),
    "port": int(_get(["QUANT_TRADE_CHARLES_DB_PORT", "WUCAI_SQL_PORT"], "3306")),
    "charset": "utf8mb4",
    "connect_timeout": 10,
}

INITIAL_CASH = int(_get(["BACKTEST_INITIAL_CASH"], "1000000"))
COMMISSION = float(_get(["BACKTEST_COMMISSION"], "0.0002"))
POSITION_PCT = int(_get(["BACKTEST_POSITION_PCT"], "95"))


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
    """批量写操作，返回影响行数。"""
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
