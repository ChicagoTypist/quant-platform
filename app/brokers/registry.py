# -*- coding: utf-8 -*-
"""券商通道管理：切换 / 连接 / 取当前使用的通道"""
from __future__ import annotations

import threading

from .. import config
from .. import store
from .base import BrokerBase
from .paper import PaperBroker
from .qmt import QmtBroker
from .ths import ThsBroker

BROKER_INFO = [
    {
        "name": "paper", "display": "模拟盘（虚拟资金）", "is_live": False, "needs_setup": False,
        "desc": "用真实行情 + 虚拟资金练习。所有策略都可以先在这里跑通，确认赚钱了再考虑实盘。",
        "fields": [],
    },
    {
        "name": "ths", "display": "同花顺（easytrader）", "is_live": True, "needs_setup": True,
        "desc": "自动操作你电脑上的同花顺下单窗口来买卖。需要安装 easytrader 并保持同花顺登录。属于界面自动化，不是官方接口，稳定性一般。",
        "fields": [
            {"key": "client_path", "label": "同花顺下单程序路径", "placeholder": "C:\\同花顺软件\\同花顺\\xiadan.exe",
             "help": "指向 xiadan.exe。任务管理器里找到「同花顺下单程序」进程 → 右键 → 打开文件所在位置"},
            {"key": "mode", "label": "模式", "type": "select", "default": "universal_client",
             "options": [{"value": "universal_client", "label": "同花顺通用版（推荐）"},
                         {"value": "ths", "label": "同花顺客户端"}]},
        ],
    },
    {
        "name": "qmt", "display": "QMT 迅投（券商官方）", "is_live": True, "needs_setup": True,
        "desc": "券商官方的量化交易接口，稳定可靠，推荐实盘使用。需要向券商单独申请量化权限。",
        "fields": [
            {"key": "userdata_path", "label": "QMT userdata 目录", "placeholder": "D:\\国金QMT交易端\\userdata_mini"},
            {"key": "account_id", "label": "资金账号", "placeholder": "你的资金账号"},
        ],
    },
]

_CLASSES = {"paper": PaperBroker, "ths": ThsBroker, "qmt": QmtBroker}


class BrokerManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._instances: dict[str, BrokerBase] = {}
        self.active_name = str(config.get("broker.active", "paper") or "paper")
        if self.active_name not in _CLASSES:
            self.active_name = "paper"

    def instance(self, name: str | None = None) -> BrokerBase:
        name = name or self.active_name
        if name not in _CLASSES:
            name = "paper"
        with self._lock:
            if name not in self._instances:
                self._instances[name] = _CLASSES[name]()
            return self._instances[name]

    def active(self) -> BrokerBase:
        return self.instance(self.active_name)

    def switch(self, name: str, cfg: dict | None = None, do_connect: bool = True) -> dict:
        if name not in _CLASSES:
            return {"ok": False, "message": "没有这个通道: %s" % name}
        with self._lock:
            self.active_name = name
            config.set_("broker.active", name)
            if cfg:
                config.update({"broker": {name: cfg}})
            b = self.instance(name)
        if name == "paper":
            b.connected = True
            store.log("INFO", "broker", "已切换到模拟盘")
            return {"ok": True, "message": "已切换到模拟盘", "status": b.status()}
        if not do_connect:
            return {"ok": True, "message": "已切换", "status": b.status()}
        res = b.connect(cfg or config.get("broker.%s" % name, {}))
        store.log("INFO" if res.get("ok") else "ERROR", "broker",
                  "切换到 %s: %s" % (name, res.get("message", "")))
        return dict(res, status=b.status())

    def all_status(self) -> list:
        out = []
        for info in BROKER_INFO:
            inst = self.instance(info["name"])
            st = inst.status()
            st.update({"desc": info["desc"], "fields": info["fields"]})
            out.append(st)
        return out


manager = BrokerManager()


def get_broker(name: str | None = None) -> BrokerBase:
    return manager.instance(name)
