# -*- coding: utf-8 -*-
"""实盘操作模块

把「准备实盘」到「真的把委托单发出去」之间所有该做的事放在这里：

  * preflight()      实盘就绪检查 —— 开实盘前逐条打勾，缺关键项不让开
  * ticket_*         待确认委托单 —— 策略出信号后先生成单子，你看过再发送
  * send_ticket()    真正把委托发到券商（发送前会重新跑一遍风控）
  * stop_monitor()   持仓止损监控 —— 每只离止损多远，超限提醒
  * stop_all()       一键全部止损
  * daily_report()   当日交易报告
  * sync_orders()    从券商同步委托状态（已报/部成/已成/已撤/废单）
"""
from __future__ import annotations

import datetime as dt
import json

from . import config
from . import datasource as ds
from . import factors as fac
from . import store
from .brokers import manager
from .engine import risk


# ---------------------------------------------------------------------------
# 实盘就绪检查
# ---------------------------------------------------------------------------
def _strategy_has_stop(s: dict) -> bool:
    """这个策略自己带止损吗"""
    if s.get("stype") == "pullback_buy":
        return True                      # 回调买入法天生带止损
    p = s.get("params") or {}
    for k in ("stop_loss_pct", "max_risk", "atr_stop"):
        try:
            if float(p.get(k) or 0) > 0:
                return True
        except Exception:
            continue
    return False


def preflight() -> dict:
    b = manager.active()
    checks = []

    def add(key, name, ok, critical, detail=""):
        checks.append({"key": key, "name": name, "ok": bool(ok),
                       "critical": bool(critical), "detail": detail})

    # 1 通道
    add("broker", "交易通道已连接", b.connected, True,
        "%s%s" % (b.display, "" if b.connected else "（未连接，先去实盘交易页点连接）"))

    # 2 账户可读
    bal, berr = {}, ""
    try:
        bal = b.balance()
    except Exception as e:  # noqa: BLE001
        berr = str(e)[:120]
    add("account", "能读到账户资金", bool(bal) and not berr, True,
        ("总资产 %.2f 元，可用 %.2f 元" % (float(bal.get("total_asset") or 0),
                                        float(bal.get("available_cash") or bal.get("cash") or 0)))
        if bal else ("读取失败: %s" % berr))

    # 3 风控开启
    add("risk", "风控已开启", config.get("risk.enabled", True), True,
        "单只上限 %.0f%%｜单笔上限 %.0f 元｜日亏熔断 %.0f%%"
        % (float(config.get("risk.max_single_position_pct", 20)),
           float(config.get("risk.max_order_amount", 50000)),
           float(config.get("risk.daily_loss_limit_pct", 5)))
        if config.get("risk.enabled", True) else "风控被关掉了，非常危险，建议打开")

    # 4 有止损
    global_stop = float(config.get("risk.stop_loss_pct", 0) or 0)
    strategies = store.list_strategies()
    enabled = [s for s in strategies if s["enabled"]]
    stop_strats = [s["name"] for s in enabled if _strategy_has_stop(s)]
    add("stop", "有止损保护", global_stop > 0 or bool(stop_strats), True,
        ("全局止损 %.1f%%" % global_stop) if global_stop > 0
        else ("策略自带止损：%s" % "、".join(stop_strats) if stop_strats
              else "既没设全局止损，也没有带止损的策略 —— 亏起来没有底"))

    # 5 资金
    cash = float(bal.get("available_cash") or bal.get("cash") or 0)
    add("cash", "账户有可用资金", cash > 0, True,
        "可用 %.2f 元" % cash if cash > 0 else "可用资金为 0，发不出委托")

    # 6 启用策略
    add("strategy", "至少启用了一个策略", bool(enabled), False,
        "已启用 %d 个：%s" % (len(enabled), "、".join(s["name"] for s in enabled[:3]))
        if enabled else "还没有启用任何策略（也可以只用手动下单）")

    # 7 回测过
    bt = store.list_backtests(1)
    add("backtest", "做过回测", bool(bt), False,
        "最近一次：%s" % bt[0]["name"] if bt else "还没回测过，建议先看看策略历史表现")

    # 8 单笔上限别太离谱
    mo = float(config.get("risk.max_order_amount", 50000) or 0)
    total = float(bal.get("total_asset") or 0)
    sane = (total <= 0) or (mo <= total * 0.5)
    add("limits", "单笔上限设置合理", sane, False,
        "单笔上限 %.0f 元 / 总资产 %.0f 元（%.0f%%）"
        % (mo, total, mo / total * 100 if total else 0)
        + ("" if sane else " —— 单笔占用超过一半资产，太激进了"))

    # 9 委托方式
    mode = config.get("trading.order_mode", "confirm")
    add("ordermode", "下单方式", True, False,
        "待确认（推荐）：策略出信号后先生成单子，你点发送才真发"
        if mode == "confirm" else "全自动：策略一出信号就直接发给券商，不问你")

    critical_bad = [c for c in checks if c["critical"] and not c["ok"]]
    warn_bad = [c for c in checks if not c["critical"] and not c["ok"]]
    return {
        "ok": True,
        "ready": not critical_bad,
        "checks": checks,
        "critical_failed": [c["name"] for c in critical_bad],
        "warnings": [c["name"] for c in warn_bad],
        "broker": b.name, "is_live": b.is_live,
        "live_enabled": bool(config.get("live.enabled")),
        "price_note": "未成交前价格都可能变，发送时用的是当时的实时价",
        "checked_at": store.now_str(),
    }


