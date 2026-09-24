#coding:gbk
"""
大 QMT 文件桥消费策略（粘贴到大 QMT 策略编辑器）。

用法:
1. 登录大 QMT（模拟或实盘）
2. 新建 Python 策略，把本文件内容粘进去
3. 改 BRIDGE_DIR / ACCOUNT_ID 与作战台 .env 一致
4. 运行策略；保持客户端在线

作战台授权后会在 BRIDGE_DIR/outbox 写入 JSON；
本策略扫到后 passorder / cancel，并写回 BRIDGE_DIR/inbox。
同时按 SNAPSHOT_EVERY_SEC 把资金/持仓/当日委托写到
BRIDGE_DIR/inbox/account_snapshot.json，供作战台实盘页读取（含手动单）。
"""

# ====== 按本机修改 ======
BRIDGE_DIR = r"E:/AI_quant/12_投资晨会与作战系统/AI量化系统/AIQuant_OpsDesk_DEV/outputs/qmt_bridge"
ACCOUNT_ID = "66674429"   # 模拟端; 实盘改成你的资金账号
POLL_SECONDS = 2
SNAPSHOT_EVERY_SEC = 10   # 账户快照写入间隔（持仓/委托/资金）
DRY_RUN = False           # True=只回执不下单，联调用

_last_snapshot_ts = 0.0


def _ensure_dirs():
    import os
    for name in ("outbox", "processing", "acked", "inbox"):
        p = BRIDGE_DIR + "/" + name
        if not os.path.isdir(p):
            os.makedirs(p)


def _list_json(folder):
    import os
    root = BRIDGE_DIR + "/" + folder
    if not os.path.isdir(root):
        return []
    names = [n for n in os.listdir(root) if n.endswith(".json")]
    names.sort()
    return [root + "/" + n for n in names]


def _read_json(path):
    import json
    f = open(path, "r", encoding="utf-8")
    try:
        return json.load(f)
    finally:
        f.close()


def _write_json(path, obj):
    import json
    import os
    tmp = path + ".tmp"
    f = open(tmp, "w", encoding="utf-8")
    try:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    finally:
        f.close()
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)


def _move(src, dst_folder):
    import os
    import shutil
    name = os.path.basename(src)
    dst = BRIDGE_DIR + "/" + dst_folder + "/" + name
    if os.path.exists(dst):
        os.remove(dst)
    shutil.move(src, dst)
    return dst


def _expired(order):
    from datetime import datetime
    try:
        created = order.get("created_at") or ""
        ttl = int(order.get("ttl_sec") or 300)
        t0 = datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")
        return (datetime.now() - t0).total_seconds() > ttl
    except Exception:
        return False


def _ack(order_id, status, message="", order_sys_id=""):
    from datetime import datetime
    path = BRIDGE_DIR + "/inbox/" + order_id + ".json"
    _write_json(path, {
        "id": order_id,
        "status": status,
        "message": message or "",
        "order_sys_id": order_sys_id or "",
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    })


def _std_code(obj):
    """把 QMT 对象上的代码整理成 600000.SH 形式。"""
    code = str(getattr(obj, "m_strInstrumentID", "") or "")
    if not code:
        return ""
    if "." in code:
        return code
    exch = str(getattr(obj, "m_strExchangeID", "")
               or getattr(obj, "m_strMarket", "") or "").upper()
    if exch in ("SH", "1", "SSE", "SHSE"):
        return code + ".SH"
    if exch in ("SZ", "2", "SZSE"):
        return code + ".SZ"
    if code.startswith(("5", "6", "9")):
        return code + ".SH"
    return code + ".SZ"


def _get_detail(acc, what):
    """兼容 STOCK/stock 两种账号类型取委托/持仓/资金。"""
    rows = None
    for atype in ("STOCK", "stock"):
        try:
            rows = get_trade_detail_data(acc, atype, what)
            if rows is not None:
                return rows
        except Exception:
            rows = None
    return rows or []


