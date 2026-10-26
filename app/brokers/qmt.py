# -*- coding: utf-8 -*-
"""QMT（迅投）券商通道 —— 券商官方量化接口，比同花顺自动化稳得多

前提：你的券商（国金/华泰/中信建投等）给你开通了 QMT 量化权限，
      并且在电脑上装了 QMT 客户端（miniQMT / 极速版），登录后保持运行。

需要在设置页填写：
  1. QMT 的 userdata 目录，例如  D:\\国金QMT交易端\\userdata_mini
  2. 你的资金账号
然后点【连接】。

依赖：QMT 安装目录下的 xtquant 包（券商客户端自带，一般需要把它复制到 Python 的
      site-packages，或者把 QMT 目录加到 PYTHONPATH）。
"""
from __future__ import annotations

import os
import random
import threading

from .. import datasource as ds
from .. import store
from .base import BrokerBase, OrderResult

SETUP_HINT = ("【QMT 通道怎么配】\n"
              "1. 找你的券商客户经理申请开通 QMT/量化交易权限（通常要求资金量，各家不同）\n"
              "2. 装好 QMT 客户端，用资金账号登录，保持运行\n"
              "3. 在设置页填 userdata 目录（QMT 安装目录下的 userdata_mini）和资金账号\n"
              "4. 确保 Python 能 import xtquant（券商客户端目录里自带这个包）\n"
              "好处：这是券商官方 API，稳定、不会抢鼠标、能程序化撤单查成交。")

BUY = 23
SELL = 24


