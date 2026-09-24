# -*- coding: utf-8 -*-
# 训练 DQN 择时权重, 输出 models/dqn_best.pth 供 live strat_dqn 使用
"""复用 vendor/rl_dqn 的 DQN + StockTradingEnv; 用 public_market 日线训练茅台."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

from lib.ext_bootstrap import ensure_public_market, resolve_vendor_or_course

ensure_public_market()

RL_DIR = resolve_vendor_or_course(
    ("rl_dqn",),
    ("10_Agent与强化学习", "强化学习", "CASE-基于RL的交易策略"),
    "2-DQN择时策略.py",
)
if RL_DIR is None:
    raise FileNotFoundError("RL 目录不存在: vendor/rl_dqn")
sys.path.insert(0, str(RL_DIR))

MODELS_DIR = ROOT / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)
OUT_PATH = MODELS_DIR / "dqn_best.pth"

EPISODES = int(os.environ.get("DQN_EPISODES", "40"))
LOOKBACK = 5
NORM_WINDOW = 252
CODE = "600519.SH"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _load_daily_df(code: str) -> pd.DataFrame:
    from public_market import load_daily_kline, to_std_code
    df = load_daily_kline(to_std_code(code), count=800)
    if df is None or len(df) < NORM_WINDOW + LOOKBACK + 50:
        raise RuntimeError(f"日 K 不足: {code} n={0 if df is None else len(df)}")
    work = df.copy()
    if "date" in work.columns and not isinstance(work.index, pd.DatetimeIndex):
        work = work.set_index("date")
    work.index = pd.to_datetime(work.index)
    need = ["open", "high", "low", "close"]
    for c in need:
        if c not in work.columns:
            raise RuntimeError(f"缺列 {c}")
    return work[need].astype(float).dropna()


def main():
    env_mod = _load_module("rl_env", RL_DIR / "1-搭建RL交易环境.py")
    dqn_mod = _load_module("rl_dqn", RL_DIR / "2-DQN择时策略.py")
    StockTradingEnv = env_mod.StockTradingEnv
    DQNAgent = dqn_mod.DQNAgent

    print(f"加载 {CODE} 日线...")
    df = _load_daily_df(CODE)
    print(f"  bars={len(df)} {df.index[0].date()} ~ {df.index[-1].date()}")

    env = StockTradingEnv(df, lookback=LOOKBACK, norm_window=NORM_WINDOW)
    state_dim = LOOKBACK * 4
    agent = DQNAgent(state_dim=state_dim, action_dim=3)

    best_reward = -1e18
    print(f"开始训练 episodes={EPISODES} ...")
    for ep in range(1, EPISODES + 1):
        state, _ = env.reset()
        done = False
        total_r = 0.0
        while not done:
            action = agent.select_action(state, training=True)
            next_state, reward, terminated, truncated, _info = env.step(action)
            done = bool(terminated or truncated)
            agent.store_transition(state, action, reward, next_state, done)
            agent.train()
            state = next_state
            total_r += float(reward)
        agent.decay_epsilon()
        if total_r > best_reward:
            best_reward = total_r
            agent.save(str(OUT_PATH))
        if ep % 10 == 0 or ep == 1:
            print(f"  ep {ep}/{EPISODES} reward={total_r:.4f} best={best_reward:.4f} "
                  f"eps={agent.epsilon:.3f}")

    if not OUT_PATH.exists():
        agent.save(str(OUT_PATH))
    print(f"完成: {OUT_PATH} best_reward={best_reward:.4f}")


if __name__ == "__main__":
    main()
