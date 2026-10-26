# -*- coding: utf-8 -*-
"""同花顺券商通道（通过 easytrader 驱动同花顺下单程序）

【重要说明，请务必读】
同花顺官方**没有**给散户提供下单 API。这个通道的原理是：
用开源库 easytrader 去"自动操作你的同花顺下单窗口"（模拟鼠标键盘点击），
也就是说电脑上必须开着同花顺的下单程序（xiadan.exe），并且已经登录。

它的特点：
  * 能用，但**比较脆弱**：同花顺升级界面后可能失效
  * 下单期间鼠标键盘会被抢占，所以这台电脑上别同时干别的
  * 稳定性不如券商官方的 QMT / PTrade 接口

如果券商给了你 QMT（迅投）权限，强烈建议改用 QMT 通道，那个是官方 API。

安装依赖（只需要一次）：
    pip install easytrader
"""
from __future__ import annotations

import os
import threading
import time

from .. import datasource as ds
from .. import store
from .base import BrokerBase, OrderResult

INSTALL_HINT = ("没有找到 easytrader 库。请在命令行里执行：\n"
                "    pip install easytrader\n"
                "装完再回到本页面点【连接】。")


def _pick(d: dict, *names, default=None):
    """在不同版本的 easytrader 返回里，按可能的字段名取值"""
    if not isinstance(d, dict):
        return default
    for n in names:
        if n in d and d[n] not in (None, "", "--"):
            return d[n]
    # 再做一次模糊匹配，防止字段名多了空格
    for k, v in d.items():
        kk = str(k).replace(" ", "")
        for n in names:
            if n.replace(" ", "") in kk and v not in (None, "", "--"):
                return v
    return default


def _f(v, default=0.0) -> float:
    if v is None:
        return default
    try:
        s = str(v).replace(",", "").replace("%", "").replace("元", "").strip()
        return float(s)
    except Exception:
        return default


def _i(v, default=0) -> int:
    try:
        return int(float(str(v).replace(",", "").strip()))
    except Exception:
        return default