def _f(obj, *names, default=0.0):
    for n in names:
        try:
            v = getattr(obj, n, None)
            if v is not None and v != "":
                return float(v)
        except Exception:
            pass
    return float(default)


def _i(obj, *names, default=0):
    for n in names:
        try:
            v = getattr(obj, n, None)
            if v is not None and v != "":
                return int(v)
        except Exception:
            pass
    return int(default)


def _s(obj, *names, default=""):
    for n in names:
        try:
            v = getattr(obj, n, None)
            if v is not None and str(v) != "":
                return str(v)
        except Exception:
            pass
    return str(default)


def _build_snapshot(acc):
    """组装作战台可读的账户快照（资金/持仓/当日委托）。"""
    from datetime import datetime

    asset = {"total_asset": 0.0, "cash": 0.0, "market_value": 0.0, "frozen_cash": 0.0}
    try:
        acc_rows = _get_detail(acc, "ACCOUNT")
        if acc_rows:
            a0 = acc_rows[0]
            cash = _f(a0, "m_dAvailable", "m_dCash")
            total = _f(a0, "m_dBalance", "m_dAsset", "m_dTotalAsset")
            mv = _f(a0, "m_dInstrumentValue", "m_dStockValue", "m_dMarketValue")
            frozen = _f(a0, "m_dFrozenMargin", "m_dFrozenCash", "m_dFetchBalance")
            if total <= 0 and (cash > 0 or mv > 0):
                total = cash + mv
            asset = {
                "total_asset": total,
                "cash": cash,
                "market_value": mv,
                "frozen_cash": frozen,
            }
    except Exception as e:
        print("[file_bridge] snapshot ACCOUNT fail:", type(e).__name__, e)

    positions = []
    try:
        for p in _get_detail(acc, "POSITION"):
            code = _std_code(p)
            vol = _i(p, "m_nVolume", "m_nPosition")
            if not code or vol == 0:
                continue
            positions.append({
                "stock_code": code,
                "volume": vol,
                "can_use_volume": _i(p, "m_nCanUseVolume", "m_nEnableAmount", default=vol),
                "open_price": _f(p, "m_dOpenPrice", "m_dAvgPrice", "m_dCostPrice"),
                "market_value": _f(p, "m_dMarketValue", "m_dInstrumentValue"),
            })
    except Exception as e:
        print("[file_bridge] snapshot POSITION fail:", type(e).__name__, e)

    orders = []
    try:
        for o in _get_detail(acc, "ORDER"):
            code = _std_code(o)
            if not code:
                continue
            # 方向: 常见 m_nOffsetFlag / m_nDirection; 兼容 23/24 与 48/49
            side_raw = _i(o, "m_nOffsetFlag", "m_nDirection", "m_nOrderType")
            if side_raw in (23, 48):
                side = "buy"
            elif side_raw in (24, 49):
                side = "sell"
            else:
                side = "buy" if side_raw in (0, 1) else ("sell" if side_raw == 2 else "type_%s" % side_raw)
            status_code = _i(o, "m_nOrderStatus")
            oid = _s(o, "m_strOrderSysID", "m_nOrderID") or _i(o, "m_nOrderID")
            order_volume = _i(o, "m_nVolumeTotalOriginal", "m_nOrderVolume")
            traded_volume = _i(o, "m_nVolumeTraded", "m_nDealVolume")
            # 大 QMT: 56=已成 55=部成；全成按量纠正
            if order_volume > 0 and traded_volume >= order_volume:
                status_code = 56
            pending = status_code in (48, 49, 50, 55)
            status_map = {
                48: "未报", 49: "待报", 50: "已报", 51: "已报待撤", 52: "部成待撤",
                53: "部撤", 54: "已撤", 55: "部成", 56: "已成", 57: "废单",
            }
            orders.append({
                "order_id": oid,
                "stock_code": code,
                "side": side,
                "order_volume": order_volume,
                "traded_volume": traded_volume,
                "price": _f(o, "m_dLimitPrice", "m_dPrice", "m_dTradeAmount"),
                "order_status": status_code,
                "status_text": status_map.get(status_code, "未知(%s)" % status_code),
                "cancelable": pending,
                "order_time": _s(o, "m_strInsertTime", "m_strOrderTime", "m_nOrderTime"),
                "strategy_name": _s(o, "m_strStrategyName"),
                "order_remark": _s(o, "m_strRemark"),
            })
    except Exception as e:
        print("[file_bridge] snapshot ORDER fail:", type(e).__name__, e)

    return {
        "type": "account_snapshot",
        "id": "account_snapshot",
        "account_id": acc,
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "asset": asset,
        "positions": positions,
        "orders": orders,
    }


