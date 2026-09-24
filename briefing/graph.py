# -*- coding: utf-8 -*-
# 投资晨会 LangGraph 工作流（内嵌于本工作台）
"""
晨会 LangGraph: industry -> stock_picker -> radar -> sentiment -> report -> push

数据为 MySQL wucai_trade；连接见 02_行情数据采集/.env。

命令行（CASE-AI 项目根目录）: python -m briefing.graph
"""
from __future__ import annotations
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Annotated, List, Dict, TypedDict
from operator import add
import re

import pandas as pd
from langgraph.graph import END, START, StateGraph

from .pusher import push_all

# 晨报落盘目录（本包内 outputs/reports）
THIS_DIR = Path(__file__).resolve().parent


class MorningState(TypedDict, total=False):
    trigger_time: str
    industry_level: int
    top_n_industries: int
    top_n_stocks: int
    lookback_days: int
    sample_stocks: int
    industry_rank: list
    stock_pool: list
    factor_rank: list
    picked_stocks: list
    radar_section: dict
    sentiment_section: dict
    team_section: dict
    report_md: str
    report_html: str
    push_result: dict
    messages: Annotated[list, add]


def industry_node(state: dict) -> dict:
    print("\n" + "=" * 70)
    print("  [节点 1] industry_node -- 申万二级板块强度 + 一二阶导拐点")
    print("=" * 70)

    from .lib.rotation_runner import rank_industries_with_phase
    level   = state.get("industry_level", 2)
    top_n   = state.get("top_n_industries", 5)
    df = rank_industries_with_phase(level=level,
                                     lookback_days=state.get("lookback_days", 90),
                                     top_n=top_n)

    rank_list = []
    for ind_name, row in df.iterrows():
        rank_list.append({
            "industry":   ind_name,
            "rank":       int(row["composite_rank"]),
            "score":      round(float(row["composite_score"]), 3),
            "raw_score":  round(float(row["score"]), 3),
            "MOM_21":     round(float(row["MOM_21"]) * 100, 2),
            "RS_60":      round(float(row["RS_60"]) * 100, 2),
            "VOL_R":      round(float(row["VOL_RATIO"]), 2),
            "phase":      row.get("phase", "neutral"),
            "phase_desc": row.get("phase_desc", "中性"),
            "ROC_20":     round(float(row.get("ROC_20", 0)), 2),
            "members":    int(row["member_count"]),
        })

    print(f"  Top {top_n} 板块 (申万 {'一' if level == 1 else '二'} 级):")
    for r in rank_list:
        print(f"    [{r['rank']:>2}] {r['industry']:<14s} "
              f"score={r['score']:+.2f} ({r['phase_desc']:<6s})  "
              f"MOM21={r['MOM_21']:+5.2f}%  RS60={r['RS_60']:+5.2f}%  "
              f"ROC20={r['ROC_20']:+5.2f}%")

    return {
        "industry_rank": rank_list,
        "messages": [{"role": "industry", "time": datetime.now().strftime("%H:%M:%S"),
                      "content": f"Top {top_n} 板块: " + ", ".join(r["industry"] for r in rank_list)}],
    }


