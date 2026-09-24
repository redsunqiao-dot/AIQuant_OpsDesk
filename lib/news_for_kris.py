# -*- coding: utf-8 -*-
# Kris 事件风控新闻文本 -- 读 MySQL trade_stock_news
"""从 02 行情库取近 N 日个股新闻, 拼成 EventKeywordChecker 可用的 news_text."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Tuple


def load_news_text(stock_code: str, days: int = 3, limit: int = 20) -> Tuple[str, str]:
    """返回 (news_text, status_reason).

    status_reason 说明数据来源或为何为空 (无行 / 查询失败信息).
    有库无行时 news_text='' 且 reason 明确, 不假装已做过事件检查.
    """
    code = str(stock_code or "").strip()
    if not code:
        return "", "股票代码为空"

    since = (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d 00:00:00")
    sql = (
        "SELECT title, content, published_at FROM trade_stock_news "
        "WHERE stock_code=%s AND published_at >= %s "
        "ORDER BY published_at DESC LIMIT %s"
    )
    try:
        from briefing.lib.db_config import execute_query
        rows = execute_query(sql, (code, since, int(limit)))
    except Exception as e:
        raise RuntimeError(f"查询 trade_stock_news 失败: {type(e).__name__}: {e}") from e

    if not rows:
        return "", f"近{days}日 trade_stock_news 无记录 ({code})"

    parts = []
    for r in rows:
        title = str(r.get("title") or "").strip()
        content = str(r.get("content") or "").strip()
        chunk = title
        if content:
            chunk = f"{title}。{content}" if title else content
        if chunk:
            parts.append(chunk)
    text = "\n".join(parts)
    return text, f"已加载 {len(parts)} 条新闻 (近{days}日)"
