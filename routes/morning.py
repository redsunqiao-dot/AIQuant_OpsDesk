# -*- coding: utf-8 -*-
# 晨会分析路由 -- REST + SSE
"""
GET  /api/morning/cache         -- 读最近一次缓存
GET  /api/morning/stream?...    -- SSE 流式跑工作流, 推送进度
"""

from __future__ import annotations
import asyncio
import json
import queue
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Query
from sse_starlette.sse import EventSourceResponse

from lib.paths import setup_sys_path, OUTPUTS_DIR
setup_sys_path()

router = APIRouter()

# 缓存目录
CACHE_DIR = OUTPUTS_DIR.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------
def _load_latest_cache() -> dict:
    fp = CACHE_DIR / "morning_latest.json"
    if not fp.exists():
        return {}
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(state: dict):
    cache = {
        "saved_at":           datetime.now().isoformat(timespec="seconds"),
        "industry_rank":      state.get("industry_rank", []),
        "picked_stocks":      state.get("picked_stocks", []),
        "radar_section":      state.get("radar_section") or {},
        "sentiment_section":  state.get("sentiment_section") or {},
        "team_section":       state.get("team_section") or {},
        "messages":           state.get("messages", []),
        "report_html":        state.get("report_html", ""),
    }
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    (CACHE_DIR / f"morning_{ts}.json").write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    (CACHE_DIR / "morning_latest.json").write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------
@router.get("/cache")
def get_cache():
    """读最近一次晨会缓存"""
    cache = _load_latest_cache()
    if not cache:
        return {"error": "暂无缓存. 先点 '一键触发晨会' 跑一次"}
    return cache


# ------------------------------------------------------------
NODE_LABELS = {
    "industry":     "[1/7] industry -- 读库算板块强度与拐点",
    "stock_picker": "[2/7] stock_picker -- 截面多因子选股",
    "radar":        "[3/7] radar -- TALib 精选池摘要",
    "sentiment":    "[4/7] sentiment -- 舆情摘要",
    "team":         "[5/7] team -- 交易团队工作流附录",
    "report":       "[6/7] report -- 拼装晨报 HTML",
    "push":         "[7/7] push -- 推送到钉钉/微信",
}
NODE_EST = {
    "industry":     "读 MySQL 板块/指数表并排名",
    "stock_picker": "强势板块内 IC 加权多因子排序",
    "radar":        "读 04 最新精选池 CSV",
    "sentiment":    "读 trade_stock_news 粗分",
    "team":         "Top1-2 交易团队 LangGraph (可能较慢)",
    "report":       "~1 秒",
    "push":         "~1 秒",
}
NODE_ORDER = ["industry", "stock_picker", "radar", "sentiment", "team", "report", "push"]