def stock_picker_node(state: dict) -> dict:
    print("\n" + "=" * 70)
    print("  [节点 2] stock_picker_node -- 强势板块内截面多因子选股 (IC 加权)")
    print("=" * 70)

    from .lib.rotation_runner import get_sector_member_codes
    from .lib.factor_runner import filter_tradable, rank_multifactor_stocks

    industry_rank = state.get("industry_rank", [])
    level = state.get("industry_level", 2)
    if not industry_rank:
        print("  [SKIP] 无行业排名, 跳过选股")
        return {"stock_pool": [], "factor_rank": [], "picked_stocks": []}

    top_stocks: List[str] = []
    industry_to_codes: Dict[str, List[str]] = {}
    for r in industry_rank:
        ind_name = r["industry"]
        codes = get_sector_member_codes(ind_name, level=level)
        codes = filter_tradable(codes)
        industry_to_codes[ind_name] = codes
        top_stocks.extend(codes)
    top_stocks = sorted(set(top_stocks))
    print(f"  候选股票池: {len(top_stocks)} 只 (来自 {len(industry_rank)} 个 Top 板块, 不按代码截断)")

    ind_map = {c: ind for ind, codes in industry_to_codes.items() for c in codes}
    ranked = rank_multifactor_stocks(top_stocks, ind_map)
    if ranked.empty:
        print("  [WARN] 截面样本不足或全部被硬过滤剔除, 跳过选股")
        return {"stock_pool": top_stocks, "factor_rank": [], "picked_stocks": []}

    top_n = state.get("top_n_stocks", 5)
    factor_rank_list = []
    for code, row in ranked.head(top_n * 3).iterrows():
        raw_factors = {}
        for col in ("MOM_1M", "MOM_3M", "MOM_6M", "REV_5D", "VOL_20",
                    "VOL_60", "LIQ_20", "TURN_20", "RSI_14", "BIAS_20"):
            if col in ranked.columns:
                val = row[col]
                raw_factors[col] = None if pd.isna(val) else round(float(val), 4)
        factor_rank_list.append({
            "code":     code,
            "industry": row["industry"],
            "alpha":    round(float(row["score"]), 3),
            "raw_factors": raw_factors,
        })
    picked = factor_rank_list[:top_n]

    print(f"  Top {top_n} 选中标的 (IC 加权 alpha):")
    for p in picked:
        rf = p["raw_factors"]
        print(f"    {p['code']}  [{p['industry']:<6s}]  alpha={p['alpha']:+.3f}  "
              f"MOM_1M={(rf.get('MOM_1M') or 0):+.2%}  "
              f"MOM_3M={(rf.get('MOM_3M') or 0):+.2%}")

    return {
        "stock_pool":    top_stocks,
        "factor_rank":   factor_rank_list,
        "picked_stocks": picked,
        "messages": [{"role": "stock_picker", "time": datetime.now().strftime("%H:%M:%S"),
                      "content": f"选中 {len(picked)} 只: " + ", ".join(p["code"] for p in picked)}],
    }


def radar_section_node(state: dict) -> dict:
    """读取 04 最新精选池, 写入报告章节; 不替换多因子 picked_stocks."""
    print("\n" + "=" * 70)
    print("  [节点 2b] radar_section_node -- TALib 形态精选池摘要")
    print("=" * 70)
    try:
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from lib.radar_pool import load_elite_pool
        pool = load_elite_pool(top_n=5)
    except Exception as e:
        msg = f"精选池读取失败: {type(e).__name__}: {e}"
        print(f"  [WARN] {msg}")
        return {
            "radar_section": {"ok": False, "error": msg, "items": []},
            "messages": [{"role": "radar", "time": datetime.now().strftime("%H:%M:%S"),
                          "content": msg}],
        }
    print(f"  扫描日 {pool.get('scan_date')} 文件={pool.get('path')}")
    for it in pool.get("items") or []:
        print(f"    {it['code']} 综合分={it.get('score')} {it.get('signal')}")
    return {
        "radar_section": {"ok": True, **pool},
        "messages": [{"role": "radar", "time": datetime.now().strftime("%H:%M:%S"),
                      "content": f"精选池 {pool.get('scan_date')}: "
                                 + ", ".join(pool.get("codes") or [])}],
    }


def sentiment_snippet_node(state: dict) -> dict:
    """对多因子 Top3 做轻量舆情摘要 (trade_stock_news 关键词粗分)."""
    print("\n" + "=" * 70)
    print("  [节点 2c] sentiment_snippet_node -- 舆情摘要")
    print("=" * 70)
    picked = state.get("picked_stocks") or []
    top = picked[:3]
    items = []
    for p in top:
        code = p.get("code") if isinstance(p, dict) else str(p)
        try:
            from lib.news_for_kris import load_news_text
            text, status = load_news_text(code, days=3, limit=10)
        except Exception as e:
            items.append({"code": code, "status": f"失败:{type(e).__name__}:{e}",
                          "label": "未知", "snippet": ""})
            continue
        label = "中性"
        if text:
            neg = ("立案", "调查", "处罚", "违规", "亏损", "减持", "退市", "暴雷")
            pos = ("中标", "增持", "预增", "回购", "突破", "合作")
            if any(k in text for k in neg):
                label = "偏空"
            elif any(k in text for k in pos):
                label = "偏多"
        items.append({
            "code": code,
            "status": status,
            "label": label,
            "snippet": (text[:180] + "...") if text and len(text) > 180 else (text or ""),
        })
        print(f"    {code}: {label} | {status}")
    return {
        "sentiment_section": {"ok": True, "items": items},
        "messages": [{"role": "sentiment", "time": datetime.now().strftime("%H:%M:%S"),
                      "content": "舆情: " + ", ".join(
                          f"{x['code']}={x['label']}" for x in items)}],
    }