class ThsBroker(BrokerBase):
    name = "ths"
    display = "同花顺（easytrader 界面自动化）"
    is_live = True
    needs_setup = True

    def __init__(self):
        super().__init__()
        self.user = None
        self._lock = threading.RLock()
        self.client_path = ""
        self.mode = "universal_client"

    # ------------------------------------------------------------------ 连接
    def connect(self, cfg: dict | None = None) -> dict:
        cfg = cfg or {}
        path = (cfg.get("client_path") or "").strip().strip('"')
        mode = (cfg.get("mode") or "universal_client").strip()
        self.client_path = path
        self.mode = mode
        if not path:
            self.last_error = "还没有填写同花顺下单程序 xiadan.exe 的路径"
            return {"ok": False, "message": self.last_error, "hint": self.setup_hint()}
        if not os.path.exists(path):
            self.last_error = "路径不存在：%s" % path
            return {"ok": False, "message": self.last_error, "hint": self.setup_hint()}
        try:
            import easytrader  # noqa: F401
        except Exception:
            self.last_error = "缺少 easytrader 库"
            return {"ok": False, "message": self.last_error, "hint": INSTALL_HINT}
        try:
            import easytrader
            with self._lock:
                self.user = easytrader.use(mode if mode else "universal_client")
                self.user.connect(path)
                # 如果填了账号密码并且打开了自动登录，就帮用户把登录框填掉
                if cfg.get("auto_login"):
                    self._try_auto_login(cfg, path)
                # 立刻读一次余额，验证真的连上了
                bal = self.user.balance
            self.connected = True
            self.connected_at = store.now_str()
            self.last_error = ""
            self.account_info = {"client_path": path, "mode": mode,
                                 "raw_balance": bal if isinstance(bal, (list, dict)) else str(bal)}
            store.log("INFO", "ths", "同花顺通道连接成功: %s" % path)
            return {"ok": True, "message": "同花顺连接成功", "account": self.account_info}
        except Exception as e:  # noqa: BLE001
            self.connected = False
            self.last_error = "连接失败: %s" % str(e)[:300]
            store.log("ERROR", "ths", self.last_error)
            return {"ok": False, "message": self.last_error,
                    "hint": "常见原因：\n1) 同花顺下单程序没打开，或没登录\n2) 路径填错了（要指向 xiadan.exe）\n3) 同花顺版本太新，界面变了，easytrader 需要更新"}

    def _try_auto_login(self, cfg: dict, path: str) -> None:
        """尝试用保存的账号密码自动登录（尽力而为，失败了就要求手动登录）"""
        from .. import secret
        user = str(cfg.get("username") or "").strip()
        pwd = secret.load("ths_password")
        if not user or not pwd:
            store.log("WARN", "ths", "打开了自动登录，但账号或密码是空的，请手动登录同花顺下单窗口")
            return
        try:
            self.user.prepare(user=user, password=pwd, exe_path=path)
            store.log("INFO", "ths", "已尝试自动登录（账号 %s）" % user[:3] + "***")
        except TypeError:
            try:
                self.user.prepare(user, pwd, path)
                store.log("INFO", "ths", "已尝试自动登录（旧版 easytrader 调用方式）")
            except Exception as e:  # noqa: BLE001
                store.log("WARN", "ths", "自动登录失败，请手动登录：%s" % str(e)[:150])
        except Exception as e:  # noqa: BLE001
            store.log("WARN", "ths", "自动登录失败，请手动登录：%s" % str(e)[:150])

    def disconnect(self) -> dict:
        if self.user is not None:
            try:
                self.user.exit()
            except Exception:
                pass
        self.user = None
        return super().disconnect()

    def setup_hint(self) -> str:
        return (
            "【同花顺通道怎么配】\n"
            "第 1 步：装库。命令行执行  pip install easytrader\n"
            "第 2 步：打开同花顺，进入【委托/下单】窗口，登录你的券商账号（普通交易，不用 VIP）\n"
            "第 3 步：找到这个下单窗口的程序路径，一般是\n"
            "        C:\\同花顺软件\\同花顺\\xiadan.exe\n"
            "     任务管理器里找到「同花顺下单程序」进程 → 右键 → 打开文件所在位置 就能看到\n"
            "第 4 步：把完整路径粘到下面输入框，点【连接】\n"
            "注意：连接和下单期间不要动鼠标键盘；同花顺要保持打开。\n"
            "更稳的方案：向你的券商申请 QMT 量化权限，用 QMT 通道。"
        )

    def _ensure(self):
        if not self.connected or self.user is None:
            raise RuntimeError("同花顺通道还没连接")

    def _guard(self):
        if not self.connected or self.user is None:
            return OrderResult(False, "未连接", "同花顺通道还没有连接，请先到【实盘交易】页点【连接】")
        return None

    # ------------------------------------------------------------------ 查询
    def balance(self) -> dict:
        self._ensure()
        with self._lock:
            raw = self.user.balance
        row = raw[0] if isinstance(raw, list) and raw else (raw if isinstance(raw, dict) else {})
        total = _f(_pick(row, "总资产", "资产总值", "总资产值"))
        cash = _f(_pick(row, "可用金额", "可用余额", "可用资金", "可用"))
        mv = _f(_pick(row, "股票市值", "证券市值", "市值"))
        frozen = _f(_pick(row, "冻结金额", "冻结资金"))
        bal = _f(_pick(row, "资金余额", "余额"))
        return {
            "broker": "ths",
            "total_asset": round(total, 2),
            "cash": round(bal or cash, 2),
            "available_cash": round(cash, 2),
            "market_value": round(mv, 2),
            "frozen": round(frozen, 2),
            "raw": row,
            "updated_at": store.now_str(),
        }

    def positions(self) -> list:
        self._ensure()
        with self._lock:
            raw = self.user.position or []
        if isinstance(raw, dict):
            raw = [raw]
        out = []
        for r in raw:
            code = str(_pick(r, "证券代码", "股票代码", "证券编码", default="") or "").strip()
            if not code:
                continue
            try:
                sym = ds.normalize(code)
            except Exception:
                continue
            qty = _i(_pick(r, "股票余额", "证券数量", "当前持仓", "参考持股"))
            if qty <= 0:
                continue
            out.append({
                "symbol": sym,
                "code": code,
                "name": str(_pick(r, "证券名称", "股票名称", default="") or ""),
                "qty": qty,
                "available": _i(_pick(r, "可用余额", "可用数量", "可卖数量")),
                "avg_cost": round(_f(_pick(r, "参考成本价", "成本价", "买入均价")), 3),
                "last_price": round(_f(_pick(r, "参考市价", "市价", "当前价")), 3),
                "market_value": round(_f(_pick(r, "参考市值", "市值")), 2),
                "pnl": round(_f(_pick(r, "参考盈亏", "浮动盈亏", "盈亏")), 2),
                "pnl_pct": round(_f(_pick(r, "盈亏比例", "盈亏率")), 2),
                "source": "ths",
            })
        return out

    def orders(self) -> list:
        self._ensure()
        try:
            with self._lock:
                raw = self.user.today_entrusts or []
            return raw if isinstance(raw, list) else []
        except Exception:
            return []

    def trades(self) -> list:
        self._ensure()
        try:
            with self._lock:
                raw = self.user.today_trades or []
            return raw if isinstance(raw, list) else []
        except Exception:
            return []

    # ------------------------------------------------------------------ 交易
    def _code6(self, symbol: str) -> str:
        return ds.normalize(symbol).split(".")[0]

    def buy(self, symbol: str, price=None, qty: int = 0, order_type: str = "limit",
            reason: str = "", strategy_id=None, **kw) -> OrderResult:
        g = self._guard()
        if g:
            return g
        code = self._code6(symbol)
        if not price:
            q = ds.quote(symbol)
            price = q.get("price")
        if not price:
            return OrderResult(False, "废单", "没有价格，无法下单")
        try:
            with self._lock:
                res = self.user.buy(code, price=float(price), amount=int(qty))
            entrust = str(_pick(res if isinstance(res, dict) else {}, "entrust_no", "委托编号", default="") or "")
            oid = store.add_order({"symbol": ds.normalize(symbol), "side": "buy", "price": float(price),
                                   "qty": int(qty), "status": "已报", "broker": "ths", "mode": "live",
                                   "reason": reason, "strategy_id": strategy_id, "entrust_no": entrust,
                                   "message": str(res)[:200]})
            store.log("INFO", "ths", "同花顺买入委托 %s %d股 @%.2f 委托号%s" % (code, qty, price, entrust))
            return OrderResult(True, "已报", "同花顺已提交委托", order_id=oid, entrust_no=entrust)
        except Exception as e:  # noqa: BLE001
            msg = "同花顺下单失败: %s" % str(e)[:250]
            oid = store.add_order({"symbol": ds.normalize(symbol), "side": "buy", "price": float(price),
                                   "qty": int(qty), "status": "废单", "broker": "ths", "mode": "live",
                                   "reason": reason, "strategy_id": strategy_id, "message": msg})
            store.log("ERROR", "ths", msg)
            return OrderResult(False, "废单", msg, order_id=oid)

    def sell(self, symbol: str, price=None, qty: int = 0, order_type: str = "limit",
             reason: str = "", strategy_id=None, **kw) -> OrderResult:
        g = self._guard()
        if g:
            return g
        code = self._code6(symbol)
        if not price:
            q = ds.quote(symbol)
            price = q.get("price")
        if not price:
            return OrderResult(False, "废单", "没有价格，无法下单")
        try:
            with self._lock:
                res = self.user.sell(code, price=float(price), amount=int(qty))
            entrust = str(_pick(res if isinstance(res, dict) else {}, "entrust_no", "委托编号", default="") or "")
            oid = store.add_order({"symbol": ds.normalize(symbol), "side": "sell", "price": float(price),
                                   "qty": int(qty), "status": "已报", "broker": "ths", "mode": "live",
                                   "reason": reason, "strategy_id": strategy_id, "entrust_no": entrust,
                                   "message": str(res)[:200]})
            store.log("INFO", "ths", "同花顺卖出委托 %s %d股 @%.2f 委托号%s" % (code, qty, price, entrust))
            return OrderResult(True, "已报", "同花顺已提交委托", order_id=oid, entrust_no=entrust)
        except Exception as e:  # noqa: BLE001
            msg = "同花顺下单失败: %s" % str(e)[:250]
            oid = store.add_order({"symbol": ds.normalize(symbol), "side": "sell", "price": float(price),
                                   "qty": int(qty), "status": "废单", "broker": "ths", "mode": "live",
                                   "reason": reason, "strategy_id": strategy_id, "message": msg})
            store.log("ERROR", "ths", msg)
            return OrderResult(False, "废单", msg, order_id=oid)

    def cancel(self, order_id) -> dict:
        self._ensure()
        row = store.q1("SELECT * FROM orders WHERE id=?", (int(order_id),))
        if not row or not row.get("entrust_no"):
            return {"ok": False, "message": "找不到这个委托的券商委托编号，无法撤单"}
        try:
            with self._lock:
                self.user.cancel_entrust(str(row["entrust_no"]))
            store.update_order(int(order_id), status="已撤")
            return {"ok": True, "message": "已撤单"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": "撤单失败: %s" % str(e)[:200]}

    def check_env(self) -> dict:
        """自检：告诉用户还差什么"""
        items = []
        try:
            import easytrader  # noqa: F401
            items.append({"name": "easytrader 库", "ok": True, "detail": "已安装"})
        except Exception:
            items.append({"name": "easytrader 库", "ok": False, "detail": "未安装，执行 pip install easytrader"})
        p = self.client_path
        items.append({"name": "同花顺下单程序路径", "ok": bool(p and os.path.exists(p)),
                      "detail": p or "未填写（在设置页填写 xiadan.exe 的完整路径）"})
        if p and os.path.exists(p):
            try:
                import subprocess
                out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq xiadan.exe"],
                                     capture_output=True, text=True, timeout=8)
                running = "xiadan.exe" in (out.stdout or "")
                items.append({"name": "下单程序是否在运行", "ok": running,
                              "detail": "运行中" if running else "没检测到 xiadan.exe 进程，请先打开同花顺下单窗口并登录"})
            except Exception as e:  # noqa: BLE001
                items.append({"name": "进程检测", "ok": False, "detail": str(e)[:100]})
        items.append({"name": "当前是否已连接", "ok": self.connected, "detail": "已连接" if self.connected else "未连接"})
        return {"ok": all(i["ok"] for i in items), "items": items}