class QmtBroker(BrokerBase):
    name = "qmt"
    display = "QMT 迅投（券商官方量化接口）"
    is_live = True
    needs_setup = True

    def __init__(self):
        super().__init__()
        self.trader = None
        self.account = None
        self._lock = threading.RLock()
        self._xtconstant = None

    def connect(self, cfg: dict | None = None) -> dict:
        cfg = cfg or {}
        path = (cfg.get("userdata_path") or "").strip().strip('"')
        acct = (cfg.get("account_id") or "").strip()
        if not path or not os.path.isdir(path):
            self.last_error = "userdata 目录不存在：%s" % (path or "（没填）")
            return {"ok": False, "message": self.last_error, "hint": SETUP_HINT}
        if not acct:
            self.last_error = "还没填资金账号"
            return {"ok": False, "message": self.last_error, "hint": SETUP_HINT}
        try:
            from xtquant.xttrader import XtQuantTrader
            from xtquant.xttype import StockAccount
            from xtquant import xtconstant
        except Exception:
            self.last_error = "没有找到 xtquant 包"
            return {"ok": False, "message": self.last_error,
                    "hint": "xtquant 在 QMT 安装目录里。请把 QMT 目录下的 xtquant 文件夹\n"
                            "复制到 Python 的 site-packages 目录，或设置 PYTHONPATH。"}
        try:
            session = int(random.randint(100000, 999999))
            trader = XtQuantTrader(path, session)
            trader.start()
            r = trader.connect()
            if r != 0:
                self.last_error = "连接 QMT 失败，返回码 %s（-1 通常是客户端没开或路径不对）" % r
                return {"ok": False, "message": self.last_error, "hint": SETUP_HINT}
            account = StockAccount(acct, "STOCK")
            trader.subscribe(account)
            self.trader = trader
            self.account = account
            self._xtconstant = xtconstant
            self.connected = True
            self.connected_at = store.now_str()
            self.last_error = ""
            self.account_info = {"userdata_path": path, "account_id": acct}
            store.log("INFO", "qmt", "QMT 通道连接成功，账号 %s" % acct)
            return {"ok": True, "message": "QMT 连接成功", "account": self.account_info}
        except Exception as e:  # noqa: BLE001
            self.connected = False
            self.last_error = "QMT 连接异常: %s" % str(e)[:300]
            return {"ok": False, "message": self.last_error, "hint": SETUP_HINT}

    def disconnect(self) -> dict:
        try:
            if self.trader:
                self.trader.stop()
        except Exception:
            pass
        self.trader = None
        return super().disconnect()

    def _ensure(self):
        if not self.connected or self.trader is None:
            raise RuntimeError("QMT 通道还没连接")

    def balance(self) -> dict:
        self._ensure()
        with self._lock:
            a = self.trader.query_stock_asset(self.account)
        if a is None:
            return {"broker": "qmt", "total_asset": 0, "cash": 0, "error": "查询资产返回空"}
        return {
            "broker": "qmt",
            "total_asset": round(float(getattr(a, "total_asset", 0) or 0), 2),
            "cash": round(float(getattr(a, "cash", 0) or 0), 2),
            "available_cash": round(float(getattr(a, "cash", 0) or 0), 2),
            "market_value": round(float(getattr(a, "market_value", 0) or 0), 2),
            "frozen": round(float(getattr(a, "frozen_cash", 0) or 0), 2),
            "updated_at": store.now_str(),
        }

    def positions(self) -> list:
        self._ensure()
        with self._lock:
            ps = self.trader.query_stock_positions(self.account) or []
        out = []
        for p in ps:
            code = str(getattr(p, "stock_code", "") or "")
            if not code:
                continue
            qty = int(getattr(p, "volume", 0) or 0)
            if qty <= 0:
                continue
            sym = code if "." in code else ds.normalize(code)
            avg = float(getattr(p, "avg_price", 0) or 0)
            mv = float(getattr(p, "market_value", 0) or 0)
            last = mv / qty if qty else avg
            out.append({
                "symbol": sym, "code": sym.split(".")[0], "name": "",
                "qty": qty, "available": int(getattr(p, "can_use_volume", 0) or 0),
                "avg_cost": round(avg, 3), "last_price": round(last, 3),
                "market_value": round(mv, 2),
                "pnl": round(mv - avg * qty, 2),
                "pnl_pct": round((last / avg - 1.0) * 100.0, 2) if avg else 0.0,
                "source": "qmt",
            })
        # 用行情补一下名字
        try:
            qs = ds.quotes([p["symbol"] for p in out])
            for p in out:
                p["name"] = (qs.get(p["symbol"]) or {}).get("name", "")
        except Exception:
            pass
        return out

    def orders(self) -> list:
        self._ensure()
        try:
            with self._lock:
                os_ = self.trader.query_stock_orders(self.account) or []
            return [{"order_id": getattr(o, "order_id", ""), "symbol": getattr(o, "stock_code", ""),
                     "side": "买入" if getattr(o, "order_type", 0) in (23, 24) and
                             getattr(o, "order_type", 0) == 23 else "卖出",
                     "price": getattr(o, "price", 0), "qty": getattr(o, "order_volume", 0),
                     "filled": getattr(o, "traded_volume", 0), "status_raw": getattr(o, "order_status", 0),
                     "time": getattr(o, "order_time", 0)} for o in os_]
        except Exception:
            return []

    def _order(self, side: str, symbol, price, qty, reason="", strategy_id=None) -> OrderResult:
        if not self.connected or self.trader is None:
            return OrderResult(False, "未连接", "QMT 通道还没有连接，请先到【实盘交易】页点【连接】")
        self._ensure()
        sym = symbol if "." in str(symbol) else ds.normalize(symbol)
        code = sym
        if not price:
            q = ds.quote(sym)
            price = q.get("price")
        if not price:
            return OrderResult(False, "废单", "没有价格，无法下单")
        const = self._xtconstant.STOCK_BUY if side == "buy" else self._xtconstant.STOCK_SELL
        try:
            with self._lock:
                oid = self.trader.order_stock(self.account, code, const, int(qty),
                                              self._xtconstant.FIX_PRICE, float(price))
            if oid is None or (isinstance(oid, int) and oid < 0):
                raise RuntimeError("券商拒单，返回 %s（可能是资金不足/无权限/数量不对）" % oid)
            dbid = store.add_order({"symbol": sym, "side": side, "price": float(price), "qty": int(qty),
                                    "status": "已报", "broker": "qmt", "mode": "live", "reason": reason,
                                    "strategy_id": strategy_id, "entrust_no": str(oid),
                                    "message": "QMT 委托已提交"})
            store.log("INFO", "qmt", "QMT %s委托 %s %d股 @%.2f 委托号%s" % (side, code, qty, price, oid))
            return OrderResult(True, "已报", "QMT 已提交委托", order_id=dbid, entrust_no=str(oid))
        except Exception as e:  # noqa: BLE001
            msg = "QMT 下单失败: %s" % str(e)[:250]
            dbid = store.add_order({"symbol": sym, "side": side, "price": float(price), "qty": int(qty),
                                    "status": "废单", "broker": "qmt", "mode": "live", "reason": reason,
                                    "strategy_id": strategy_id, "message": msg})
            store.log("ERROR", "qmt", msg)
            return OrderResult(False, "废单", msg, order_id=dbid)

    def buy(self, symbol, price=None, qty=0, order_type="limit", reason="", strategy_id=None, **kw):
        return self._order("buy", symbol, price, qty, reason, strategy_id)

    def sell(self, symbol, price=None, qty=0, order_type="limit", reason="", strategy_id=None, **kw):
        return self._order("sell", symbol, price, qty, reason, strategy_id)

    def cancel(self, order_id) -> dict:
        self._ensure()
        row = store.q1("SELECT * FROM orders WHERE id=?", (int(order_id),))
        if not row or not row.get("entrust_no"):
            return {"ok": False, "message": "找不到券商委托编号"}
        try:
            with self._lock:
                r = self.trader.cancel_order_stock(self.account, int(row["entrust_no"]))
            store.update_order(int(order_id), status="已撤")
            return {"ok": True, "message": "已撤单", "code": r}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": "撤单失败: %s" % str(e)[:200]}

    def check_env(self) -> dict:
        items = []
        try:
            import xtquant  # noqa: F401
            items.append({"name": "xtquant 包", "ok": True, "detail": "已安装"})
        except Exception:
            items.append({"name": "xtquant 包", "ok": False,
                          "detail": "未找到，需要把 QMT 安装目录下的 xtquant 复制到 Python site-packages"})
        items.append({"name": "当前是否已连接", "ok": self.connected,
                      "detail": "已连接" if self.connected else "未连接"})
        return {"ok": all(i["ok"] for i in items), "items": items}
