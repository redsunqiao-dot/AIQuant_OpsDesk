# -*- coding: utf-8 -*-
# 海龟金字塔状态持久化 -- turtle_state.json
"""per-code: units / last_entry / stop / atr / last_add"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict

from lib.paths import OUTPUTS_DIR

TURTLE_STATE_FILE = OUTPUTS_DIR / "turtle_state.json"
_lock = threading.Lock()


def load_all() -> Dict[str, dict]:
    if not TURTLE_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(TURTLE_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_all(data: Dict[str, dict]) -> None:
    TURTLE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TURTLE_STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def get_state(code: str) -> dict:
    with _lock:
        all_st = load_all()
        return dict(all_st.get(code) or {})


def set_state(code: str, st: Dict[str, Any]) -> None:
    with _lock:
        all_st = load_all()
        if st is None or int(st.get("units") or 0) <= 0:
            all_st.pop(code, None)
        else:
            all_st[code] = st
        save_all(all_st)


def clear_state(code: str) -> None:
    set_state(code, {})


def apply_fill(code: str, side: str, price: float, atr: float,
               unit_shares: int = 100) -> dict:
    """成交后更新海龟状态: buy 加单元, sell 清空.

    策略评估阶段只发信号、不写状态, 避免拒单留下脏仓位记录.
    """
    code = str(code or "").strip()
    side = str(side or "").strip().lower()
    price = float(price or 0)
    atr = float(atr or 0)
    if side == "sell":
        clear_state(code)
        return {"units": 0}
    if side != "buy" or price <= 0:
        return get_state(code)

    st = get_state(code)
    units = int(st.get("units") or 0) + 1
    if units > 4:
        units = 4
    atr_use = atr if atr > 0 else float(st.get("atr") or 0)
    stop = (price - 2.0 * atr_use) if atr_use > 0 else float(st.get("stop") or 0)
    last_entry = float(st.get("last_entry") or price)
    if int(st.get("units") or 0) <= 0:
        last_entry = price
    new_st = {
        "units": units,
        "last_entry": last_entry,
        "last_add": price,
        "stop": stop,
        "atr": atr_use,
        "unit_shares": int(unit_shares or st.get("unit_shares") or 100),
    }
    set_state(code, new_st)
    return new_st
