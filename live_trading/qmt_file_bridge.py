# -*- coding: utf-8 -*-
"""
大 QMT 文件桥：作战台写 outbox，大 QMT 内置策略读文件后 passorder，再写 inbox 回执。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from lib.paths import OUTPUTS_DIR

_DEFAULT_BRIDGE = OUTPUTS_DIR / "qmt_bridge"


def bridge_root() -> Path:
    raw = (os.environ.get("QMT_BRIDGE_DIR") or "").strip().strip('"')
    root = Path(raw).expanduser() if raw else _DEFAULT_BRIDGE
    for name in ("outbox", "processing", "acked", "inbox"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def broker_mode() -> str:
    """file_bridge | xtquant | off。默认 file_bridge（券商大 QMT 无外部 API）。"""
    return (os.environ.get("BROKER_MODE") or "file_bridge").strip().lower()


def account_id() -> str:
    return (os.environ.get("QMT_ACCOUNT_ID")
            or os.environ.get("ACCOUNT_ID")
            or "").strip()


def _safe_token(text: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", str(text or ""))[:80]


def make_order_id(code: str, side: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"ord_{ts}_{_safe_token(code)}_{_safe_token(side)}"


def make_cancel_id(order_sys_id: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"cancel_{ts}_{_safe_token(order_sys_id)}"


def write_outbox_order(
    *,
    signal_id: str,
    code: str,
    side: str,
    quantity: int,
    price: float = 0.0,
    strategy: str = "",
    ttl_sec: int = 300,
    account: Optional[str] = None,
) -> dict[str, Any]:
    """授权后写入 outbox，返回委托字典。"""
    if side not in ("buy", "sell"):
        raise ValueError(f"side 无效: {side}")
    qty = int(quantity)
    if qty <= 0:
        raise ValueError(f"quantity 无效: {quantity}")
    acc = (account or account_id()).strip()
    if not acc:
        raise RuntimeError("未配置 QMT_ACCOUNT_ID / ACCOUNT_ID")

    root = bridge_root()
    oid = make_order_id(code, side)
    order = {
        "id": oid,
        "type": "order",
        "signal_id": signal_id,
        "code": str(code).strip(),
        "side": side,
        "quantity": qty,
        "price": float(price or 0),
        "price_type": "LATEST" if float(price or 0) <= 0 else "FIX",
        "account_id": acc,
        "strategy": strategy or "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "ttl_sec": int(ttl_sec),
    }
    path = root / "outbox" / f"{oid}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(order, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    order["path"] = str(path)
    return order


def write_outbox_cancel(
    *,
    order_sys_id: str,
    code: str = "",
    account: Optional[str] = None,
    ttl_sec: int = 120,
) -> dict[str, Any]:
    """写入撤单请求到 outbox，由大 QMT 策略 cancel()。"""
    sys_id = str(order_sys_id or "").strip()
    if not sys_id:
        raise ValueError("order_sys_id 为空")
    acc = (account or account_id()).strip()
    if not acc:
        raise RuntimeError("未配置 QMT_ACCOUNT_ID / ACCOUNT_ID")

    root = bridge_root()
    cid = make_cancel_id(sys_id)
    req = {
        "id": cid,
        "type": "cancel",
        "order_sys_id": sys_id,
        "code": str(code or "").strip(),
        "account_id": acc,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "ttl_sec": int(ttl_sec),
    }
    path = root / "outbox" / f"{cid}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    req["path"] = str(path)
    return req


def list_inbox_acks(limit: int = 200) -> list[dict[str, Any]]:
    """读取 inbox 委托回执，按修改时间倒序（跳过账户快照）。"""
    inbox = bridge_root() / "inbox"
    files = sorted(inbox.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for fp in files:
        if fp.stem == "account_snapshot":
            continue
        if len(out) >= limit:
            break
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if data.get("type") == "account_snapshot":
                    continue
                data.setdefault("id", fp.stem)
                out.append(data)
        except Exception:
            continue
    return out


# 大 QMT 委托状态（与 xtquant 编号不同：56=已成，55=部成）
_STATUS_MAP_QMT = {
    48: "未报", 49: "待报", 50: "已报", 51: "已报待撤", 52: "部成待撤",
    53: "部撤", 54: "已撤", 55: "部成", 56: "已成", 57: "废单",
}
_CANCELABLE_QMT = {48, 49, 50, 55}


def _normalize_bridge_orders(orders: list[Any]) -> list[dict[str, Any]]:
    """按大 QMT 状态码规范化；全成按量纠正为已成。"""
    out: list[dict[str, Any]] = []
    for raw in orders or []:
        if not isinstance(raw, dict):
            continue
        o = dict(raw)
        try:
            ov = int(o.get("order_volume") or 0)
            tv = int(o.get("traded_volume") or 0)
            st = int(o.get("order_status") or 0)
        except (TypeError, ValueError):
            out.append(o)
            continue
        if ov > 0 and tv >= ov:
            st = 56  # 大 QMT: 已成
        o["order_status"] = st
        o["status_text"] = _STATUS_MAP_QMT.get(st, o.get("status_text") or f"未知({st})")
        o["cancelable"] = st in _CANCELABLE_QMT
        out.append(o)
    return out


def read_account_snapshot(max_age_sec: float = 60.0) -> dict[str, Any]:
    """读大 QMT 策略写入的账户快照。

    返回:
      {
        "ok": bool,
        "connected": bool,
        "error": str|None,
        "age_sec": float,
        "source": "file_bridge",
        "account_id": str,
        "ts": str,
        "asset": {...},
        "positions": [...],
        "orders": [...],
      }
    """
    path = bridge_root() / "inbox" / "account_snapshot.json"
    if not path.exists():
        return {
            "ok": False,
            "connected": False,
            "error": "尚无账户快照。请确认大 QMT 文件桥策略已运行",
            "age_sec": -1.0,
            "source": "file_bridge",
            "account_id": account_id(),
            "ts": "",
            "asset": {},
            "positions": [],
            "orders": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("快照格式不是 JSON 对象")
        mtime = path.stat().st_mtime
        age = max(0.0, datetime.now().timestamp() - mtime)
        stale = age > float(max_age_sec)
        err = None
        if stale:
            err = f"快照过期 {age:.0f}s（阈值 {max_age_sec:.0f}s）。请确认大 QMT 策略仍在运行"
        return {
            "ok": not stale,
            "connected": not stale,
            "error": err,
            "age_sec": round(age, 2),
            "source": "file_bridge",
            "account_id": str(data.get("account_id") or account_id()),
            "ts": str(data.get("ts") or ""),
            "asset": data.get("asset") or {},
            "positions": list(data.get("positions") or []),
            "orders": _normalize_bridge_orders(list(data.get("orders") or [])),
        }
    except Exception as e:
        return {
            "ok": False,
            "connected": False,
            "error": f"{type(e).__name__}: {e}",
            "age_sec": -1.0,
            "source": "file_bridge",
            "account_id": account_id(),
            "ts": "",
            "asset": {},
            "positions": [],
            "orders": [],
        }


def apply_inbox_to_approvals(approvals: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """把 inbox 回执合并进 approvals。返回 (新 approvals, 更新条数)。

    当回执变为 submitted/filled 时, 顺带触发 Kris 仓位登记 + 海龟状态写入
    (仅首次, 用 order.post_fill_done 防重复)。
    """
    if not approvals:
        return approvals, 0
    by_order_id: dict[str, str] = {}
    for sid, rec in approvals.items():
        if not isinstance(rec, dict):
            continue
        order = rec.get("order") or {}
        oid = str(order.get("order_id") or order.get("id") or "")
        if oid and rec.get("status") == "queued":
            by_order_id[oid] = sid

    if not by_order_id:
        return approvals, 0

    changed = 0
    for ack in list_inbox_acks():
        oid = str(ack.get("id") or "")
        sid = by_order_id.get(oid)
        if not sid:
            continue
        rec = approvals.get(sid) or {}
        if rec.get("status") != "queued":
            continue
        st = str(ack.get("status") or "").lower()
        if st in ("submitted", "filled", "ok", "success"):
            new_status = "approved"
            err = None
        elif st in ("rejected", "expired", "error", "fail", "failed"):
            new_status = "rejected"
            err = str(ack.get("message") or ack.get("error") or st)
        else:
            continue
        order = dict(rec.get("order") or {})
        order["ack"] = ack
        if ack.get("order_sys_id"):
            order["order_sys_id"] = ack.get("order_sys_id")
        # 券商已接受委托: 写 Kris / 海龟状态 (拒单路径不会走到这里)
        if new_status == "approved" and not order.get("post_fill_done"):
            try:
                from lib.post_fill import after_real_order_accepted
                after_real_order_accepted(
                    code=str(order.get("code") or ""),
                    side=str(order.get("side") or ""),
                    price=float(order.get("price") or 0),
                    strategy=str(order.get("strategy") or ""),
                    quantity=int(order.get("quantity") or 100),
                )
                order["post_fill_done"] = True
            except Exception as e:
                print(f"[file_bridge] post_fill 失败: {type(e).__name__}: {e}", flush=True)
        approvals[sid] = {
            **rec,
            "status": new_status,
            "processed_ts": datetime.now().isoformat(timespec="seconds"),
            "order": order,
            "error": err,
        }
        changed += 1
    return approvals, changed
