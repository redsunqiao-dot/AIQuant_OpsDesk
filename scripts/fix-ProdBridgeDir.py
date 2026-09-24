# -*- coding: utf-8 -*-
"""发布后把 PROD 大 QMT 策略的 BRIDGE_DIR 改写为正式树绝对路径。"""
from pathlib import Path
import re
import sys


def main() -> int:
    if len(sys.argv) >= 3:
        strat = Path(sys.argv[1])
        bridge = sys.argv[2].replace("\\", "/")
    else:
        prod = Path(__file__).resolve().parents[1]
        # 若从 DEV/scripts 调用，目标应为同级 PROD
        parent = prod.parent
        if prod.name.endswith("_DEV"):
            prod = parent / "AIQuant_OpsDesk_PROD"
        strat = prod / "live_trading" / "big_qmt_file_bridge_strategy.py"
        bridge = (prod / "outputs" / "qmt_bridge").as_posix()

    raw = strat.read_bytes()
    enc = "utf-8"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        enc = "gbk"
    text = raw.decode(enc)
    new = re.sub(
        r'BRIDGE_DIR\s*=\s*r"[^"]+"',
        f'BRIDGE_DIR = r"{bridge}"',
        text,
        count=1,
    )
    if new == text:
        # 已是目标路径则视为成功
        if bridge in text and "BRIDGE_DIR" in text:
            print("BRIDGE_DIR 已是目标路径:", bridge)
            return 0
        print("BRIDGE_DIR 未匹配到:", strat)
        return 1
    strat.write_bytes(new.encode(enc))
    print("BRIDGE_DIR ->", bridge, "enc=", enc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