def _maybe_write_snapshot(ContextInfo):
    """按间隔把账户快照写到 inbox/account_snapshot.json。"""
    global _last_snapshot_ts
    import time
    now = time.time()
    if _last_snapshot_ts and (now - _last_snapshot_ts) < float(SNAPSHOT_EVERY_SEC):
        return
    acc = ACCOUNT_ID
    try:
        # 优先用 ContextInfo 当前账号
        a = getattr(ContextInfo, "accountid", None) or getattr(ContextInfo, "account_id", None)
        if a:
            acc = str(a)
    except Exception:
        pass
    try:
        snap = _build_snapshot(acc)
        path = BRIDGE_DIR + "/inbox/account_snapshot.json"
        _write_json(path, snap)
        _last_snapshot_ts = now
        print("[file_bridge] snapshot ok cash=%s pos=%d orders=%d" % (
            snap.get("asset", {}).get("cash"),
            len(snap.get("positions") or []),
            len(snap.get("orders") or []),
        ))
    except Exception as e:
        print("[file_bridge] snapshot error:", type(e).__name__, e)


def _cancel_order(ContextInfo, req):
    """调用大 QMT cancel(orderSysID, account, STOCK, ContextInfo)。"""
    sys_id = str(req.get("order_sys_id") or req.get("order_id") or "")
    acc = str(req.get("account_id") or ACCOUNT_ID)
    if not sys_id:
        return {"ret": False, "detail": "缺少 order_sys_id"}

    if DRY_RUN:
        print("[file_bridge] DRY_RUN cancel", sys_id, acc)
        return {"ret": True, "detail": "DRY_RUN 未真实撤单"}

    # 可选：先问是否可撤
    try:
        ok = can_cancel_order(sys_id, acc, "STOCK")
        print("[file_bridge] can_cancel_order", sys_id, ok)
        if ok is False:
            # 再试小写
            try:
                ok = can_cancel_order(sys_id, acc, "stock")
            except Exception:
                pass
            if ok is False:
                return {"ret": False, "detail": "can_cancel_order=False"}
    except Exception as e:
        print("[file_bridge] can_cancel_order skip:", type(e).__name__, e)

    ret = None
    last_err = ""
    for atype in ("STOCK", "stock"):
        try:
            ret = cancel(sys_id, acc, atype, ContextInfo)
            print("[file_bridge] cancel", sys_id, atype, "ret=", ret)
            break
        except Exception as e:
            last_err = "%s:%s" % (type(e).__name__, e)
            print("[file_bridge] cancel fail", atype, last_err)
            ret = None

    if ret is None:
        return {"ret": False, "detail": "cancel 调用失败 %s" % last_err}
    return {"ret": bool(ret), "detail": "cancel ret=%s" % ret}