def report_node(state: dict) -> dict:
    print("\n" + "=" * 70)
    print("  [节点 3] report_node -- 拼装晨报")
    print("=" * 70)

    today_str = datetime.now().strftime("%Y-%m-%d %A")
    industries = state.get("industry_rank", [])
    picked = state.get("picked_stocks", [])

    md_lines = [
        f"# 晨会分析简报 -- {today_str}",
        "",
        f"## Top {len(industries)} 强势板块 (申万二级)",
        "",
        "| Rank | 板块 | 综合分 | 拐点信号 | 21日动量 | 60日相对强度 | 20日ROC |",
        "|------|------|--------|----------|----------|--------------|---------|",
    ]
    for r in industries:
        md_lines.append(
            f"| {r['rank']} | **{r['industry']}** | {r['score']:+.2f} | "
            f"{r.get('phase_desc', '中性')} | "
            f"{r['MOM_21']:+.2f}% | {r['RS_60']:+.2f}% | {r.get('ROC_20', 0):+.2f}% |"
        )
    md_lines += ["", f"## Top {len(picked)} 选中标的 (截面多因子 · IC 加权)", ""]
    md_lines.append("| 代码 | 行业 | 综合alpha | 1M动量 | 3M动量 | 6M动量 |")
    md_lines.append("|------|------|-----------|--------|--------|--------|")
    for p in picked:
        rf = p.get("raw_factors", {})
        md_lines.append(
            f"| `{p['code']}` | {p['industry']} | {p['alpha']:+.3f} | "
            f"{(rf.get('MOM_1M') or 0):+.2%} | "
            f"{(rf.get('MOM_3M') or 0):+.2%} | "
            f"{(rf.get('MOM_6M') or 0):+.2%} |"
        )

    md_lines += ["", "## 盘中应对建议", ""]
    if picked:
        for p in picked:
            rf = p.get("raw_factors", {})
            md_lines.append(
                f"- `{p['code']}` ({p['industry']}): 多因子 alpha={p['alpha']:+.3f}, "
                f"MOM_1M={(rf.get('MOM_1M') or 0):+.2%}, "
                f"MOM_3M={(rf.get('MOM_3M') or 0):+.2%}, "
                f"波动/流动性等因子已按历史 IC 加权计入"
            )
    else:
        md_lines.append("- 无候选标的, 今日观望")

    # 形态精选池章节 (不替换多因子采纳池)
    radar = state.get("radar_section") or {}
    md_lines += ["", "## 形态精选池 (TALib 雷达 · 偏好一买, 仅参考)", ""]
    if radar.get("ok") and radar.get("items"):
        md_lines.append(f"扫描日: {radar.get('scan_date', '')}")
        md_lines.append("")
        md_lines.append("| 代码 | 综合分 | 信号 | 缠论 | ML |")
        md_lines.append("|------|--------|------|------|-----|")
        for it in radar["items"]:
            md_lines.append(
                f"| `{it['code']}` | {it.get('score')} | {it.get('signal','')} | "
                f"{it.get('chan_tag','')} | {it.get('ml_tag','')} |"
            )
    else:
        md_lines.append(f"- {radar.get('error') or '暂无精选池数据'}")

    # 舆情摘要
    sent = state.get("sentiment_section") or {}
    md_lines += ["", "## 舆情摘要 (多因子 Top3)", ""]
    for it in sent.get("items") or []:
        md_lines.append(
            f"- `{it.get('code')}` **{it.get('label')}**: {it.get('status')} "
            f"{('-- ' + it['snippet']) if it.get('snippet') else ''}"
        )
    if not sent.get("items"):
        md_lines.append("- 无舆情摘录")

    # 交易团队附录
    team = state.get("team_section") or {}
    if team.get("items"):
        md_lines += ["", "## 交易团队工作流附录 (Top1-2)", ""]
        for it in team["items"]:
            md_lines.append(
                f"- `{it.get('code')}`: {it.get('summary', it.get('error', ''))}"
            )

    md_lines += ["", "---", "",
                 "> 本简报由 AI 量化团队自动生成, 仅供参考, 不构成投资建议",
                 f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]

    report_md = "\n".join(md_lines)

    report_html = _md_to_html(report_md)

    output_dir = THIS_DIR / "outputs" / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = output_dir / f"morning_brief_{ts}.md"
    html_path = output_dir / f"morning_brief_{ts}.html"
    md_path.write_text(report_md, encoding="utf-8")
    html_path.write_text(report_html, encoding="utf-8")

    print(f"  晨报已落盘:")
    print(f"    Markdown: {md_path}")
    print(f"    HTML:     {html_path}")

    return {
        "report_md":   report_md,
        "report_html": str(html_path),
        "messages": [{"role": "report", "time": datetime.now().strftime("%H:%M:%S"),
                      "content": f"晨报生成 {len(report_md)} 字节"}],
    }