@router.get("/stream")
async def stream(
    top_industries: int = Query(3),
    top_stocks: int = Query(5),
    sample_per_industry: int = Query(15),
    lookback: int = Query(90),
):
    """SSE 流式触发晨会, 边跑边推中间状态"""

    initial_state = {
        "trigger_time":     datetime.now().isoformat(timespec="seconds"),
        "top_n_industries": top_industries,
        "top_n_stocks":     top_stocks,
        "sample_stocks":    sample_per_industry,
        "lookback_days":    lookback,
        "messages":         [],
    }

    accumulated_state = dict(initial_state)
    accumulated_state["messages"] = []
    chunk_queue: "queue.Queue" = queue.Queue()

    def worker():
        try:
            from briefing.graph import build_graph
            graph = build_graph()
            for chunk in graph.stream(initial_state, stream_mode="updates"):
                chunk_queue.put(("update", chunk))
            chunk_queue.put(("done", None))
        except Exception as e:
            chunk_queue.put(("error", e))

    th = threading.Thread(target=worker, daemon=True)
    th.start()

    async def event_gen():
        current_node = NODE_ORDER[0]
        started_at = time.time()

        # 立刻发一次
        yield {
            "event": "progress",
            "data": json.dumps({
                "current_node": current_node,
                "estimate":     NODE_EST.get(current_node, ""),
                "message":      "准备启动晨会工作流",
            }),
        }

        last_progress_at = time.time()
        while True:
            # 拿一条 chunk (不阻塞太久, 1 秒内即返回)
            try:
                kind, payload = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: chunk_queue.get(timeout=1.0)
                )
            except queue.Empty:
                kind, payload = None, None

            if kind == "update":
                chunk = payload
                node_name = list(chunk.keys())[0]
                delta = chunk[node_name]

                # 累积
                for k, v in delta.items():
                    if k == "messages" and isinstance(v, list):
                        accumulated_state.setdefault("messages", []).extend(v)
                    else:
                        accumulated_state[k] = v

                # 推断下一个节点
                if node_name in NODE_ORDER:
                    idx = NODE_ORDER.index(node_name)
                    if idx + 1 < len(NODE_ORDER):
                        current_node = NODE_ORDER[idx + 1]

                yield {
                    "event": "node_done",
                    "data": json.dumps({
                        "node":              node_name,
                        "node_label":        NODE_LABELS.get(node_name, node_name),
                        "industry_rank":     accumulated_state.get("industry_rank", []),
                        "picked_stocks":     accumulated_state.get("picked_stocks", []),
                        "radar_section":     accumulated_state.get("radar_section") or {},
                        "sentiment_section": accumulated_state.get("sentiment_section") or {},
                        "team_section":      accumulated_state.get("team_section") or {},
                        "messages":          accumulated_state.get("messages", []),
                    }),
                }
                last_progress_at = time.time()
                continue

            if kind == "done":
                # 落盘缓存，并用晨会入选整池替换监控名单（回测匹配放线程池, 不堵事件循环）
                watch_msg = ""
                try:
                    _save_cache(accumulated_state)
                except Exception:
                    pass
                picked = accumulated_state.get("picked_stocks") or []
                codes = [
                    p.get("code") for p in picked
                    if isinstance(p, dict) and p.get("code")
                ]
                if codes:
                    yield {
                        "event": "progress",
                        "data": json.dumps({
                            "current_node": "match",
                            "estimate": "近一年 + 再往前一年日线回测匹配策略 (较慢)",
                            "message": f"正在为 {len(codes)} 只股票回测匹配策略...",
                        }),
                    }
                    try:
                        from routes.live import _SIM

                        def _adopt():
                            return _SIM.adopt_morning_watch(codes)

                        watch_msg = await asyncio.get_event_loop().run_in_executor(
                            None, _adopt,
                        )
                        print(f"[morning] {watch_msg}", flush=True)
                    except Exception as e:
                        watch_msg = f"写入监控池失败: {type(e).__name__}: {e}"
                        print(f"[morning] {watch_msg}", flush=True)
                else:
                    watch_msg = "[SKIP] 晨会入选为空, 监控池未改"
                yield {
                    "event": "done",
                    "data": json.dumps({
                        "industry_rank":     accumulated_state.get("industry_rank", []),
                        "picked_stocks":     accumulated_state.get("picked_stocks", []),
                        "radar_section":     accumulated_state.get("radar_section") or {},
                        "sentiment_section": accumulated_state.get("sentiment_section") or {},
                        "team_section":      accumulated_state.get("team_section") or {},
                        "messages":          accumulated_state.get("messages", []),
                        "report_html":       accumulated_state.get("report_html", ""),
                        "watch_pool":        watch_msg,
                    }),
                }
                break

            if kind == "error":
                yield {
                    "event": "error_event",
                    "data": json.dumps({
                        "error": f"{type(payload).__name__}: {payload}",
                    }),
                }
                break

            # 心跳 (每 1.5 秒)
            now = time.time()
            if now - last_progress_at > 1.5:
                last_progress_at = now
                yield {
                    "event": "progress",
                    "data": json.dumps({
                        "current_node": current_node,
                        "estimate":     NODE_EST.get(current_node, ""),
                        "message":      f"已运行 {now - started_at:.1f} 秒",
                    }),
                }

    return EventSourceResponse(event_gen())
