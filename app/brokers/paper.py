# -*- coding: utf-8 -*-
"""模拟盘券商（纸上交易）

用真实的实时行情成交，资金是虚拟的。规则完全按 A 股来：
  * 一手 = 100 股，买入必须是 100 的整数倍
  * T+1：今天买的今天不能卖
  * 涨停买不进、跌停卖不出
  * 限价单价格没到就挂着，到了自动成交（每个轮询周期撮合一次）
  * 手续费/印花税/过户费/滑点都算进去
所有状态存在 data/quant.db，重启程序不会丢。
"""
from __future__ import annotations

import datetime as dt

from .. import config
from .. import datasource as ds
from .. import fees as fee_mod
from .. import store
from .base import BrokerBase, OrderResult


def _lot() -> int:
    return int(config.get("trading.lot_size", 100) or 100)


class PaperBroker(BrokerBase):
    name = "paper"
    display = "模拟盘（虚拟资金，真实行情）"
    is_live = False
    needs_setup = False

    def __init__(self):
        super().__init__()
        self.connected = True
        self.connected_at = store.now_str()
        self.account_info = {"type": "模拟盘"}
        self._pending: dict[int, dict] = {}
        self._restore_pending()

    def _restore_pending(self) -> None:
        """程序重启后，把还挂着的委托恢复回来"""
        try:
            rows = store.q("SELECT * FROM orders WHERE broker='paper' AND status='已报'")
        except Exception:
            return
        for r in rows:
            self._pending[int(r["id"])] = {
                "id": int(r["id"]), "symbol": r["symbol"], "name": r["name"] or "",
                "side": r["side"], "price": float(r["price"] or 0), "qty": int(r["qty"] or 0),
                "strategy_id": r["strategy_id"], "reason": r["reason"] or "",
            }

    # ------------------------------------------------------------------ 账户
    def _acct(self) -> dict:
        row = store.q1("SELECT * FROM paper_account WHERE id=1")
        if not row:
            cap = float(config.get("paper.initial_capital", 200000.0))
            store.execute("INSERT INTO paper_account(id,cash,initial_capital,updated_at) VALUES(1,?,?,?)",
                          (cap, cap, store.now_str()))
            row = store.q1("SELECT * FROM paper_account WHERE id=1")
        return row

    def _settle(self) -> None:
        """T+1 结算：过了交易日，昨天买的就变成可卖"""
        today = self.today()
        for p in store.q("SELECT * FROM paper_positions"):
            if p["open_date"] != today and int(p["qty"]) > 0:
                store.execute("UPDATE paper_positions SET available=?, open_date=?, updated_at=? WHERE symbol=?",
                              (int(p["qty"]), today, store.now_str(), p["symbol"]))

    def reset(self, capital: float | None = None) -> dict:
        cap = float(capital if capital is not None else config.get("paper.initial_capital", 200000.0))
        store.execute("DELETE FROM paper_positions")
        store.execute("UPDATE paper_account SET cash=?, initial_capital=?, updated_at=? WHERE id=1",
                      (cap, cap, store.now_str()))
        store.execute("DELETE FROM orders WHERE broker='paper'")
        store.execute("DELETE FROM trades WHERE broker='paper'")
        store.execute("DELETE FROM equity WHERE broker='paper'")
        self._pending.clear()
        store.log("INFO", "paper", "模拟盘已重置，初始资金 %.2f 元" % cap)
        return {"ok": True, "initial_capital": cap}

    def _frozen_cash(self) -> float:
        return sum(float(o["price"]) * int(o["qty"]) for o in self._pending.values() if o["side"] == "buy")

    def balance(self) -> dict:
        self._settle()
        acct = self._acct()
        cash = float(acct["cash"])
        pos = self.positions()
        mv = sum(float(p.get("market_value") or 0) for p in pos)
        float_pnl = sum(float(p.get("pnl") or 0) for p in pos)
        total = cash + mv
        key = "paper_day_open_" + self.today()
        day_open = store.kv_get(key)
        if day_open is None:
            day_open = total
            store.kv_set(key, total)
        realized = store.realized_pnl_today("paper")
        init = float(acct["initial_capital"]) or 1.0
        frozen = self._frozen_cash()
        # 手续费单独立账：不然界面上只有一个"累计盈亏"，用户没法判断
        # 这 -9716 里到底有没有把佣金印花税算进去（答案是算进去了，
        # 但买入费藏在成本里、卖出费混在已实现里，肉眼追溯不到）。
        fee = store.fee_summary("paper")
        fee_recalc = store.fee_recalc("paper")
        fee_delta = None
        if fee_recalc is not None:
            # 每笔费都 round 到分，重算与实收之间天然有几分钱的舍入差，
            # 笔数越多累计噪音越大。不抹掉它，界面会一直挂着
            # 「按当前费率 432.32」这种没有意义的提示。
            n = fee["buy_count"] + fee["sell_count"]
            noise = max(1.0, n * 0.01)
            d = fee_recalc - fee["total_fee"]
            fee_delta = 0.0 if abs(d) < noise else round(d, 2)
        return {
            "broker": "paper",
            "total_asset": round(total, 2),
            "cash": round(cash, 2),
            "available_cash": round(max(cash - frozen, 0.0), 2),
            "market_value": round(mv, 2),
            "frozen": round(frozen, 2),
            "initial_capital": round(float(acct["initial_capital"]), 2),
            "total_pnl": round(total - float(acct["initial_capital"]), 2),
            "total_pnl_pct": round((total / init - 1.0) * 100.0, 2),
            "float_pnl": round(float_pnl, 2),
            "realized_pnl_today": round(realized, 2),
            "day_pnl": round(total - float(day_open), 2),
            "day_pnl_pct": round((total / float(day_open) - 1.0) * 100.0, 2) if day_open else 0.0,
            "fee_buy": round(fee["buy_fee"], 2),
            "fee_sell": round(fee["sell_fee"], 2),
            "fee_total": round(fee["total_fee"], 2),
            "fee_buy_count": fee["buy_count"],
            "fee_sell_count": fee["sell_count"],
            "fee_turnover": round(fee["turnover"], 2),
            "fee_recalc": fee_recalc,
            "fee_delta": fee_delta,
            "pending_orders": len(self._pending),
            "updated_at": store.now_str(),
        }

    def positions(self) -> list:
        rows = store.q("SELECT * FROM paper_positions WHERE qty > 0 ORDER BY symbol")
        if not rows:
            return []
        try:
            qs = ds.quotes([r["symbol"] for r in rows])
        except Exception:
            qs = {}
        out = []
        for r in rows:
            q = qs.get(r["symbol"]) or {}
            last = q.get("price") or r["last_price"] or r["avg_cost"]
            qty = int(r["qty"])
            mv = float(last) * qty
            cost = float(r["avg_cost"]) * qty
            out.append({
                "symbol": r["symbol"], "code": r["symbol"].split(".")[0],
                "name": q.get("name") or r["name"] or r["symbol"],
                "qty": qty, "available": int(r["available"]),
                "avg_cost": round(float(r["avg_cost"]), 3),
                "last_price": round(float(last), 3),
                "market_value": round(mv, 2), "cost_value": round(cost, 2),
                "pnl": round(mv - cost, 2),
                "pnl_pct": round((mv / cost - 1.0) * 100.0, 2) if cost else 0.0,
                "change_pct": q.get("change_pct"), "open_date": r["open_date"],
            })
        return out

    # ------------------------------------------------------------------ 下单校验
    def _judge(self, symbol: str, side: str, qty: int, order_type: str, limit) -> tuple:
        """返回 (结果, 说明, 行情, 参考价)
        结果: 'ok' 立即成交 / 'pending' 挂单等价格 / 'reject' 直接废单
        """
        try:
            q = ds.quote(symbol)
        except Exception as e:  # noqa: BLE001
            return "reject", "取行情失败: %s" % str(e)[:80], {}, 0.0
        if not q or not q.get("price"):
            return "reject", "拿不到 %s 的实时价格（代码可能不对，或该股停牌）" % symbol, q, 0.0
        if qty <= 0:
            return "reject", "下单数量必须大于 0", q, 0.0
        if side == "buy" and qty % _lot() != 0:
            return "reject", "买入数量必须是 %d 的整数倍（一手 = 100 股）" % _lot(), q, 0.0
        mkt = float(q["price"])
        if config.get("trading.price_limit_check", True):
            lp, ld = q.get("limit_up"), q.get("limit_down")
            if side == "buy" and lp and mkt >= float(lp) - 1e-9:
                return "pending", "已涨停(%.2f)买不进，委托先挂着" % mkt, q, mkt
            if side == "sell" and ld and mkt <= float(ld) + 1e-9:
                return "pending", "已跌停(%.2f)卖不出，委托先挂着" % mkt, q, mkt
        if order_type == "limit" and limit:
            lim = float(limit)
            if side == "buy" and mkt > lim * 1.0001:
                return "pending", "限价 %.3f 低于现价 %.3f，等价格跌下来再成交" % (lim, mkt), q, mkt
            if side == "sell" and mkt < lim * 0.9999:
                return "pending", "限价 %.3f 高于现价 %.3f，等价格涨上去再成交" % (lim, mkt), q, mkt
        return "ok", "", q, mkt

    def _fill_price(self, side: str, mkt: float, order_type: str, limit, fcfg: dict) -> float:
        """成交价：以市价为基础加滑点，限价单只作为"我能接受的最差价"约束"""
        px = fee_mod.slip(mkt, side, fcfg)
        if order_type == "limit" and limit:
            px = min(px, float(limit)) if side == "buy" else max(px, float(limit))
        return round(px, 3)

    # ------------------------------------------------------------------ 买入
    def buy(self, symbol: str, price=None, qty: int = 0, order_type: str = "limit",
            reason: str = "", strategy_id=None, **kw) -> OrderResult:
        sym = ds.normalize(symbol)
        qty = int(qty or 0)
        verdict, msg, q, mkt = self._judge(sym, "buy", qty, order_type, price)
        name = (q or {}).get("name", "")
        if verdict == "reject":
            oid = store.add_order({"symbol": sym, "name": name, "side": "buy", "price": float(price or 0),
                                   "qty": qty, "status": "废单", "broker": "paper", "mode": "paper",
                                   "reason": reason, "strategy_id": strategy_id, "message": msg})
            store.log("WARN", "paper", "买入被拒 %s: %s" % (sym, msg))
            return OrderResult(False, "废单", msg, order_id=oid)
        fcfg = fee_mod.fee_config()
        est_px = self._fill_price("buy", mkt, order_type, price, fcfg)
        est_amount = est_px * qty
        est_fee = fee_mod.buy_fee(est_amount, fcfg)
        cash = max(float(self._acct()["cash"]) - self._frozen_cash(), 0.0)
        if est_amount + est_fee > cash + 1e-6:
            msg = "资金不足：需要 %.2f 元（其中手续费 %.2f），可用 %.2f 元" % (est_amount + est_fee, est_fee, cash)
            oid = store.add_order({"symbol": sym, "name": name, "side": "buy", "price": est_px, "qty": qty,
                                   "status": "废单", "broker": "paper", "mode": "paper", "reason": reason,
                                   "strategy_id": strategy_id, "message": msg})
            store.log("WARN", "paper", "买入被拒 %s: %s" % (sym, msg))
            return OrderResult(False, "废单", msg, order_id=oid)
        if verdict == "pending":
            oid = store.add_order({"symbol": sym, "name": name, "side": "buy", "price": float(price or mkt),
                                   "qty": qty, "status": "已报", "broker": "paper", "mode": "paper",
                                   "reason": reason, "strategy_id": strategy_id, "message": msg})
            self._pending[oid] = {"id": oid, "symbol": sym, "name": name, "side": "buy",
                                  "price": float(price or mkt), "qty": qty,
                                  "strategy_id": strategy_id, "reason": reason}
            store.log("INFO", "paper", "买入挂单 %s %d股 限价%.3f (%s)" % (sym, qty, float(price or mkt), msg))
            return OrderResult(False, "已报", msg, order_id=oid)
        oid = store.add_order({"symbol": sym, "name": name, "side": "buy", "price": est_px, "qty": qty,
                               "status": "已成交", "broker": "paper", "mode": "paper", "reason": reason,
                               "strategy_id": strategy_id, "message": "模拟成交"})
        return self._do_buy(oid, sym, name, est_px, qty, strategy_id, reason)

    def _do_buy(self, oid, sym, name, px, qty, strategy_id=None, reason="") -> OrderResult:
        fcfg = fee_mod.fee_config()
        amount = px * qty
        fee = fee_mod.buy_fee(amount, fcfg)
        cash = float(self._acct()["cash"])
        if amount + fee > cash + 1e-6:
            store.update_order(oid, status="废单", message="成交时资金不足（可能被别的委托占用）")
            return OrderResult(False, "废单", "资金不足，委托作废", order_id=oid)
        store.execute("UPDATE paper_account SET cash=cash-?, updated_at=? WHERE id=1", (amount + fee, store.now_str()))
        today = self.today()
        row = store.q1("SELECT * FROM paper_positions WHERE symbol=?", (sym,))
        if row:
            new_qty = int(row["qty"]) + qty
            new_cost = float(row["avg_cost"]) * int(row["qty"]) + amount + fee
            avail = int(row["available"]) if row["open_date"] == today else int(row["qty"])
            store.execute("UPDATE paper_positions SET qty=?, available=?, avg_cost=?, last_price=?, name=?, updated_at=? WHERE symbol=?",
                          (new_qty, avail, new_cost / new_qty if new_qty else 0.0, px, name or row["name"], store.now_str(), sym))
        else:
            store.execute("INSERT INTO paper_positions(symbol,name,qty,available,avg_cost,last_price,open_date,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                          (sym, name, qty, 0, (amount + fee) / qty, px, today, store.now_str()))
        store.update_order(oid, status="已成交", filled_qty=qty, avg_price=px, fee=round(fee, 2),
                           message="模拟成交", ts=store.now_str())
        store.add_trade({"order_id": oid, "strategy_id": strategy_id, "symbol": sym, "name": name, "side": "buy",
                         "price": px, "qty": qty, "amount": round(amount, 2), "fee": round(fee, 2),
                         "broker": "paper", "mode": "paper", "pnl": 0.0, "note": reason})
        store.log("INFO", "paper", "买入成交 %s %s %d股 @%.3f 手续费%.2f (%s)" % (sym, name, qty, px, fee, reason or "手动"))
        return OrderResult(True, "已成交", "模拟买入成交", order_id=oid, filled_qty=qty,
                           avg_price=px, fee=round(fee, 2))

    # ------------------------------------------------------------------ 卖出
    def sell(self, symbol: str, price=None, qty: int = 0, order_type: str = "limit",
             reason: str = "", strategy_id=None, **kw) -> OrderResult:
        sym = ds.normalize(symbol)
        self._settle()
        row = store.q1("SELECT * FROM paper_positions WHERE symbol=?", (sym,))
        if not row or int(row["qty"]) <= 0:
            msg = "没有 %s 的持仓，无法卖出" % sym
            oid = store.add_order({"symbol": sym, "side": "sell", "price": price, "qty": qty, "status": "废单",
                                   "broker": "paper", "mode": "paper", "reason": reason,
                                   "strategy_id": strategy_id, "message": msg})
            return OrderResult(False, "废单", msg, order_id=oid)
        avail = int(row["available"])
        want = int(qty) if qty else avail
        if want > avail:
            msg = "可卖不足：可用 %d 股，想卖 %d 股（T+1 当天买的不能卖）" % (avail, want)
            oid = store.add_order({"symbol": sym, "side": "sell", "price": price, "qty": want, "status": "废单",
                                   "broker": "paper", "mode": "paper", "reason": reason,
                                   "strategy_id": strategy_id, "message": msg})
            return OrderResult(False, "废单", msg, order_id=oid)
        verdict, msg, q, mkt = self._judge(sym, "sell", want, order_type, price)
        name = (q or {}).get("name") or row["name"] or ""
        if verdict == "reject":
            oid = store.add_order({"symbol": sym, "name": name, "side": "sell", "price": float(price or 0),
                                   "qty": want, "status": "废单", "broker": "paper", "mode": "paper",
                                   "reason": reason, "strategy_id": strategy_id, "message": msg})
            store.log("WARN", "paper", "卖出被拒 %s: %s" % (sym, msg))
            return OrderResult(False, "废单", msg, order_id=oid)
        fcfg = fee_mod.fee_config()
        if verdict == "pending":
            oid = store.add_order({"symbol": sym, "name": name, "side": "sell", "price": float(price or mkt),
                                   "qty": want, "status": "已报", "broker": "paper", "mode": "paper",
                                   "reason": reason, "strategy_id": strategy_id, "message": msg})
            self._pending[oid] = {"id": oid, "symbol": sym, "name": name, "side": "sell",
                                  "price": float(price or mkt), "qty": want,
                                  "strategy_id": strategy_id, "reason": reason}
            store.log("INFO", "paper", "卖出挂单 %s %d股 限价%.3f (%s)" % (sym, want, float(price or mkt), msg))
            return OrderResult(False, "已报", msg, order_id=oid)
        px = self._fill_price("sell", mkt, order_type, price, fcfg)
        oid = store.add_order({"symbol": sym, "name": name, "side": "sell", "price": px, "qty": want,
                               "status": "已成交", "broker": "paper", "mode": "paper", "reason": reason,
                               "strategy_id": strategy_id, "message": "模拟成交"})
        return self._do_sell(oid, sym, name, px, want, strategy_id, reason)

    def _do_sell(self, oid, sym, name, px, qty, strategy_id=None, reason="") -> OrderResult:
        self._settle()
        row = store.q1("SELECT * FROM paper_positions WHERE symbol=?", (sym,))
        if not row or int(row["available"]) < qty:
            store.update_order(oid, status="废单", message="成交时持仓/可用数量不足")
            return OrderResult(False, "废单", "成交时可用持仓不足，委托作废", order_id=oid)
        fcfg = fee_mod.fee_config()
        amount = px * qty
        fee = fee_mod.sell_fee(amount, fcfg)
        avg_cost = float(row["avg_cost"])
        pnl = amount - fee - avg_cost * qty
        store.execute("UPDATE paper_account SET cash=cash+?, updated_at=? WHERE id=1", (amount - fee, store.now_str()))
        left = int(row["qty"]) - qty
        if left <= 0:
            store.execute("DELETE FROM paper_positions WHERE symbol=?", (sym,))
        else:
            store.execute("UPDATE paper_positions SET qty=?, available=?, last_price=?, updated_at=? WHERE symbol=?",
                          (left, max(int(row["available"]) - qty, 0), px, store.now_str(), sym))
        store.update_order(oid, status="已成交", filled_qty=qty, avg_price=px, fee=round(fee, 2),
                           message="模拟成交", ts=store.now_str())
        store.add_trade({"order_id": oid, "strategy_id": strategy_id, "symbol": sym, "name": name, "side": "sell",
                         "price": px, "qty": qty, "amount": round(amount, 2), "fee": round(fee, 2),
                         "broker": "paper", "mode": "paper", "pnl": round(pnl, 2), "note": reason})
        store.log("INFO", "paper", "卖出成交 %s %s %d股 @%.3f 盈亏%.2f (%s)" % (sym, name, qty, px, pnl, reason or "手动"))
        return OrderResult(True, "已成交", "模拟卖出成交", order_id=oid, filled_qty=qty,
                           avg_price=px, fee=round(fee, 2), pnl=round(pnl, 2))

    # ------------------------------------------------------------------ 委托管理
    def orders(self) -> list:
        return store.q("SELECT * FROM orders WHERE broker='paper' ORDER BY id DESC LIMIT 200")

    def trades(self) -> list:
        return store.q("SELECT * FROM trades WHERE broker='paper' ORDER BY id DESC LIMIT 200")

    def pending(self) -> list:
        return list(self._pending.values())

    def cancel(self, order_id) -> dict:
        oid = int(order_id)
        if oid in self._pending:
            self._pending.pop(oid, None)
        row = store.q1("SELECT * FROM orders WHERE id=?", (oid,))
        if not row:
            return {"ok": False, "message": "找不到这笔委托"}
        if row["status"] == "已成交":
            return {"ok": False, "message": "这笔委托已经成交了，撤不了"}
        store.update_order(oid, status="已撤", message="用户撤单")
        return {"ok": True, "message": "已撤单"}

    def refresh(self) -> dict:
        """每个轮询周期调用：尝试撮合挂着的限价单"""
        if not self._pending:
            return {"ok": True, "pending": 0, "filled": 0}
        self._settle()
        filled = 0
        for oid in list(self._pending.keys()):
            o = self._pending.get(oid)
            if not o:
                continue
            verdict, msg, q, mkt = self._judge(o["symbol"], o["side"], int(o["qty"]),
                                               "limit", o["price"])
            if verdict != "ok":
                if verdict == "reject":
                    store.update_order(oid, status="废单", message=msg)
                    self._pending.pop(oid, None)
                continue
            fcfg = fee_mod.fee_config()
            px = self._fill_price(o["side"], mkt, "limit", o["price"], fcfg)
            if o["side"] == "buy":
                r = self._do_buy(oid, o["symbol"], o["name"], px, int(o["qty"]), o["strategy_id"], o["reason"])
            else:
                r = self._do_sell(oid, o["symbol"], o["name"], px, int(o["qty"]), o["strategy_id"], o["reason"])
            if r.get("ok") or r.get("status") == "废单":
                self._pending.pop(oid, None)
            if r.get("ok"):
                filled += 1
        return {"ok": True, "pending": len(self._pending), "filled": filled}

    # ------------------------------------------------------------------ 工具
    def max_buy_shares(self, symbol: str, price: float | None = None) -> int:
        try:
            q = ds.quote(symbol)
        except Exception:
            return 0
        px = float(price or q.get("price") or 0)
        if px <= 0:
            return 0
        cash = max(float(self._acct()["cash"]) - self._frozen_cash(), 0.0)
        fcfg = fee_mod.fee_config()
        px = fee_mod.slip(px, "buy", fcfg)
        shares = int((cash * 0.995) / (px * (1 + fcfg["commission_rate"] + fcfg["transfer_fee_rate"])))
        return int(shares // _lot()) * _lot()

    def sellable_shares(self, symbol: str) -> int:
        self._settle()
        row = store.q1("SELECT * FROM paper_positions WHERE symbol=?", (ds.normalize(symbol),))
        return int(row["available"]) if row else 0