def _md_to_html(md: str) -> str:
    lines = md.splitlines()
    out = ["<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'>",
           "<title>晨会分析简报</title>",
           "<style>",
           "body{font-family:-apple-system,'Microsoft YaHei',sans-serif;max-width:900px;margin:30px auto;padding:0 24px;color:#2c3e50;line-height:1.7}",
           "h1{border-bottom:3px solid #3498db;padding-bottom:10px}",
           "h2{color:#3498db;margin-top:30px}",
           "table{border-collapse:collapse;width:100%;margin:14px 0}",
           "th{background:#34495e;color:#fff;padding:8px 12px;text-align:left}",
           "td{padding:8px 12px;border:1px solid #dee2e6}",
           "tr:nth-child(even){background:#f8f9fa}",
           "code{background:#e8ecef;padding:2px 6px;border-radius:4px;font-family:'Consolas',monospace}",
           "blockquote{border-left:3px solid #95a5a6;color:#555;padding-left:12px;background:#f1f3f5;padding-top:8px;padding-bottom:8px}",
           "</style></head><body>"]

    in_table = False
    table_rows = []
    for line in lines:
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.match(r"^-+$", c) for c in cells):
                continue
            if not in_table:
                in_table = True
                table_rows = ["<table><thead><tr>"]
                for c in cells:
                    table_rows.append(f"<th>{escape(c)}</th>")
                table_rows.append("</tr></thead><tbody>")
            else:
                table_rows.append("<tr>")
                for c in cells:
                    rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", escape(c))
                    rendered = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", rendered)
                    table_rows.append(f"<td>{rendered}</td>")
                table_rows.append("</tr>")
            continue
        else:
            if in_table:
                table_rows.append("</tbody></table>")
                out.extend(table_rows)
                in_table = False
                table_rows = []

        if s.startswith("# "):
            out.append(f"<h1>{escape(s[2:])}</h1>")
        elif s.startswith("## "):
            out.append(f"<h2>{escape(s[3:])}</h2>")
        elif s.startswith("- "):
            li_html = re.sub(r"`([^`]+)`", r"<code>\1</code>", escape(s[2:]))
            out.append(f"<li>{li_html}</li>")
        elif s.startswith("> "):
            out.append(f"<blockquote>{escape(s[2:])}</blockquote>")
        elif s == "---":
            out.append("<hr>")
        elif s == "":
            continue
        else:
            p_html = re.sub(r"`([^`]+)`", r"<code>\1</code>", escape(s))
            out.append(f"<p>{p_html}</p>")

    if in_table:
        table_rows.append("</tbody></table>")
        out.extend(table_rows)

    out.append("</body></html>")
    return "\n".join(out)


def push_node(state: dict) -> dict:
    print("\n" + "=" * 70)
    print("  [节点 4] push_node -- 推送钉钉 / 企业微信")
    print("=" * 70)

    title = f"晨会分析 {datetime.now().strftime('%m-%d')}"
    md = state.get("report_md", "")
    if not md:
        print("  [SKIP] 无内容可推送")
        return {"push_result": {}}

    result = push_all(title=title, content=md)
    return {
        "push_result": result,
        "messages": [{"role": "push", "time": datetime.now().strftime("%H:%M:%S"),
                      "content": f"推送结果: {result}"}],
    }