def _passorder(ContextInfo, order):
    """调用大 QMT passorder，并立刻查当日委托做核对。"""
    code = str(order.get("code") or "")
    side = str(order.get("side") or "")
    qty = int(order.get("quantity") or 0)
    price = float(order.get("price") or 0)
    acc = str(order.get("account_id") or ACCOUNT_ID)
    strategy = str(order.get("strategy") or "file_bridge")
    user_oid = str(order.get("id") or "")

    # opType: 23买 24卖; orderType: 1101 股票
    op_type = 23 if side == "buy" else 24
    # prType: 5最新价(price填0), 11限价
    if price > 0:
        pr_type = 11
        px = price
    else:
        pr_type = 5
        px = 0

    print("[file_bridge] passorder", op_type, acc, code, pr_type, px, qty, strategy, user_oid)
    # passorder(opType, orderType, accountid, orderCode, prType, price, volume,
    #           strategyName, quickTrade, userOrderId, ContextInfo)
    ret = passorder(op_type, 1101, acc, code, pr_type, px, qty,
                    strategy, 2, user_oid, ContextInfo)
    print("[file_bridge] passorder ret=", ret)

    # 柜台落单有延迟，稍等再查
    import time
    time.sleep(1.5)

    # 立刻查当日委托，方便确认客户端是否真有单
    detail = ""
    order_sys_id = ""
    try:
        rows = None
        for atype in ("STOCK", "stock"):
            try:
                rows = get_trade_detail_data(acc, atype, "ORDER")
                if rows is not None:
                    break
            except Exception:
                rows = None
        if rows is None:
            for atype in ("STOCK", "stock"):
                try:
                    rows = get_trade_detail_data(acc, atype, "ORDER", strategy)
                    if rows is not None:
                        break
                except Exception:
                    rows = None
        n = 0 if rows is None else len(rows)
        detail = "orders_today=%d ret=%s" % (n, ret)
        # 顺带查资金，确认账号绑对
        try:
            acc_rows = get_trade_detail_data(acc, "STOCK", "ACCOUNT")
            if not acc_rows:
                acc_rows = get_trade_detail_data(acc, "stock", "ACCOUNT")
            if acc_rows:
                a0 = acc_rows[0]
                cash = getattr(a0, "m_dAvailable", None)
                if cash is None:
                    cash = getattr(a0, "m_dBalance", "")
                detail = detail + "; cash=%s" % cash
        except Exception as e:
            detail = detail + "; account_query=%s" % type(e).__name__
        if rows:
            hits = []
            remark_hit = ""
            for o in rows:
                try:
                    c = str(getattr(o, "m_strInstrumentID", "") or "")
                    rem = str(getattr(o, "m_strRemark", "") or "")
                    sysid = str(getattr(o, "m_strOrderSysID", "") or getattr(o, "m_nOrderID", "") or "")
                    st = str(getattr(o, "m_nOrderStatus", "") or "")
                    vol = str(getattr(o, "m_nVolumeTotalOriginal", "") or "")
                    # 优先：备注精确对应本次 userOrderId（避免绑到同代码旧单）
                    if user_oid and rem and (user_oid == rem or user_oid in rem):
                        if not remark_hit and sysid:
                            remark_hit = sysid
                        hits.insert(0, "%s st=%s vol=%s sys=%s rem=%s" % (c, st, vol, sysid, rem))
                    elif code.startswith(c) or c in code:
                        hits.append("%s st=%s vol=%s sys=%s rem=%s" % (c, st, vol, sysid, rem))
                except Exception:
                    pass
            order_sys_id = remark_hit
            if not order_sys_id and hits:
                # 退化：取 hits 第一条里的 sys=
                try:
                    order_sys_id = hits[0].split("sys=")[1].split(" ")[0]
                except Exception:
                    order_sys_id = ""
            if hits:
                detail = detail + "; hit=" + " | ".join(hits[:3])
            else:
                o = rows[-1]
                c = str(getattr(o, "m_strInstrumentID", "") or "")
                rem = str(getattr(o, "m_strRemark", "") or "")
                sysid = str(getattr(o, "m_strOrderSysID", "") or "")
                detail = detail + "; last=%s rem=%s sys=%s" % (c, rem, sysid)
                if sysid and not order_sys_id:
                    order_sys_id = sysid
    except Exception as e:
        detail = "query_order_fail:%s" % type(e).__name__
        print("[file_bridge]", detail, e)

    return {
        "ret": ret,
        "detail": detail,
        "order_sys_id": order_sys_id,
    }