# ---------------------------------------------------------------------------
# 待确认委托单
# ---------------------------------------------------------------------------
def make_ticket(source: str, symbol: str, side: str, qty: int, price=None,
                strategy_id=None, strategy_name: str = "", reason: str = "",
                risk_text: str = "", order_type: str = "limit") -> int:
    sym = ds.normalize(symbol)
    name = ""
    px = price
    try:
        q = ds.quote(sym)
        name = q.get("name") or ""
        if not px:
            px = q.get("price")
    except Exception:
        pass
    mins = int(config.get("trading.ticket_expire_min", 30) or 30)
    exp = (dt.datetime.now() + dt.timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M:%S")
    tid = store.add_ticket({
        "source": source, "strategy_id": strategy_id, "strategy_name": strategy_name,
        "symbol": sym, "name": name, "side": side, "qty": int(qty),
        "price": float(px or 0), "order_type": order_type,
        "reason": reason, "risk": risk_text, "expires_at": exp,
    })
    store.log("INFO", "ticket", "生成待确认委托单 #%d %s %s %d股 @%.3f（%s）"
              % (tid, sym, "买入" if side == "buy" else "卖出", qty, float(px or 0), source))
    return tid


def list_tickets(status: str | None = None) -> dict:
    try:
        store.expire_tickets(int(config.get("trading.ticket_expire_min", 30) or 0))
    except Exception:
        pass
    rows = store.list_tickets(status, 200)
    out = []
    for t in rows:
        d = dict(t)
        try:
            d["risk_parsed"] = json.loads(t.get("risk") or "{}")
        except Exception:
            d["risk_parsed"] = {}
        out.append(d)
    pending = [t for t in out if t["status"] == "待确认"]
    return {"tickets": out, "pending": len(pending)}


def send_ticket(tid: int, force: bool = False) -> dict:
    """把一张待确认委托单真正发出去（发送前重新跑风控）"""
    t = store.get_ticket(tid)
    if not t:
        return {"ok": False, "message": "找不到这张委托单 #%s" % tid}
    if t["status"] != "待确认":
        return {"ok": False, "message": "这张单子状态是「%s」，不能重复发送" % t["status"]}

    b = manager.active()
    if not b.connected and b.name != "paper":
        return {"ok": False, "message": "通道 %s 还没连接" % b.display}
    if b.is_live and not config.get("live.enabled", False):
        return {"ok": False, "message": "当前是实盘通道，但实盘总开关没打开。请先到实盘交易页确认启用实盘。"}

    sym = t["symbol"]
    side = t["side"]
    qty = int(t["qty"])
    try:
        q = ds.quote(sym)
        live_px = q.get("price")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": "取实时行情失败: %s" % str(e)[:120]}
    if not live_px:
        return {"ok": False, "message": "%s 拿不到实时价格，可能停牌了" % sym}

    px = float(t["price"] or live_px)
    order_type = t["order_type"] or "limit"
    # 价格相对偏离提示
    drift = abs(live_px / px - 1) * 100 if px else 0

    try:
        bal = b.balance()
        pos = b.positions()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": "读取账户失败: %s" % str(e)[:120]}

    from .engine import engine
    if side == "buy":
        ok, why = risk.check_buy({"symbol": sym, "price": px, "qty": qty, "balance": bal,
                                  "positions": pos, "orders_today": engine.orders_today,
                                  "halted": engine.halted_reason})
    else:
        ok, why = risk.check_sell({"symbol": sym, "qty": qty,
                                   "orders_today": engine.orders_today})
    if not ok and not force:
        store.update_ticket(tid, status="被风控拦下", result=why)
        store.log("WARN", "ticket", "委托单 #%d 被风控拦下: %s" % (tid, why))
        return {"ok": False, "message": "被风控拦下：%s" % why, "blocked": True}

    try:
        if side == "buy":
            r = b.buy(sym, price=(px if order_type == "limit" else None), qty=qty,
                      order_type=order_type, reason="[委托单#%d] %s" % (tid, t["reason"]),
                      strategy_id=t["strategy_id"])
        else:
            r = b.sell(sym, price=(px if order_type == "limit" else None), qty=qty,
                       order_type=order_type, reason="[委托单#%d] %s" % (tid, t["reason"]),
                       strategy_id=t["strategy_id"])
    except Exception as e:  # noqa: BLE001
        msg = "发送异常: %s" % str(e)[:180]
        store.update_ticket(tid, status="发送失败", result=msg)
        return {"ok": False, "message": msg}

    ok_sent = bool(r.get("ok"))
    store.update_ticket(
        tid,
        status="已发送" if ok_sent else "发送失败",
        sent_at=store.now_str(),
        order_id=r.get("order_id"),
        result=(r.get("message") or "")[:300],
    )
    if ok_sent:
        engine.orders_today += 1
        engine._mark_cooldown(sym)
    store.log("INFO" if ok_sent else "ERROR", "ticket",
              "委托单 #%d %s %s %d股：%s" % (tid, "已发送" if ok_sent else "发送失败",
                                            sym, qty, r.get("message")))
    return dict(r, ticket_id=tid, price_drift_pct=round(drift, 2), live_price=live_px)


def send_many(ids: list | None = None, all_pending: bool = False) -> dict:
    if all_pending or not ids:
        ids = [t["id"] for t in store.list_tickets("待确认")]
    results = []
    for tid in ids:
        r = send_ticket(int(tid))
        results.append({"id": int(tid), "ok": bool(r.get("ok")),
                        "message": r.get("message"), "symbol": r.get("symbol")})
    ok_n = sum(1 for x in results if x["ok"])
    return {"ok": True, "sent": ok_n, "failed": len(results) - ok_n, "results": results,
            "message": "成功发送 %d 张，失败 %d 张" % (ok_n, len(results) - ok_n)}


def ignore_ticket(tid: int) -> dict:
    t = store.get_ticket(tid)
    if not t:
        return {"ok": False, "message": "找不到这张委托单"}
    store.update_ticket(tid, status="已忽略", result="用户忽略")
    return {"ok": True, "message": "已忽略"}


def clear_tickets(all_of_them: bool = False) -> dict:
    left = store.clear_tickets(only_done=not all_of_them)
    return {"ok": True, "message": "已清理，还剩 %d 张待确认" % left, "pending": left}


# ---------------------------------------------------------------------------
# 止损监控
# ---------------------------------------------------------------------------
def stop_monitor() -> dict:
    b = manager.active()
    try:
        pos = b.positions()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": "读取持仓失败: %s" % str(e)[:150], "items": []}
    gsl = float(config.get("risk.stop_loss_pct", 0) or 0)
    items = []
    for p in pos:
        sym = p.get("symbol")
        qty = int(p.get("qty") or 0)
        if not sym or qty <= 0:
            continue
        avg = float(p.get("avg_cost") or 0)
        last = float(p.get("last_price") or 0)
        pnl_pct = p.get("pnl_pct")
        stop = None
        source = ""
        if gsl > 0 and avg:
            stop = avg * (1 - gsl / 100.0)
            source = "全局止损 %.1f%%" % gsl
        # 用回调买入法算一个技术止损位，哪个高用哪个（更紧的止损优先）
        tech = None
        try:
            ks = ds.kline(sym, "daily", 1, 200)
            ks = [k for k in ks if k.get("close")]
            if len(ks) >= 80:
                st = fac.pullback_state(ks)[-1]
                if st and st.get("stop"):
                    tech = float(st["stop"])
        except Exception:
            pass
        if tech and (stop is None or tech > stop):
            stop, source = tech, "技术止损（回调买入法支撑位）"
        dist = None
        if stop and last:
            dist = (last - stop) / last * 100.0
        alert = ""
        if pnl_pct is not None and gsl > 0 and float(pnl_pct) <= -gsl:
            alert = "已跌破全局止损线，建议立刻卖出"
        elif dist is not None and dist <= 1.0:
            alert = "价格已经贴到止损位，准备执行"
        elif dist is not None and dist < 0:
            alert = "已经跌破止损位！"
        items.append({
            "symbol": sym, "name": p.get("name"), "qty": qty,
            "available": int(p.get("available") or 0),
            "avg_cost": round(avg, 3), "last_price": round(last, 3),
            "pnl": p.get("pnl"), "pnl_pct": pnl_pct,
            "stop": round(stop, 3) if stop else None,
            "stop_source": source,
            "dist_pct": round(dist, 2) if dist is not None else None,
            "market_value": p.get("market_value"),
            "alert": alert,
        })
    items.sort(key=lambda x: (x["dist_pct"] is None, x["dist_pct"] if x["dist_pct"] is not None else 999))
    return {"ok": True, "items": items, "count": len(items),
            "alerts": [i for i in items if i["alert"]],
            "global_stop_pct": gsl,
            "note": "止损位取「全局止损价」和「回调买入法支撑位」里更紧的那个"}


def stop_all(symbols: list | None = None, dry_run: bool = False) -> dict:
    """一键止损：把持仓全部市价卖出（卖出是降风险，不受熔断限制）"""
    b = manager.active()
    if b.is_live and not config.get("live.enabled", False):
        return {"ok": False, "message": "实盘总开关没打开，无法发送真实委托"}
    try:
        pos = b.positions()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)[:150]}
    want = set(ds.normalize(s) for s in symbols) if symbols else None
    done, errs = [], []
    for p in pos:
        sym = p.get("symbol")
        if want and ds.normalize(sym) not in want:
            continue
        qty = int(p.get("available") or 0)
        if qty <= 0:
            done.append({"symbol": sym, "qty": 0, "ok": False, "message": "T+1 锁仓中，今天卖不了"})
            continue
        if dry_run:
            done.append({"symbol": sym, "qty": qty, "ok": True, "message": "（演练）将市价卖出"})
            continue
        try:
            r = b.sell(sym, price=None, qty=qty, order_type="market", reason="【一键止损】用户手动全部止损")
            done.append({"symbol": sym, "qty": qty, "ok": bool(r.get("ok")), "message": r.get("message")})
        except Exception as e:  # noqa: BLE001
            errs.append("%s: %s" % (sym, e))
    store.log("WARN", "live", "一键止损：处理 %d 只，错误 %d 个" % (len(done), len(errs)))
    return {"ok": True, "results": done, "errors": errs, "dry_run": dry_run}


