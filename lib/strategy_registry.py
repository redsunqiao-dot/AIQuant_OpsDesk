# -*- coding: utf-8 -*-
# 25-AI量化系统 策略注册中心 -- 将多类策略统一封装为 (code, market, capital) -> signal
"""
StrategyRegistry -- 策略注册中心 (单只股票视角)

设计理念:
    - live_loop 每轮对 watch 池的每只股票调一次 evaluator
    - 我们这里把"全市场扫描"型策略 (多因子/龙头) 也包装成"单只股票评估"模式
    - 每个策略都返回统一格式: {"side": "buy"/"sell"/"hold", "strategy": str, "reason": str}

技术指标里 MACD 有两种 (名称里写清周期, 避免和日线混淆):

        - macd_5min      5 分钟 K 线, 参数 12/26/9 (快线/慢线指「根数」为 5 分钟 bar)
        - macd_1d        日 K 线, 参数 12/26/9 (经典「日线 MACD」)

    [技术指标]
        - macd_5min      5min K 线 MACD (日内短线)
        - macd_1d        日 K 线 MACD (波段)
        - dual_ma_5min   5min K 线 5/20 EMA 双均线
        - ma20_hold      日 K 收盘突破 MA20 买入, 跌破 MA20 卖出

    [量化选股]
        - multi_factor_top   多因子轻量版 (MOM_1M + RSI + BIAS_20 三因子合成)

    [龙头动量]
        - dragon_picker  当日涨幅 + 量比 + 价位 计算龙头分

    [震荡网格]
        - grid_classic   过去 60 日 high/low 切 8 格的经典网格

每个策略都做了健壮兜底: 数据不足 / 异常 -> 返回 hold (不会让循环挂掉)
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ============================================================
# 数据类
# ============================================================

@dataclass
class StrategyMeta:
    """策略元信息"""
    name: str                      # 唯一标识 (路由表里用)
    label: str                     # 中文显示名
    group: str                     # 分组 (技术指标 / 量化选股 / 龙头动量 / 震荡网格)
    description: str             # 一句话说明 (列表/折叠区仍用)
    evaluator: Callable            # (code, market, capital) -> dict
    scenario: str = ""             # 适用场景 (弹窗)
    rules: str = ""              # 规则要点 (弹窗)
    example: str = ""            # 简短示例 (弹窗)


# ============================================================
# 注册中心
# ============================================================

_REGISTRY: Dict[str, StrategyMeta] = {}


def register(
    name: str,
    label: str,
    group: str,
    description: str = "",
    *,
    scenario: str = "",
    rules: str = "",
    example: str = "",
):
    """装饰器: 注册一个策略 (scenario / rules / example 供持仓说明弹窗结构化展示)"""
    def deco(fn: Callable) -> Callable:
        _REGISTRY[name] = StrategyMeta(
            name=name,
            label=label,
            group=group,
            description=description,
            evaluator=fn,
            scenario=scenario,
            rules=rules,
            example=example,
        )
        return fn
    return deco


def get_strategy(name: str) -> Optional[StrategyMeta]:
    return _REGISTRY.get(name)


def list_strategies() -> List[Dict[str, str]]:
    """按分组返回所有可用策略 (供前端展示)"""
    out = []
    for meta in _REGISTRY.values():
        out.append({
            "name":        meta.name,
            "label":       meta.label,
            "group":       meta.group,
            "description": meta.description,
            "scenario":    meta.scenario,
            "rules":       meta.rules,
            "example":     meta.example,
        })
    return out


def list_groups() -> Dict[str, List[Dict[str, str]]]:
    """按分组聚合: {group: [{name, label, desc}, ...]}"""
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for meta in _REGISTRY.values():
        grouped.setdefault(meta.group, []).append({
            "name":        meta.name,
            "label":       meta.label,
            "description": meta.description,
            "scenario":    meta.scenario,
            "rules":       meta.rules,
            "example":     meta.example,
        })
    return grouped


# ============================================================
# 通用工具: 拉 K 线 (容错)
# ============================================================

def _safe_kline(market, code: str, period: str, count: int):
    """拉 K 线, 拿不到返回 None"""
    try:
        df = market.get_recent_kline(code, period=period, count=count)
        if df is None or len(df) == 0:
            return None
        return df
    except Exception:
        return None


def _hold(strategy: str, reason: str = "") -> dict:
    return {"side": "hold", "strategy": strategy, "reason": reason}


def _signal(side: str, strategy: str, reason: str = "") -> dict:
    return {"side": side, "strategy": strategy, "reason": reason}


def _macd_cross_from_close(close, strategy_name: str, bar_desc: str) -> dict:
    """
    经典 MACD: 快线=收盘 EMA12, 慢线=收盘 EMA26, DIF=快线-慢线, DEA=DIF 的 EMA9;
    信号: DIF 上穿 DEA 买, 下穿卖。
    bar_desc: 用于 reason 里标明周期, 如 "5min" / "日K"
    """
    if close is None or len(close) < 30:
        return _hold(strategy_name, "K 线不足 30 根")
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    if len(dif) < 2:
        return _hold(strategy_name, "DIF 序列过短")
    prev = dif.iloc[-2] - dea.iloc[-2]
    curr = dif.iloc[-1] - dea.iloc[-1]
    if prev <= 0 and curr > 0:
        return _signal("buy", strategy_name,
                       f"[{bar_desc}] 金叉 12/26/9 DIF-DEA={curr:+.4f}")
    if prev >= 0 and curr < 0:
        return _signal("sell", strategy_name,
                       f"[{bar_desc}] 死叉 12/26/9 DIF-DEA={curr:+.4f}")
    return _hold(strategy_name, f"[{bar_desc}] 无交叉")


# ============================================================
# 策略 1a: MACD 5min（日内短线，非日线）
# ============================================================

@register(
    name="macd_5min",
    label="MACD·5分钟K线 (12/26/9·日内)",
    group="技术指标",
    description="基于 5 分钟收盘价的经典参数 12/26/9 (快线慢线指 K 线根数, 非日历日)。偏日内节奏。A 股现货 T+1: 当日买入次日才能卖, 若更关心中线波段可改用「MACD·日K线」。",
    scenario="看盘内几分钟到数小时的涨跌节奏，希望信号跟得上分时波动、做短线参考时。",
    rules="用 5 分钟 K 线收盘价计算 MACD(12/26/9)：DIF 从下向上穿过 DEA 为金叉（偏买入）；从上向下穿过为死叉（偏卖出）。K 线不足则保持观望。",
    example="急跌后 DIF 再次上穿 DEA，可能对应一小段反弹；震荡市里交叉会较频繁，需结合风控。",
)
def strat_macd_5min(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "5m", 50)
    if df is None:
        return _hold("macd_5min", "无 5 分钟 K 线")
    close = df["close"].astype(float)
    return _macd_cross_from_close(close, "macd_5min", "5min")


# ============================================================
# 策略 1b: MACD 日线 (经典 12/26/9, 日 K 收盘)
# ============================================================

@register(
    name="macd_1d",
    label="MACD·日K线 (12/26/9·波段)",
    group="技术指标",
    description="基于日 K 收盘价的 12/26/9, 与常见软件「日线 MACD」一致。适合多日持仓与 T+1 下的波段决策; 信号比 5 分钟 MACD 稀疏。",
    scenario="更做隔日、波段，不想被 5 分钟频繁交叉打扰；与 A 股 T+1「今日买明日卖」的节奏更接近时。",
    rules="用日 K 收盘价算经典 MACD(12/26/9)，金叉 / 死叉含义与 5 分钟版相同，只是每根 K 代表一个交易日。",
    example="连续回调后日线出现金叉，常作为波段关注信号之一（是否下单仍看资金与风控）。",
)
def strat_macd_1d(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 80)
    if df is None:
        return _hold("macd_1d", "无日 K 线")
    close = df["close"].astype(float)
    return _macd_cross_from_close(close, "macd_1d", "日K")


# ============================================================
# 策略 2: 双均线 5min
# ============================================================

@register(
    name="dual_ma_5min",
    label="双均线 5min (5/20 EMA)",
    group="技术指标",
    description="5 分钟 K 线: 5EMA 上穿 20EMA 买入, 下穿卖出",
    scenario="喜欢「快慢线交叉」这种直观规则，且希望比日线更快反应时。",
    rules="在 5 分钟收盘价上计算 5 周期与 20 周期指数均线；快线上穿慢线 → 偏买；快线下穿慢线 → 偏卖。",
    example="横盘后快线上穿慢线，可理解为短期均线重新站到长期均线上方，常当作转强信号之一。",
)
def strat_dual_ma_5min(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "5m", 50)
    if df is None or len(df) < 25:
        return _hold("dual_ma_5min", "K 线不足 25 根")
    close = df["close"].astype(float)
    fast = close.ewm(span=5, adjust=False).mean()
    slow = close.ewm(span=20, adjust=False).mean()
    if len(fast) < 2:
        return _hold("dual_ma_5min")
    prev_diff = fast.iloc[-2] - slow.iloc[-2]
    curr_diff = fast.iloc[-1] - slow.iloc[-1]
    if prev_diff <= 0 and curr_diff > 0:
        return _signal("buy", "dual_ma_5min",
                       f"5EMA 上穿 20EMA, diff={curr_diff:+.3f}")
    if prev_diff >= 0 and curr_diff < 0:
        return _signal("sell", "dual_ma_5min",
                       f"5EMA 下穿 20EMA, diff={curr_diff:+.3f}")
    return _hold("dual_ma_5min")


# ============================================================
# 策略 2b: MA20 持股法 (日 K, 价格与 MA20 交叉)
# ============================================================
# 规则与 dual_ma_5min 同属「均线交叉」族, 换成收盘价 vs 简单 MA20:
#   - 前一日收盘在 MA20 及以下、当日收盘站上 MA20 -> buy
#   - 前一日收盘在 MA20 及以上、当日收盘跌穿 MA20 -> sell

@register(
    name="ma20_hold",
    label="MA20 持股法 (日K 突破/跌破)",
    group="技术指标",
    description="日 K 线: 收盘向上突破 MA20 买入, 向下跌破 MA20 卖出 (站上持股、跌破离场)",
    scenario="想用最简单的「20 日线」做波段过滤: 站上认为趋势转强可介入, 跌破则离场观望时。",
    rules="用日 K 收盘价计算 MA20。前一日收盘 ≤ MA20 且当日收盘 > MA20 → 偏买；前一日收盘 ≥ MA20 且当日收盘 < MA20 → 偏卖；其余观望。",
    example="整理后首日阳线收盘站上 MA20 触发买入；之后若回调收在 MA20 下方, 触发卖出离场。",
)
def strat_ma20_hold(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 80)
    if df is None or len(df) < 22:
        return _hold("ma20_hold", "日 K 不足 22 根")
    close = df["close"].astype(float)
    ma20 = close.rolling(20).mean()
    if math.isnan(ma20.iloc[-1]) or math.isnan(ma20.iloc[-2]) or float(ma20.iloc[-1]) <= 0:
        return _hold("ma20_hold", "MA20 不可用")
    prev_c = float(close.iloc[-2])
    curr_c = float(close.iloc[-1])
    prev_m = float(ma20.iloc[-2])
    curr_m = float(ma20.iloc[-1])
    if prev_c <= prev_m and curr_c > curr_m:
        return _signal(
            "buy",
            "ma20_hold",
            f"日K 收盘 {prev_c:.2f}->{curr_c:.2f} 突破 MA20 {prev_m:.2f}->{curr_m:.2f}",
        )
    if prev_c >= prev_m and curr_c < curr_m:
        return _signal(
            "sell",
            "ma20_hold",
            f"日K 收盘 {prev_c:.2f}->{curr_c:.2f} 跌破 MA20 {prev_m:.2f}->{curr_m:.2f}",
        )
    return _hold(
        "ma20_hold",
        f"收盘 {curr_c:.2f} MA20 {curr_m:.2f} 无穿越",
    )


# ============================================================
# 策略 3: 多因子轻量版 (MOM_1M + RSI_14 + BIAS_20 合成)
# ============================================================
# 全市场截面多因子原版是"全市场截面 + IC 加权", 单只股做不了截面比较
# 这里改成"绝对阈值"版: 三个因子分别打分 [-1, +1] 后求平均

@register(
    name="multi_factor_top",
    label="多因子轻量 (动量+RSI+乖离)",
    group="量化选股",
    description="日线 MOM_1M + RSI_14 + BIAS_20 合成 alpha, > 0.3 买, < -0.3 卖",
    scenario="单只股票也想用「动量 + 超买超卖 + 乖离」综合打分，而不是只看一条均线时。",
    rules="在日 K 上算三类因子并归一后取平均得到 alpha；alpha 高于阈值偏买入，低于负阈值偏卖出；中间区间观望。",
    example="alpha 从负区间一举升到阈值之上，表示多因子同时转强，可能触发买入侧信号。",
)
def strat_multi_factor(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 200)
    if df is None or len(df) < 30:
        return _hold("multi_factor_top", "日 K 线不足 30 根")
    close = df["close"].astype(float)

    # 因子 1: 1 个月动量 (21 日涨跌幅), 映射到 [-1, +1] (15% 涨幅 -> 1.0)
    if len(close) >= 22:
        mom_1m = close.iloc[-1] / close.iloc[-22] - 1.0
    else:
        mom_1m = 0.0
    f_mom = max(-1.0, min(1.0, mom_1m / 0.15))

    # 因子 2: RSI_14, 偏离 50 越远动能越强; > 70 超买 (反转减分), < 30 超卖 (反转加分)
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = (100 - 100 / (1 + rs)).iloc[-1]
    if rsi > 70:
        f_rsi = -((rsi - 70) / 30)        # 超买扣分
    elif rsi < 30:
        f_rsi = (30 - rsi) / 30           # 超卖加分 (反转买入)
    else:
        f_rsi = (rsi - 50) / 50           # 中间区随动能

    # 因子 3: BIAS_20 (乖离率), 过涨易回调 (取负号)
    if len(close) >= 20:
        ma20 = close.rolling(20).mean().iloc[-1]
        bias = (close.iloc[-1] - ma20) / ma20 if ma20 > 0 else 0.0
    else:
        bias = 0.0
    f_bias = max(-1.0, min(1.0, -bias / 0.10))   # +/- 10% 乖离 -> +/- 1.0

    alpha = (f_mom + f_rsi + f_bias) / 3
    reason = (f"MOM_1M={mom_1m:+.2%} RSI={rsi:.1f} BIAS={bias:+.2%} "
              f"-> alpha={alpha:+.2f}")

    if alpha > 0.30:
        return _signal("buy", "multi_factor_top", reason)
    if alpha < -0.30:
        return _signal("sell", "multi_factor_top", reason)
    return _hold("multi_factor_top", reason)


# ============================================================
# 策略 3b: ML 涨跌概率 (与 WF / 回测共用 run_ml_prob)
# ============================================================

@register(
    name="ml_prob",
    label="ML涨跌概率 (XGBoost)",
    group="量化选股",
    description="日线滚动训练 XGBoost 预测次日上涨概率; >0.6 买, <0.4 卖。与复盘 Walk-Forward 同一入口。",
    scenario="想把机器学习因子直接挂到监控票上，与技术指标策略对照时。",
    rules="用近约 200 根日 K 做特征与滚动训练，得到最新上涨概率 last_prob；高于买入阈值偏多，低于卖出阈值偏空，中间观望。",
    example="last_prob 从 0.45 升到 0.68，跨过 0.6 阈值，给出买入侧信号。",
)
def strat_ml_prob(code: str, market, capital: float) -> dict:
    """研究栈 Phase1: 把 ml.run_ml_prob 接到 live 路由。"""
    df = _safe_kline(market, code, "1d", 200)
    if df is None or len(df) < 150:
        return _hold("ml_prob", f"日 K 不足 150 根 (现 {0 if df is None else len(df)})")
    try:
        from ml.ml_prob_runner import run_ml_prob
    except Exception as e:
        return _hold("ml_prob", f"导入 ml 失败: {type(e).__name__}: {e}")

    buy_th, sell_th = 0.6, 0.4
    try:
        out = run_ml_prob(
            df,
            train_days=120,
            retrain_interval=20,
            buy_th=buy_th,
            sell_th=sell_th,
            code=code,
            verbose=False,
        )
    except Exception as e:
        return _hold("ml_prob", f"ML 计算异常: {type(e).__name__}: {e}")

    meta = out.get("meta") or {}
    last_prob = float(meta.get("last_prob", 0.5))
    cached = "缓存" if out.get("cached") else "新算"
    reason = f"last_prob={last_prob:.3f} 买阈{buy_th}/卖阈{sell_th} ({cached})"

    if last_prob > buy_th:
        return _signal("buy", "ml_prob", reason)
    if last_prob < sell_th:
        return _signal("sell", "ml_prob", reason)
    return _hold("ml_prob", reason)


# ============================================================
# 策略 3c: 缠论买卖点 (复用 06 ChanAnalyzer)
# ============================================================

def _ensure_chan_path() -> None:
    """把缠论模块加入 sys.path（优先 vendor/chan）。"""
    from lib.ext_bootstrap import ensure_on_path, resolve_vendor_or_course
    d = resolve_vendor_or_course(
        ("chan",),
        ("06_缠论精华量化",),
        "chan_analyzer.py",
    )
    ensure_on_path(d)


@register(
    name="chanlun",
    label="缠论买卖点",
    group="趋势跟随",
    description="日线缠论一/二/三买买入, 三卖卖出; 仅当最新一根日 K 当日出现信号时触发。",
    scenario="关注结构确认后的波段买点, 与技术指标/多因子对照时。",
    rules="ChanAnalyzer 识别 first_buy/second_buy/third_buy -> 买; third_sell -> 卖; 信号日期须等于最新日 K。",
    example="最新日 K 上出现三买, 给出买入侧信号; 随后出现三卖则给出卖出。",
)
def strat_chanlun(code: str, market, capital: float) -> dict:
    """复用 ChanAnalyzer（vendor/chan 或课程 06_），仅对「当日最新信号」发单。"""
    df = _safe_kline(market, code, "1d", 300)
    if df is None or len(df) < 120:
        return _hold("chanlun", f"日 K 不足 120 根 (现 {0 if df is None else len(df)})")

    try:
        _ensure_chan_path()
        from chan_analyzer import ChanAnalyzer
    except Exception as e:
        return _hold("chanlun", f"导入 ChanAnalyzer 失败: {type(e).__name__}: {e}")

    # ChanAnalyzer 需要 DatetimeIndex + open/high/low/close/volume
    work = df.copy()
    if not hasattr(work.index, "to_pydatetime"):
        if "date" in work.columns:
            work = work.set_index("date")
        work.index = __import__("pandas").to_datetime(work.index)

    try:
        analyzer = ChanAnalyzer(work).analyze()
    except Exception as e:
        return _hold("chanlun", f"缠论分析异常: {type(e).__name__}: {e}")

    if not analyzer.signals:
        return _hold("chanlun", "无买卖点信号")

    last_bar = work.index[-1]
    last_bar_day = str(last_bar)[:10]
    # 取落在最新交易日上的信号 (可能同一天多个, 取最后一个)
    day_sigs = []
    for sig in analyzer.signals:
        d = sig.get("date")
        day = str(d)[:10] if d is not None else ""
        if day == last_bar_day:
            day_sigs.append(sig)
    if not day_sigs:
        last = analyzer.signals[-1]
        return _hold(
            "chanlun",
            f"最新信号 {last.get('type')}@{str(last.get('date'))[:10]} 非今日 {last_bar_day}",
        )

    sig = day_sigs[-1]
    t = str(sig.get("type") or "")
    px = sig.get("price")
    reason = f"{t} @ {px} ({last_bar_day})"
    if t in ("first_buy", "second_buy", "third_buy"):
        return _signal("buy", "chanlun", reason)
    if t == "third_sell":
        return _signal("sell", "chanlun", reason)
    return _hold("chanlun", reason)


# ============================================================
# 策略 4: 龙头动量 (单只股票版)
# ============================================================
# dragon_picker 全市场版是全市场涨幅榜, 这里只看单只股自身:
#   - 当日涨幅 (从今日开盘到现在)
#   - 量比 (今日累计成交量 / 过去 5 日均量)
#   - 价位
# 满足 5 法则 (除"涨幅榜排名"无法在单股视角拿到) -> buy
# 当日跌幅 > 3% 或 大幅放量下跌 -> sell

@register(
    name="dragon_picker",
    label="龙头首板战法 (5 法则简化)",
    group="龙头动量",
    description="当日涨幅+量比+价位综合打分, 龙头分 >= 1.5 买入, 跌破日内回撤线卖出",
    scenario="关注当日强势、放量上攻的短线博弈（单票版龙头思路），愿意承担较大波动时。",
    rules="综合当日涨幅、量比、股价区间等打分；满足涨幅、量比、价位且总分够高时偏买；当日大跌或从高点明显回撤时偏卖。",
    example="当日涨幅已超过约 5%、量比显著放大、股价在约 30 元下方且综合分达标，可能触发买入侧信号。",
)
def strat_dragon(code: str, market, capital: float) -> dict:
    # 拉日线: 用昨日收盘 / 5 日均量做基准
    df_d = _safe_kline(market, code, "1d", 10)
    if df_d is None or len(df_d) < 6:
        return _hold("dragon_picker", "日 K 不足 6 根")
    prev_close = float(df_d["close"].iloc[-2]) if len(df_d) >= 2 else float(df_d["close"].iloc[-1])
    avg_vol_5d = float(df_d["volume"].iloc[-6:-1].mean()) if "volume" in df_d.columns else 0

    # 拉今日 5min K 线累加 (用 5min 而不是 1m, 因日线 market 常与 5m 分钟流配套)
    df_min = _safe_kline(market, code, "5m", 80)
    if df_min is None or len(df_min) == 0:
        return _hold("dragon_picker", "分钟 K 不足")

    # 取最新一根作为现价
    cur_price = float(df_min["close"].iloc[-1])
    today_str = str(df_min.index[-1])[:10]
    today_bars = df_min[df_min.index.astype(str).str[:10] == today_str]
    if len(today_bars) == 0:
        return _hold("dragon_picker", "今日分钟 K 缺失")
    today_high = float(today_bars["high"].max()) if "high" in today_bars.columns else cur_price
    today_vol = float(today_bars["volume"].sum()) if "volume" in today_bars.columns else 0

    # 当日涨幅 vs 昨收
    day_change = (cur_price / prev_close - 1.0) if prev_close > 0 else 0.0
    # 量比 (今日累计 vs 5 日均)
    vol_ratio = (today_vol / avg_vol_5d) if avg_vol_5d > 0 else 0.0

    # 出场: 从当日最高点回撤 > 3%, 或 当日整体跌幅 > 3% -> sell
    drawdown = (cur_price / today_high - 1.0) if today_high > 0 else 0.0
    if day_change < -0.03 or drawdown < -0.03:
        return _signal("sell", "dragon_picker",
                       f"日内 chg={day_change:+.2%} 回撤={drawdown:+.2%}")

    # 入场打分 (复用 calc_dragon_score 思路, 简化无市值)
    score = 0.0
    if day_change > 0.09:
        score += 0.5      # 接近涨停减分
    else:
        score += min(max(day_change, 0) * 10, 1.0)
    score += min(vol_ratio / 3, 1.5)
    if cur_price < 20:
        score += 0.5
    elif cur_price <= 30:
        score += 0.2

    reason = (f"日涨={day_change:+.2%} 量比={vol_ratio:.2f} "
              f"价={cur_price:.2f} 龙头分={score:.2f}")

    # 阈值: 涨幅 > 5% + 量比 > 2 + 价位 < 30 + 综合分 >= 1.5
    if (day_change > 0.05 and vol_ratio > 2.0 and cur_price < 30
            and score >= 1.5):
        return _signal("buy", "dragon_picker", reason)
    return _hold("dragon_picker", reason)


# ============================================================
# 策略 6: RSI 反转（经典超买超卖 + 穿越确认）
# ============================================================
# 经典 RSI: RSI<30 买、RSI>70 卖, period=14
# 这里在原逻辑基础上 + "穿越确认"（减少在强趋势里反复触发）:
#   - RSI 上穿 30  -> 买  (从超卖反弹, 比单纯 <30 更稳, 避免抄底抄到一半)
#   - RSI 下穿 70  -> 卖  (从超买回落, 减少在强趋势里被洗下车)
# 适用: 震荡 / 反转性强的票 (银行 / 公用 / 部分大盘蓝筹)

@register(
    name="rsi_reversal",
    label="RSI 反转 (14·30/70 穿越)",
    group="技术指标",
    description="日 K RSI(14) 上穿 30 买入, 下穿 70 卖出 (经典 RSI + 穿越确认)",
    scenario="震荡市 / 反转性强的票（银行、公用事业、部分大盘蓝筹），不追趋势, 抓「跌透了反弹」「涨过头回落」的边界。",
    rules="日 K 收盘算 RSI(14)。RSI 从 ≤30 上穿 30 -> 买（超卖反弹确认）；从 ≥70 下穿 70 -> 卖（超买回落确认）；其他时间观望。比裸 <30 / >70 信号更稳, 避免在强趋势里被反复打脸。",
    example="平安银行 RSI 跌到 28, 次日反弹收 32 -> 触发买入；后续涨到 RSI 73, 次日回落收 68 -> 触发卖出。",
)
def strat_rsi_reversal(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 60)
    if df is None or len(df) < 20:
        return _hold("rsi_reversal", "日 K 不足 20 根")
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - 100 / (1 + rs)
    if len(rsi) < 2 or math.isnan(rsi.iloc[-1]) or math.isnan(rsi.iloc[-2]):
        return _hold("rsi_reversal", "RSI 序列不足")
    prev = float(rsi.iloc[-2])
    curr = float(rsi.iloc[-1])
    if prev <= 30 < curr:
        return _signal("buy", "rsi_reversal",
                       f"RSI 上穿 30: {prev:.1f} -> {curr:.1f} (超卖反弹)")
    if prev >= 70 > curr:
        return _signal("sell", "rsi_reversal",
                       f"RSI 下穿 70: {prev:.1f} -> {curr:.1f} (超买回落)")
    return _hold("rsi_reversal", f"RSI={curr:.1f} 中性区")


# ============================================================
# 策略 7: 布林带均值回归 (BollingerBands)
# ============================================================
# 经典版: 触下轨买、触上轨卖, period=20, dev=2.0
# 完全保留原参数; 加 "中轨止盈" 防止持仓涨到中轨就吐回去
#   - 收盘 < 下轨   -> 买
#   - 收盘 > 上轨   -> 卖 (止盈)
#   - 收盘 < 中轨且前一日 >= 中轨 -> 卖 (跌破中轨止盈)

@register(
    name="boll_revert",
    label="布林带均值回归 (20·2σ)",
    group="技术指标",
    description="日 K 布林带(20, 2σ) 触下轨买、触上轨卖 + 跌破中轨止盈",
    scenario="波动有规律的票, 价格围绕中线均值上下震荡, 想做「触下轨吃货, 触上轨止盈」的均值回归操作。",
    rules="日 K 收盘价计算 MA20 与 ±2σ 三条线。收盘 < 下轨 -> 买; 收盘 > 上轨 -> 卖; 持仓时收盘从中轨上方跌破中轨 -> 卖 (止盈, 防回吐)。波动率扩张到强趋势时容易追涨杀跌, 需配合风控。",
    example="纳指 ETF 价格触下轨 1.65 触发买入; 反弹到 1.78 突破上轨 -> 卖出止盈。",
)
def strat_boll_revert(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 60)
    if df is None or len(df) < 25:
        return _hold("boll_revert", "日 K 不足 25 根")
    close = df["close"].astype(float)
    ma = close.rolling(20).mean()
    std = close.rolling(20).std(ddof=0)
    upper = ma + 2.0 * std
    lower = ma - 2.0 * std
    if math.isnan(ma.iloc[-1]):
        return _hold("boll_revert", "BOLL 序列不足")
    cur = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    mid_cur = float(ma.iloc[-1])
    mid_prev = float(ma.iloc[-2]) if not math.isnan(ma.iloc[-2]) else mid_cur
    up_cur = float(upper.iloc[-1])
    lo_cur = float(lower.iloc[-1])
    if cur < lo_cur:
        return _signal("buy", "boll_revert",
                       f"收盘 {cur:.2f} < 下轨 {lo_cur:.2f} (中轨 {mid_cur:.2f})")
    if cur > up_cur:
        return _signal("sell", "boll_revert",
                       f"收盘 {cur:.2f} > 上轨 {up_cur:.2f} (中轨 {mid_cur:.2f})")
    if prev >= mid_prev and cur < mid_cur:
        return _signal("sell", "boll_revert",
                       f"跌破中轨 {mid_cur:.2f} 止盈 (前 {prev:.2f}/中 {mid_prev:.2f})")
    return _hold("boll_revert",
                 f"在 [{lo_cur:.2f}, {up_cur:.2f}] 之间 中轨 {mid_cur:.2f}")


# ============================================================
# 策略 8: 乖离率均值回归 (BIAS)
# ============================================================
# 经典阈值: BIAS<-6% 买, BIAS>3% 卖, MA20
# 适用: 短期超跌反弹 / 涨多回调 -- 节奏型票 (大盘蓝筹反弹)

@register(
    name="bias_revert",
    label="乖离率均值回归 (BIAS·20)",
    group="技术指标",
    description="日 K 乖离率 < -6% 买入 (超跌), > 3% 卖出 (超涨)",
    scenario="跟随 20 日均线节奏运行的票, 想做「跌得离均线太远 -> 反弹补涨」「涨得离均线太远 -> 回调收口」的中期均值回归。",
    rules="BIAS = (收盘 - MA20) / MA20。BIAS < -6% -> 偏买 (超跌反弹); BIAS > 3% -> 偏卖 (涨过头, 注意不对称: 上涨节奏比下跌温和)。趋势单边市 (持续创新高 / 新低) 容易钝化。",
    example="贵州茅台日 K 收盘 1450, MA20 在 1545, BIAS = -6.15% -> 触发买入; 涨到 BIAS = 3.5% -> 卖出。",
)
def strat_bias_revert(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 60)
    if df is None or len(df) < 25:
        return _hold("bias_revert", "日 K 不足 25 根")
    close = df["close"].astype(float)
    ma20 = close.rolling(20).mean()
    if math.isnan(ma20.iloc[-1]) or ma20.iloc[-1] <= 0:
        return _hold("bias_revert", "MA20 不可用")
    cur = float(close.iloc[-1])
    bias = (cur - float(ma20.iloc[-1])) / float(ma20.iloc[-1])
    reason = f"BIAS={bias:+.2%} 收盘 {cur:.2f}/MA20 {float(ma20.iloc[-1]):.2f}"
    if bias < -0.06:
        return _signal("buy", "bias_revert", "超跌 " + reason)
    if bias > 0.03:
        return _signal("sell", "bias_revert", "超涨 " + reason)
    return _hold("bias_revert", reason)


# ============================================================
# 策略 9: 海龟唐奇安通道（ATR 仓位 + 金字塔 + 2N 止损）
# ============================================================
# 方向: 20 日突破入 / 10 日出; 仓位用 ATR 单元; 最多 4 单元、+0.5N 加仓;
# 状态写入 outputs/turtle_state.json

@register(
    name="turtle_donchian",
    label="海龟唐奇安通道 (ATR/金字塔/2N)",
    group="趋势跟随",
    description="日 K 突破 20 日高买入, 跌破 10 日低或 2N 止损卖出; ATR 单元仓位 + 最多 4 单元金字塔加仓",
    scenario="趋势性强的标的: 科技成长 / 宽基 ETF / 跨境 ETF。",
    rules="突破 20 日高开仓 1 单元; 每上涨 0.5N 加仓至多 4 单元; 跌破入场价-2N 或 10 日低出场。",
    example="突破 20 日高买入 1 单元; 涨 0.5ATR 再加 1 单元; 跌破 2N 止损全部卖出。",
)
def strat_turtle_donchian(code: str, market, capital: float) -> dict:
    import numpy as np
    from lib import turtle_state as ts

    df = _safe_kline(market, code, "1d", 80)
    if df is None or len(df) < 40:
        return _hold("turtle_donchian", "日 K 不足 40 根")

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    cur = float(close.iloc[-1])
    entry_high = float(high.iloc[-21:-1].max())
    exit_low = float(low.iloc[-11:-1].min())

    # ATR(20)
    prev_c = close.shift(1)
    tr = np.maximum(
        (high - low).abs(),
        np.maximum((high - prev_c).abs(), (low - prev_c).abs()),
    )
    atr = float(tr.rolling(20).mean().iloc[-1])
    if atr != atr or atr <= 0:
        return _hold("turtle_donchian", "ATR 不可用")

    risk_pct = 0.01
    unit_shares = int((float(capital) * risk_pct / atr) // 100) * 100
    if unit_shares < 100:
        unit_shares = 100

    st = ts.get_state(code)
    units = int(st.get("units") or 0)
    last_entry = float(st.get("last_entry") or 0)
    last_add = float(st.get("last_add") or last_entry)
    stop = float(st.get("stop") or 0)

    # ---- 已有仓: 2N / 通道出场 / 金字塔加仓 (状态只读, 成交后由 live_loop 更新) ----
    if units > 0:
        if stop > 0 and cur <= stop:
            return _signal(
                "sell", "turtle_donchian",
                f"2N止损 现价{cur:.2f}<=止损{stop:.2f} ATR={atr:.3f} 单元={units}",
            )
        if cur < exit_low:
            return _signal(
                "sell", "turtle_donchian",
                f"跌破10日低 {exit_low:.2f} 现价{cur:.2f} 单元={units}",
            )
        if units < 4 and cur >= last_add + 0.5 * atr:
            return _signal(
                "buy", "turtle_donchian",
                f"金字塔加仓第{units + 1}单元 +0.5N 建议{unit_shares}股 "
                f"ATR={atr:.3f} 止损->{cur - 2.0 * atr:.2f}",
            )
        return _hold(
            "turtle_donchian",
            f"持仓{units}单元 止损{stop:.2f} 通道[{exit_low:.2f},{entry_high:.2f}] "
            f"现价{cur:.2f} 建议单元股数{unit_shares}",
        )

    # ---- 空仓: 突破开仓 (不写状态, 成交后再记单元) ----
    if cur > entry_high:
        stop_px = cur - 2.0 * atr
        return _signal(
            "buy", "turtle_donchian",
            f"突破20日高{entry_high:.2f} 开1单元 建议{unit_shares}股 "
            f"ATR={atr:.3f} 2N止损{stop_px:.2f}",
        )
    return _hold(
        "turtle_donchian",
        f"空仓 通道[{exit_low:.2f},{entry_high:.2f}] 现价{cur:.2f} "
        f"ATR={atr:.3f} 建议单元{unit_shares}股",
    )


# ============================================================
# 策略 10: DQN 择时 (models/dqn_best.pth)
# ============================================================

_DQN_AGENT = None
_DQN_LOOKBACK = 5
_DQN_NORM = 252


def _build_dqn_obs(df) -> "np.ndarray":
    import numpy as np
    idx = len(df) - 1
    lb = _DQN_LOOKBACK
    hist_start = max(0, idx - _DQN_NORM)
    hist_close = df["close"].iloc[hist_start:idx].astype(float).values
    mu = float(hist_close.mean()) if len(hist_close) else 0.0
    sigma = float(hist_close.std()) if len(hist_close) else 1.0
    if sigma < 1e-8:
        sigma = 1.0
    obs = []
    for offset in range(lb, 0, -1):
        row = df.iloc[idx - offset]
        obs.extend([
            (float(row["open"]) - mu) / sigma,
            (float(row["high"]) - mu) / sigma,
            (float(row["low"]) - mu) / sigma,
            (float(row["close"]) - mu) / sigma,
        ])
    return np.array(obs, dtype=np.float32)


def _get_dqn_agent():
    """懒加载 DQNAgent + dqn_best.pth (进程内单例)."""
    global _DQN_AGENT
    if _DQN_AGENT is not None:
        return _DQN_AGENT
    import importlib.util
    from pathlib import Path

    from lib.ext_bootstrap import resolve_vendor_or_course
    from lib.paths import PROJECT_ROOT

    rl_dir = resolve_vendor_or_course(
        ("rl_dqn",),
        ("10_Agent与强化学习", "强化学习", "CASE-基于RL的交易策略"),
        "2-DQN择时策略.py",
    )
    model_path = PROJECT_ROOT / "models" / "dqn_best.pth"
    if not model_path.exists():
        raise FileNotFoundError(f"缺少模型 {model_path}, 请先运行 train_dqn_live.py")
    if rl_dir is None or not rl_dir.is_dir():
        raise FileNotFoundError("RL 目录不存在: vendor/rl_dqn")

    sp = str(rl_dir)
    if sp not in __import__("sys").path:
        __import__("sys").path.insert(0, sp)
    dqn_path = rl_dir / "2-DQN择时策略.py"
    spec = importlib.util.spec_from_file_location("rl_dqn_live", str(dqn_path))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    agent = mod.DQNAgent(state_dim=_DQN_LOOKBACK * 4, action_dim=3)
    agent.load(str(model_path))
    agent.epsilon = 0.0
    _DQN_AGENT = agent
    return agent


@register(
    name="dqn",
    label="DQN择时 (RL)",
    group="量化选股",
    description="加载 models/dqn_best.pth, 日线 OHLC Z-score 观测 -> hold/buy/sell",
    scenario="研究向: 把课内 DQN 权重挂到监控票上对照。",
    rules="观测=近5日OHLC相对252日收盘Z-score; 动作0持有/1买/2卖; 需先 train_dqn_live.py。",
    example="模型输出 action=1 给出买入侧信号。",
)
def strat_dqn(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", _DQN_NORM + _DQN_LOOKBACK + 20)
    need = _DQN_NORM + _DQN_LOOKBACK + 5
    if df is None or len(df) < need:
        return _hold("dqn", f"日 K 不足 {need} 根")
    try:
        agent = _get_dqn_agent()
        obs = _build_dqn_obs(df)
        action = int(agent.select_action(obs, training=False))
    except Exception as e:
        return _hold("dqn", f"DQN 推理失败: {type(e).__name__}: {e}")

    reason = f"action={action} (0hold/1buy/2sell)"
    if action == 1:
        return _signal("buy", "dqn", reason)
    if action == 2:
        return _signal("sell", "dqn", reason)
    return _hold("dqn", reason)


# ============================================================
# 策略 5: 经典网格 (无状态版)
# ============================================================
# 经典网格为有状态「格子位置变化触发」策略
# 这里改成"无状态阈值版": 每轮根据当前价在网格中的位置判断
#   - 价格落到下半区 (<= 第 2 格) -> buy (低吸)
#   - 价格冲到上半区 (>= 倒数第 2 格) -> sell (高抛)
# 配合 5min K 线的短期动能避免趋势单边市追高/抄底

@register(
    name="grid_classic",
    label="经典网格 (60 日区间 8 格)",
    group="震荡网格",
    description="过去 60 日 high/low 切 8 格, 价格在底部 2 格买, 顶部 2 格卖 (适合震荡市)",
    scenario="判断该股在一段时期内主要在箱体内震荡，想做「低位多吸、高位分批减」时。",
    rules="取约 60 个交易日最高价与最低价划成 8 格；现价落在最下两格偏买、最上两格偏卖；冲出区间上下沿另有止损/止盈类处理。",
    example="长期在箱体内运行时，价格回到区间下沿附近可能出现低吸类信号；单边趋势市则容易反复打脸。",
)
def strat_grid_classic(code: str, market, capital: float) -> dict:
    df_d = _safe_kline(market, code, "1d", 80)
    if df_d is None or len(df_d) < 60:
        return _hold("grid_classic", "日 K 不足 60 根")
    high_60 = float(df_d["high"].iloc[-60:].max())
    low_60 = float(df_d["low"].iloc[-60:].min())
    if high_60 <= low_60:
        return _hold("grid_classic", "网格区间无效")

    # 上下预留 2% 缓冲
    margin = (high_60 - low_60) * 0.02
    upper = high_60 + margin
    lower = low_60 - margin
    grid_size = (upper - lower) / 8

    df_min = _safe_kline(market, code, "5m", 5)
    if df_min is None or len(df_min) == 0:
        cur_price = float(df_d["close"].iloc[-1])
    else:
        cur_price = float(df_min["close"].iloc[-1])

    grid_idx = int((cur_price - lower) / grid_size) if grid_size > 0 else 4
    grid_idx = max(0, min(7, grid_idx))

    reason = (f"区间[{lower:.2f},{upper:.2f}] 当前={cur_price:.2f} "
              f"位于第 {grid_idx + 1}/8 格")

    # 出界处理: 跌破下界 (满仓套牢) 或 涨破上界 (踏空)
    if cur_price < lower:
        return _signal("sell", "grid_classic", f"跌破下界 {lower:.2f} -> 止损 " + reason)
    if cur_price > upper:
        return _signal("sell", "grid_classic", f"涨破上界 {upper:.2f} -> 止盈 " + reason)

    if grid_idx <= 1:        # 底部 2 格
        return _signal("buy", "grid_classic", "底部低吸 " + reason)
    if grid_idx >= 6:        # 顶部 2 格
        return _signal("sell", "grid_classic", "顶部高抛 " + reason)
    return _hold("grid_classic", reason)


# ============================================================
# Router: 按 (per_stock_map, default) 路由表派发
# ============================================================

class StrategyRouter:
    """
    策略路由器 -- 给 LiveTradingLoop 用的 evaluator

    用法:
        router = StrategyRouter(
            per_stock={"600519.SH": "macd_5min", "510300.SH": "grid_classic"},
            default="macd_5min",
        )
        loop = LiveTradingLoop(..., signal_evaluator=router)
    """

    def __init__(self, per_stock: Dict[str, str], default: str = "macd_5min"):
        self.per_stock = dict(per_stock or {})
        self.default = default

    def update(self, per_stock: Optional[Dict[str, str]] = None,
               default: Optional[str] = None):
        """热更新路由表 (前端保存配置后调)"""
        if per_stock is not None:
            self.per_stock = dict(per_stock)
        if default is not None:
            self.default = default

    def __call__(self, code: str, market, capital: float) -> dict:
        name = self.per_stock.get(code, self.default)
        meta = get_strategy(name)
        if meta is None:
            return _hold("unknown", f"策略 {name} 未注册")
        try:
            return meta.evaluator(code, market, capital)
        except Exception as e:
            return _hold(name, f"策略异常: {type(e).__name__}: {e}")