def init(ContextInfo):
    _ensure_dirs()
    try:
        ContextInfo.set_account(ACCOUNT_ID)
    except Exception as e:
        print("[file_bridge] set_account warn:", type(e).__name__, e)
    # run_time(funcName, period, startTime, market)
    period = "%dnSecond" % int(POLL_SECONDS)
    ContextInfo.run_time("drain", period, "2019-10-14 13:20:00", "SH")
    print("[file_bridge] init ok dir=%s account=%s dry_run=%s period=%s snapshot=%ss" % (
        BRIDGE_DIR, ACCOUNT_ID, DRY_RUN, period, SNAPSHOT_EVERY_SEC))


def drain(ContextInfo):
    import os
    import traceback
    global _last_snapshot_ts
    _ensure_dirs()
    for path in _list_json("outbox"):
        order = None
        order_id = ""
        try:
            order = _read_json(path)
            order_id = str(order.get("id") or os.path.basename(path).replace(".json", ""))
            # 账户快照文件名保护，防止误当委托
            if order_id == "account_snapshot" or order.get("type") == "account_snapshot":
                _move(path, "acked")
                continue
            # 已有回执则跳过并归档
            inbox_path = BRIDGE_DIR + "/inbox/" + order_id + ".json"
            if os.path.exists(inbox_path):
                _move(path, "acked")
                continue
            proc = _move(path, "processing")
            if _expired(order):
                _ack(order_id, "expired", "委托已过期")
                _move(proc, "acked")
                continue

            # ---- 撤单请求 ----
            if order.get("type") == "cancel" or str(order.get("side") or "") == "cancel":
                if DRY_RUN:
                    _ack(order_id, "submitted", "DRY_RUN 未真实撤单",
                         order_sys_id=str(order.get("order_sys_id") or ""))
                    _move(proc, "acked")
                    print("[file_bridge] dry_run cancel", order_id, order.get("order_sys_id"))
                    continue
                info = _cancel_order(ContextInfo, order)
                ok = bool(info.get("ret"))
                msg = str(info.get("detail") or "")
                _ack(order_id,
                     "submitted" if ok else "error",
                     msg,
                     order_sys_id=str(order.get("order_sys_id") or ""))
                _move(proc, "acked")
                print("[file_bridge] cancel done", order_id, order.get("order_sys_id"), msg)
                # 撤单后强制刷新快照
                _last_snapshot_ts = 0.0
                continue

            # ---- 普通下单 ----
            if DRY_RUN:
                _ack(order_id, "submitted", "DRY_RUN 未真实下单")
                _move(proc, "acked")
                print("[file_bridge] dry_run", order_id, order.get("side"), order.get("code"))
                continue
            info = _passorder(ContextInfo, order)
            msg = "passorder ok ret=%s; %s" % (info.get("ret"), info.get("detail"))
            _ack(order_id, "submitted", msg, order_sys_id=str(info.get("order_sys_id") or ""))
            _move(proc, "acked")
            print("[file_bridge] submitted", order_id, order.get("side"), order.get("code"), order.get("quantity"), msg)
        except Exception as e:
            msg = "%s: %s" % (type(e).__name__, e)
            print("[file_bridge] error", order_id, msg)
            print(traceback.format_exc())
            if order_id:
                _ack(order_id, "error", msg)
            try:
                if os.path.exists(path):
                    _move(path, "acked")
                proc2 = BRIDGE_DIR + "/processing/" + os.path.basename(path)
                if os.path.exists(proc2):
                    _move(proc2, "acked")
            except Exception:
                pass
    # 每轮末尾按间隔写账户快照（含手动单）
    _maybe_write_snapshot(ContextInfo)


def handlebar(ContextInfo):
    pass


def order_callback(ContextInfo, orderInfo):
    try:
        print("[file_bridge][order_callback]",
              getattr(orderInfo, "m_strInstrumentID", ""),
              getattr(orderInfo, "m_nOrderStatus", ""),
              getattr(orderInfo, "m_nVolumeTotalOriginal", ""),
              getattr(orderInfo, "m_strRemark", ""))
    except Exception as e:
        print("[file_bridge][order_callback] err", type(e).__name__, e)
