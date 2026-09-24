# 内嵌依赖（vendor）

随 OpsDesk 源码包分发，迁机不必再依赖课程仓外置目录。

| 目录 | 说明 |
|------|------|
| `charles/` | 投研对话 Charles（nanobot） |
| `market_data_prep/` | 板块/日 K 日更 |
| `db_env/` | MySQL `QUANT_TRADE_*` 样例 |
| `market/` | 公开行情 `market_data_source.py` |
| `shared/` | 统一行情入口 `public_market.py` |
| `chan/` | 缠论 `chan_analyzer.py` |
| `quantstats/` | QuantStats 报告引擎 |
| `rl_dqn/` | DQN 择时环境与 Agent |
| `radar/` | 精选池 CSV 快照（可定期从 04_ 刷新） |
| `team_workflow/` | 晨会交易团队 LangGraph |

路径引导见 `lib/ext_bootstrap.py`（优先本树 vendor，兼容课程仓）。