# ---------------------------------------------------------------------------
# 从券商同步委托状态
# ---------------------------------------------------------------------------
QMT_STATUS = {48: "未报", 49: "待报", 50: "已报", 51: "已报待撤", 52: "部成待撤",
              53: "部撤", 54: "已撤", 55: "部成", 56: "已成", 57: "废单"}


def sync_orders() -> dict:
    """把券商的委托状态同步回来，这样页面上的「已报」能变成「已成」"""
    b = manager.active()
    if b.name == "paper":
        return {"ok": True, "synced": 0, "note": "模拟盘是即时成交的，不需要同步"}
    if not getattr(b, "connected", False):
        return {"ok": False, "message": "通道未连接"}
    try:
        raw = b.orders()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": "读取券商委托失败: %s" % str(e)[:150]}
    if not raw:
        return {"ok": True, "synced": 0}
    local = {str(o.get("entrust_no") or ""): o for o in store.list_orders(300)}
    n = 0
    for it in raw:
        if not isinstance(it, dict):
            continue
        eno = str(it.get("entrust_no") or it.get("order_id") or it.get("委托编号") or "")
        if not eno:
            continue
        row = local.get(eno)
        if not row:
            continue
        status = ""
        if "委托状态" in it:
            status = str(it.get("委托状态") or "")
        elif "status_raw" in it:
            status = QMT_STATUS.get(int(it.get("status_raw") or 0), "")
        elif "status" in it:
            status = str(it["status"])
        filled = it.get("filled") or it.get("成交数量") or it.get("traded_volume")
        fields = {}
        if status and status != row.get("status"):
            fields["status"] = status
        if filled is not None:
            try:
                fields["filled_qty"] = int(float(filled))
            except Exception:
                pass
        if fields:
            store.update_order(int(row["id"]), **fields)
            n += 1
    return {"ok": True, "synced": n}


