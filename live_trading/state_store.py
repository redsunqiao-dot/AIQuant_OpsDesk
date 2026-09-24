# -*- coding: utf-8 -*-
# 23-CASE-A: 实盘 state 落盘存储
"""
StateStore -- 实盘运行的 state 持久化

为什么需要这个?
    - 盘中状态 (持仓 / 当日盈亏 / 信号历史) 需要跨进程共享
    - CEO 控制台 (CASE-B Gradio) 要读这个 state 渲染界面
    - 进程崩溃重启后, 用 state 恢复

设计:
    - 用 JSON 文件 + 文件锁存储 (轻量, 跨进程)
    - 每次写都是原子操作 (写入临时文件再 rename)
    - 读取支持快照, 不阻塞写
"""

from __future__ import annotations
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def read_text_shared(path: Path) -> str:
    """读文本。Windows 打开时带 FILE_SHARE_DELETE，避免挡住 os.replace。"""
    path = Path(path)
    if os.name != "nt":
        return path.read_text(encoding="utf-8")
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    invalid = ctypes.c_void_p(-1).value
    handle = kernel32.CreateFileW(
        str(path),
        0x80000000,       # GENERIC_READ
        0x1 | 0x2 | 0x4,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        None,
        3,                # OPEN_EXISTING
        0x80,             # FILE_ATTRIBUTE_NORMAL
        None,
    )
    if handle is None or handle == invalid:
        err = ctypes.get_last_error()
        raise OSError(err, f"读取失败: {path}")
    fd = msvcrt.open_osfhandle(handle, os.O_BINARY)
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        return f.read()


_WRITE_LOCK = threading.Lock()


def atomic_write_text(path: Path, text: str) -> None:
    """先写入独立临时文件，再替换目标。

    Windows 上页面正在读 live_state.json，或两路同时写同一个 .tmp 时，
    os.replace 会报 WinError 5。每次使用独立临时文件，同一进程内串行替换，
    目标短暂被占用时重试。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    tmp.write_text(text, encoding="utf-8")
    last_err: Optional[PermissionError] = None
    with _WRITE_LOCK:
        for i in range(8):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as e:
                last_err = e
                time.sleep(0.05 * (i + 1))
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    raise last_err


class StateStore:
    """JSON 文件版 state 存储"""

    def __init__(self, state_file: str = "outputs/live_state.json"):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        """读取当前 state"""
        if not self.state_file.exists():
            return self._default_state()
        try:
            return json.loads(read_text_shared(self.state_file))
        except Exception:
            return self._default_state()

    def save(self, state: dict):
        """原子写入 state"""
        # 加上更新时间戳
        state = {**state, "_updated_at": datetime.now().isoformat(timespec="seconds")}
        # 先写临时文件再替换，避免读到半截；Windows 上目标被占用时会重试
        atomic_write_text(
            self.state_file,
            json.dumps(state, ensure_ascii=False, indent=2),
        )

    def update(self, **kv):
        """局部更新"""
        s = self.load()
        s.update(kv)
        self.save(s)

    def append_event(self, event: dict, max_keep: int = 200):
        """往 events 列表追加一条 (滚动保留最新 N 条)"""
        s = self.load()
        events = s.get("events", [])
        events.append({**event, "ts": datetime.now().isoformat(timespec="seconds")})
        s["events"] = events[-max_keep:]
        self.save(s)

    def append_signal(self, signal: dict, max_keep: int = 100):
        """追加一条信号"""
        s = self.load()
        signals = s.get("signals", [])
        signals.append({**signal, "ts": datetime.now().isoformat(timespec="seconds")})
        s["signals"] = signals[-max_keep:]
        self.save(s)

    def append_order(self, order: dict, max_keep: int = 100):
        """追加一条订单 (含成功 / 失败 / 拒绝)"""
        s = self.load()
        orders = s.get("orders", [])
        orders.append({**order, "ts": datetime.now().isoformat(timespec="seconds")})
        s["orders"] = orders[-max_keep:]
        self.save(s)

    def update_pnl(self, pnl_record: dict):
        """每日盈亏曲线追加一个点"""
        s = self.load()
        pnl_history = s.get("pnl_history", [])
        pnl_history.append({**pnl_record, "ts": datetime.now().isoformat(timespec="seconds")})
        s["pnl_history"] = pnl_history[-500:]
        self.save(s)

    @staticmethod
    def _default_state() -> dict:
        return {
            "trading_status": "RUNNING",   # RUNNING / PAUSED / HALTED
            "capital":        1_000_000.0,
            "positions":      [],          # [{"code","name","volume","cost","cur_price","mv","pnl"}]
            "today_pnl":      0.0,
            "today_pnl_pct":  0.0,
            "events":         [],          # 时间事件流
            "signals":        [],          # 信号历史
            "orders":         [],          # 订单历史
            "pnl_history":    [],          # 盈亏曲线
            "control":        {            # CEO 控制台可写的字段
                "pause_buying":     False,
                "force_clear_all":  False,
                "max_daily_loss":   -0.02,
                "dry_run":          True,
            },
            "health": {
                "miniqmt_connected": False,
                "last_heartbeat":    None,
                "errors_24h":        0,
            },
        }
