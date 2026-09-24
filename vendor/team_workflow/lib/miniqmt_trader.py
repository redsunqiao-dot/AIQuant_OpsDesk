# -*- coding: utf-8 -*-
"""
MiniQMT / xtquant 交易封装已停用。

本仓库行情与财务改用公开源（baostock/akshare）与 MySQL。
实盘下单需另行对接其他券商 API。本文件仅保留占位，避免误 import 崩溃。
"""

from __future__ import annotations


class MiniQMTTrader:
    """占位：调用任何交易方法都会报错。"""

    def __init__(self, *args, **kwargs):
        self.connected = False

    def connect(self, *args, **kwargs):
        raise RuntimeError("MiniQMT 已停用，请改用公开源采数；交易需另接券商 API")

    def buy(self, *args, **kwargs):
        return self.connect()

    def sell(self, *args, **kwargs):
        return self.connect()

    def disconnect(self):
        self.connected = False


def connect_trader(*args, **kwargs):
    raise RuntimeError("MiniQMT 已停用，请改用公开源采数；交易需另接券商 API")


def place_order(*args, **kwargs):
    raise RuntimeError("MiniQMT 已停用，请改用公开源采数；交易需另接券商 API")