# ---------------------------------------------------------------------------
# 当日交易报告
# ---------------------------------------------------------------------------
def daily_report() -> dict:
    b = manager.active()
    today = dt.date.today().isoformat()
    trades = [t for t in store.list_trades(500) if str(t.get("ts", "")).startswith(today)]
    orders = [o for o in store.list_orders(500) if str(o.get("ts", "")).startswith(today)]
    buys = [t for t in trades if t["side"] == "buy"]
    sells = [t for t in trades if t["side"] == "sell"]
    pnls = [float(t.get("pnl") or 0) for t in sells]
    wins = [x for x in pnls if x > 0]
    fees = sum(float(t.get("fee") or 0) for t in trades)
    try:
        bal = b.balance()
        pos = b.positions()
    except Exception:
        bal, pos = {}, []
    return {
        "ok": True, "date": today, "broker": b.display, "is_live": b.is_live,
        "trade_count": len(trades), "buy_count": len(buys), "sell_count": len(sells),
        "order_count": len(orders), "fee_total": round(fees, 2),
        "realized_pnl": round(sum(pnls), 2),
        "win_count": len(wins), "lose_count": len(pnls) - len(wins),
        "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else None,
        "balance": bal, "positions": pos,
        "trades": trades[:60], "orders": orders[:60],
        "best": max(sells, key=lambda x: float(x.get("pnl") or 0), default=None),
        "worst": min(sells, key=lambda x: float(x.get("pnl") or 0), default=None),
    }
