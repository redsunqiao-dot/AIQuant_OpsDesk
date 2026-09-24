# -*- coding: utf-8 -*-
"""统一读取 MySQL / 业务 .env。

优先级（高 -> 低）:
1. 作战台根目录 .env（含 app.py 的目录）
2. vendor/db_env/.env（迁机用的库配置模块）
3. 课程仓 02_行情数据采集/.env（兼容旧布局）
4. 调用方附近的 .env / 进程环境变量
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values


def find_opsdesk_root(start: Path | None = None) -> Path | None:
    """自 start 向上找含 app.py 的作战台根。"""
    cur = (start or Path(__file__).resolve()).resolve()
    if cur.is_file():
        cur = cur.parent
    for p in [cur, *cur.parents]:
        if (p / "app.py").exists() and (p / "lib").is_dir():
            return p
    return None


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return dict(dotenv_values(path) or {})


def collect_env_maps(start: Path | None = None) -> list[dict]:
    """按优先级返回多个 env dict，供 _get 顺序查找。"""
    maps: list[dict] = []
    seen: set[str] = set()

    def _add(d: dict, label: str) -> None:
        if not d:
            return
        key = label
        if key in seen:
            return
        seen.add(key)
        maps.append(d)

    root = find_opsdesk_root(start)
    if root is not None:
        _add(_load(root / ".env"), f"root:{root}")
        _add(_load(root / "vendor" / "db_env" / ".env"), f"vendor_db:{root}")

    here = (start or Path(__file__).resolve()).resolve()
    if here.is_file():
        here = here.parent

    # 兼容: 仓库内 02_行情数据采集/.env
    for p in [here, *here.parents]:
        cand = p / "02_行情数据采集" / ".env"
        if cand.exists():
            _add(_load(cand), f"course02:{cand}")
            break

    # 调用方附近 .env（到阶段目录为止）
    for p in [here, *here.parents]:
        _add(_load(p / ".env"), f"near:{p}")
        name = p.name
        if len(name) >= 3 and name[:2].isdigit() and name[2] == "_":
            break

    # 进程环境（最低优先级，单独在 get_env 里拼）
    return maps


def get_env(
    keys: str | list[str],
    default: str | None = None,
    *,
    start: Path | None = None,
) -> str | None:
    if isinstance(keys, str):
        keys = [keys]
    for env in collect_env_maps(start):
        for k in keys:
            v = env.get(k)
            if v is not None and str(v).strip() != "":
                return str(v).strip()
    for k in keys:
        v = os.environ.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def require_db(
    keys_primary: list[str],
    keys_fallback: list[str],
    *,
    start: Path | None = None,
) -> str:
    v = get_env(keys_primary, start=start)
    if not v:
        v = get_env(keys_fallback, start=start)
    if not v:
        raise RuntimeError(
            f"缺少数据库配置 {keys_primary}，"
            f"请在作战台根 .env 或 vendor/db_env/.env 中设置 QUANT_TRADE_*"
        )
    return v


def mysql_config(start: Path | None = None) -> dict[str, Any]:
    """标准 pymysql 连接参数。"""
    return {
        "host": require_db(
            ["QUANT_TRADE_CHARLES_DB_HOST"], ["WUCAI_SQL_HOST"], start=start
        ),
        "user": require_db(
            ["QUANT_TRADE_CHARLES_DB_USER"], ["WUCAI_SQL_USERNAME"], start=start
        ),
        "password": require_db(
            ["QUANT_TRADE_CHARLES_DB_PASSWORD"], ["WUCAI_SQL_PASSWORD"], start=start
        ),
        "database": require_db(
            ["QUANT_TRADE_CHARLES_DB_NAME"], ["WUCAI_SQL_DB"], start=start
        ),
        "port": int(
            get_env(
                ["QUANT_TRADE_CHARLES_DB_PORT", "WUCAI_SQL_PORT"],
                "3306",
                start=start,
            )
            or "3306"
        ),
        "charset": "utf8mb4",
        "connect_timeout": 10,
    }