def team_workflow_node(state: dict) -> dict:
    """对多因子 Top1-2 调用 11 交易团队 LangGraph (auto_approve), 写入附录."""
    print("\n" + "=" * 70)
    print("  [节点 2d] team_workflow_node -- 交易团队工作流 Top1-2")
    print("=" * 70)
    picked = state.get("picked_stocks") or []
    top = picked[:2]
    items = []
    if not top:
        return {
            "team_section": {"ok": True, "items": []},
            "messages": [{"role": "team", "time": datetime.now().strftime("%H:%M:%S"),
                          "content": "无标的, 跳过团队工作流"}],
        }
    try:
        import sys
        from lib.ext_bootstrap import ensure_on_path, resolve_vendor_or_course
        team_dir = resolve_vendor_or_course(
            ("team_workflow",),
            (
                "11_风控与LangGraph",
                "交易团队工作流",
                "CASE-交易团队工作流（langgraph）",
            ),
            "main.py",
        )
        if team_dir is None:
            raise FileNotFoundError("团队工作流目录不存在: vendor/team_workflow")
        ensure_on_path(team_dir)
        from main import run_workflow  # type: ignore
    except Exception as e:
        msg = f"导入交易团队失败: {type(e).__name__}: {e}"
        print(f"  [WARN] {msg}")
        return {
            "team_section": {"ok": False, "error": msg, "items": []},
            "messages": [{"role": "team", "time": datetime.now().strftime("%H:%M:%S"),
                          "content": msg}],
        }

    for p in top:
        code = p.get("code") if isinstance(p, dict) else str(p)
        try:
            result = run_workflow(
                stock=code,
                capital=100_000,
                auto_approve=True,
                question=f"晨会多因子入选 {code}, 请给出投资观点与交易建议",
            )
            summary = str(result)[:300] if result is not None else "无返回"
            if isinstance(result, dict):
                sig = result.get("trade_signal") or result.get("final_decision") or result
                summary = str(sig)[:300]
            items.append({"code": code, "summary": summary})
            print(f"    {code}: {summary[:120]}")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            items.append({"code": code, "error": err})
            print(f"    {code}: 失败 {err}")
    return {
        "team_section": {"ok": True, "items": items},
        "messages": [{"role": "team", "time": datetime.now().strftime("%H:%M:%S"),
                      "content": f"团队工作流完成 {len(items)} 只"}],
    }


def build_graph():
    g = StateGraph(MorningState)
    g.add_node("industry",     industry_node)
    g.add_node("stock_picker", stock_picker_node)
    g.add_node("radar",        radar_section_node)
    g.add_node("sentiment",    sentiment_snippet_node)
    g.add_node("team",         team_workflow_node)
    g.add_node("report",       report_node)
    g.add_node("push",         push_node)

    g.add_edge(START, "industry")
    g.add_edge("industry", "stock_picker")
    g.add_edge("stock_picker", "radar")
    g.add_edge("radar", "sentiment")
    g.add_edge("sentiment", "team")
    g.add_edge("team", "report")
    g.add_edge("report", "push")
    g.add_edge("push", END)

    return g.compile()


def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    import argparse
    parser = argparse.ArgumentParser(description="晨会分析工作流（读库）")
    parser.add_argument("--level", type=int, choices=[1, 2], default=2,
                        help="申万级别 (默认 2 二级板块)")
    parser.add_argument("--top-industries", type=int, default=5,
                        help="选 Top N 强势板块 (默认 5)")
    parser.add_argument("--top-stocks", type=int, default=5,
                        help="最终输出 Top N 选股 (默认 5)")
    parser.add_argument("--sample-per-industry", type=int, default=20,
                        help="保留参数。选股已改为板块内全部可交易股票，不再按此截断")
    parser.add_argument("--lookback", type=int, default=90,
                        help="拉多少日 K 线 (默认 90)")
    args = parser.parse_args()

    print()
    print("#" * 70)
    print("# 投资晨会工作流启动")
    print(f"# 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("#" * 70)

    graph = build_graph()
    result = graph.invoke({
        "trigger_time":     datetime.now().isoformat(timespec="seconds"),
        "industry_level":   args.level,
        "top_n_industries": args.top_industries,
        "top_n_stocks":     args.top_stocks,
        "lookback_days":    args.lookback,
        "sample_stocks":    args.sample_per_industry,
        "messages":         [],
    })

    print("\n" + "#" * 70)
    print("# 工作流执行完成")
    print("#" * 70)
    print()
    print("--- 节点对话历史 ---")
    for m in result.get("messages", []):
        print(f"  [{m['time']}] {m['role']:<14s} | {m['content']}")
    print()
    print(f"晨报路径 (HTML): {result.get('report_html', '')}")
    print(f"推送结果:        {result.get('push_result', {})}")


if __name__ == "__main__":
    main()
