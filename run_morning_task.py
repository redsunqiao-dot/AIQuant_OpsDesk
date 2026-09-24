# -*- coding: utf-8 -*-
# 晨会定时入口：跑 graph -> 落盘缓存 -> 推送 -> 监控池替换（按双段日线回测匹配策略）
"""
供 Windows 计划任务调用。在 OpsDesk 项目根目录执行:

    python run_morning_task.py

日志由 run_morning_task.bat 重定向到 outputs/run_morning_*.log
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from lib.paths import OUTPUTS_DIR, setup_sys_path

setup_sys_path()

from briefing.graph import build_graph


CACHE_DIR = OUTPUTS_DIR.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _save_cache(state: dict) -> Path:
    cache = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "industry_rank": state.get("industry_rank", []),
        "picked_stocks": state.get("picked_stocks", []),
        "radar_section": state.get("radar_section") or {},
        "sentiment_section": state.get("sentiment_section") or {},
        "team_section": state.get("team_section") or {},
        "messages": state.get("messages", []),
        "report_html": state.get("report_html", ""),
    }
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    latest = CACHE_DIR / "morning_latest.json"
    (CACHE_DIR / f"morning_{ts}.json").write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    latest.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return latest


def main() -> int:
    print("#" * 70)
    print("# 晨会定时任务启动")
    print(f"# 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("#" * 70)

    graph = build_graph()
    result = graph.invoke({
        "trigger_time": datetime.now().isoformat(timespec="seconds"),
        "industry_level": 2,
        "top_n_industries": 5,
        "top_n_stocks": 5,
        "lookback_days": 90,
        "sample_stocks": 20,
        "messages": [],
    })

    cache_path = _save_cache(result)
    print(f"[OK] 缓存已写入: {cache_path}")

    picked = result.get("picked_stocks") or []
    codes = [p.get("code") for p in picked if isinstance(p, dict) and p.get("code")]
    watch_msg = ""
    if codes:
        try:
            from lib.live_simulator import LiveSimRunner
            # 不传 strategy: 近一年 + 再往前一年日线回测匹配
            print(f"[match] 开始回测匹配策略, 共 {len(codes)} 只: {codes}", flush=True)
            watch_msg = LiveSimRunner().adopt_morning_watch(codes)
            print(f"[OK] {watch_msg}")
        except Exception as e:
            watch_msg = f"写入监控池失败: {type(e).__name__}: {e}"
            print(f"[WARN] {watch_msg}")
    else:
        print("[SKIP] 无入选标的, 监控池未改")

    print()
    print("--- 节点对话 ---")
    for m in result.get("messages", []):
        print(f"  [{m.get('time')}] {m.get('role')}: {m.get('content')}")
    print()
    print(f"晨报 HTML: {result.get('report_html', '')}")
    print(f"推送结果:  {result.get('push_result', {})}")
    print(f"监控池:    {watch_msg}")
    print("#" * 70)
    print("# 晨会定时任务完成")
    print("#" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
