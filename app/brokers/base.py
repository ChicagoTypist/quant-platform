# -*- coding: utf-8 -*-
"""券商接口基类

所有券商（模拟盘、同花顺、QMT）都实现这几件事：
    connect / disconnect / status
    balance / positions / orders / trades
    buy / sell / cancel

统一用 dict 返回，方便直接转成给网页的 JSON。
"""
from __future__ import annotations

import datetime as dt
from typing import Any


class OrderResult(dict):
    """下单结果，本质上是个 dict"""

    def __init__(self, ok: bool, status: str = "", message: str = "", **kw):
        super().__init__(ok=ok, status=status, message=message, **kw)
        self.setdefault("order_id", None)
        self.setdefault("entrust_no", "")
        self.setdefault("filled_qty", 0)
        self.setdefault("avg_price", 0.0)
        self.setdefault("fee", 0.0)


class BrokerBase:
    name = "base"
    display = "基础接口"
    is_live = False          # True = 会真的把钱投出去
    needs_setup = False      # True = 需要用户额外安装/配置

    def __init__(self):
        self.connected = False
        self.last_error = ""
        self.connected_at = ""
        self.account_info: dict = {}

    # ---- 生命周期 --------------------------------------------------------
    def connect(self, cfg: dict | None = None) -> dict:
        raise NotImplementedError

    def disconnect(self) -> dict:
        self.connected = False
        self.account_info = {}
        return {"ok": True, "message": "已断开"}

    def status(self) -> dict:
        return {
            "name": self.name, "display": self.display, "is_live": self.is_live,
            "connected": self.connected, "last_error": self.last_error,
            "connected_at": self.connected_at, "needs_setup": self.needs_setup,
            "account": self.account_info,
        }

    # ---- 查询 ------------------------------------------------------------
    def balance(self) -> dict:
        raise NotImplementedError

    def positions(self) -> list:
        raise NotImplementedError

    def orders(self) -> list:
        return []

    def trades(self) -> list:
        return []

    # ---- 交易 ------------------------------------------------------------
    def buy(self, symbol: str, price: float | None = None, qty: int = 0, **kw) -> OrderResult:
        raise NotImplementedError

    def sell(self, symbol: str, price: float | None = None, qty: int = 0, **kw) -> OrderResult:
        raise NotImplementedError

    def cancel(self, order_id) -> dict:
        return {"ok": False, "message": "该通道不支持撤单"}

    def refresh(self) -> dict:
        """轮询时调用，用来撮合未成交的委托（模拟盘）/ 同步券商状态"""
        return {"ok": True}

    # ---- 工具 ------------------------------------------------------------
    @staticmethod
    def today() -> str:
        return dt.date.today().isoformat()

    def snapshot(self) -> dict:
        """给网页用的一站式快照"""
        out: dict[str, Any] = {"status": self.status()}
        try:
            out["balance"] = self.balance()
        except Exception as e:  # noqa: BLE001
            out["balance"] = {"error": str(e)[:200]}
        try:
            out["positions"] = self.positions()
        except Exception as e:  # noqa: BLE001
            out["positions"] = []
            out["positions_error"] = str(e)[:200]
        return out
