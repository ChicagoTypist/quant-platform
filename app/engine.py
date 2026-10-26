# -*- coding: utf-8 -*-
"""实盘/模拟盘交易引擎

它做的事：
  1. 后台线程按固定间隔醒来（默认 5 秒）
  2. 检查现在是不是交易时间、行情是否正常
  3. 把每个"已启用 + 自动下单"的策略跑一遍，算出每只股票现在该不该持有
  4. 和实际持仓对比，该买的买、该卖的卖
  5. 每一单都要先过风控（仓位上限、单笔金额、每日亏损熔断、频率限制……）
  6. 记录净值曲线、委托、成交、日志

安全设计：
  * 默认只跑模拟盘，实盘必须手动打开总开关
  * 新建仓必须"信号是新的"（避免接入一个跑了很久的策略后，立刻按几个月前的信号追高）
  * 卖出永远允许（卖出是降低风险），但同样有频率限制
  * 一键急停 + 一键清仓
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
import traceback

from . import config
from . import datasource as ds
from . import strategies as st
from . import store
from .brokers import manager


# ---------------------------------------------------------------------------
# 风控
# ---------------------------------------------------------------------------
class RiskManager:
    def check_buy(self, ctx: dict) -> tuple:
        """买入前置检查，返回 (是否允许, 原因)"""
        if not config.get("risk.enabled", True):
            return True, ""
        sym = ctx["symbol"]
        price = float(ctx["price"])
        qty = int(ctx["qty"])
        amount = price * qty
        bal = ctx["balance"]
        pos_list = ctx["positions"]
        total = float(bal.get("total_asset") or 0)
        mv = float(bal.get("market_value") or 0)
        max_single = float(config.get("risk.max_single_position_pct", 20.0))
        max_total = float(config.get("risk.max_total_position_pct", 90.0))
        max_order = float(config.get("risk.max_order_amount", 50000.0))
        min_order = float(config.get("risk.min_order_amount", 1000.0))
        max_orders = int(config.get("risk.max_orders_per_day", 50))
        blacklist = [str(x).strip() for x in (config.get("risk.blacklist") or [])]

        if ctx.get("halted"):
            return False, "已触发熔断：%s" % ctx.get("halted")
        if ctx.get("orders_today", 0) >= max_orders:
            return False, "今天下单笔数已达上限 %d 笔" % max_orders
        if amount > max_order + 1e-6:
            return False, "单笔金额 %.0f 元超过上限 %.0f 元" % (amount, max_order)
        if amount < min_order - 1e-6:
            return False, "单笔金额 %.0f 元低于最小下单金额 %.0f 元" % (amount, min_order)
        if sym in blacklist or sym.split(".")[0] in blacklist:
            return False, "%s 在黑名单里" % sym
        if config.get("risk.block_st", True):
            nm = ""
            cur0 = next((p for p in pos_list if ds.normalize(p.get("symbol", "")) == sym), None)
            if cur0:
                nm = str(cur0.get("name") or "")
            if not nm:
                try:
                    nm = str((ds.quote(sym) or {}).get("name") or "")
                except Exception:
                    nm = ""
            if "ST" in nm.upper().replace(" ", ""):
                return False, "%s（%s）是 ST 股，风控禁止买入" % (sym, nm)
        cur = next((p for p in pos_list if ds.normalize(p.get("symbol", "")) == sym), None)
        cur_value = float(cur.get("market_value") or 0) if cur else 0.0
        if total > 0 and (cur_value + amount) / total * 100.0 > max_single + 0.01:
            return False, ("买入后该股占比 %.1f%% 超过单只上限 %.1f%%"
                           % ((cur_value + amount) / total * 100.0, max_single))
        if total > 0 and (mv + amount) / total * 100.0 > max_total + 0.01:
            return False, ("买入后总仓位 %.1f%% 超过上限 %.1f%%" % ((mv + amount) / total * 100.0, max_total))
        return True, ""

    def check_sell(self, ctx: dict) -> tuple:
        if ctx.get("orders_today", 0) >= int(config.get("risk.max_orders_per_day", 50)):
            return False, "今天下单笔数已达上限"
        if int(ctx.get("qty") or 0) <= 0:
            return False, "没有可卖数量"
        return True, ""


risk = RiskManager()


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
class TradingEngine:
    def __init__(self):
        self.running = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.start_time = ""
        self.last_tick = ""
        self.last_error = ""
        self.ticks = 0
        self.orders_today = 0
        self._day = dt.date.today().isoformat()
        self.halted_reason = ""
        self.last_equity_save = 0.0
        self._cooldown: dict = {}
        # 卖出失败退避：{symbol: (解锁时间戳, 原因)}
        # 有些卖单会被券商直接拒（跌停、停牌、可用数量对不上……）。
        # 不记住这个失败，引擎每轮（几秒~几十秒）都会再发一次，一天能刷出几百张废单。
        self._sell_block: dict = {}
        # 当天交易记录 {symbol: (side, "YYYY-MM-DD")}，防止信号抖动导致同日反向操作：
        # 上午卖了下午又买回、或者刚买就想卖（T+1 卖不掉变废单）。
        self._traded: dict = {}
        self.last_actions: list = []

    # ---------------- 生命周期 ----------------
    def start(self) -> dict:
        with self._lock:
            if self.running:
                return {"ok": True, "message": "引擎已经在跑了"}
            self._stop.clear()
            self.running = True
            self.start_time = store.now_str()
            self._thread = threading.Thread(target=self._loop, name="quant-engine", daemon=True)
            self._thread.start()
            # 启动就记一帧净值，让总览的资金曲线立刻有一个点
            try:
                b = manager.active()
                bal = b.balance()
                store.add_equity("live" if b.is_live else "paper", b.name,
                                 float(bal.get("total_asset") or 0), float(bal.get("cash") or 0),
                                 float(bal.get("market_value") or 0), float(bal.get("day_pnl") or 0))
                self.last_equity_save = time.time()
            except Exception:
                pass
            store.log("INFO", "engine", "交易引擎已启动（间隔 %ss，当前通道：%s）"
                      % (config.get("trading.poll_interval_sec", 5), manager.active().display))
            return {"ok": True, "message": "引擎已启动"}

    def stop(self) -> dict:
        with self._lock:
            if not self.running:
                return {"ok": True, "message": "引擎本来就没在跑"}
            self._stop.set()
            self.running = False
            store.log("INFO", "engine", "交易引擎已停止")
            return {"ok": True, "message": "引擎已停止"}

    def panic(self, liquidate: bool = False) -> dict:
        """急停：停引擎 +（可选）立刻市价清仓"""
        self.stop()
        result = {"stopped": True, "liquidated": [], "errors": []}
        if liquidate:
            try:
                b = manager.active()
                for p in b.positions():
                    sym = p.get("symbol")
                    # 同上：available=0（T+1 锁定）不能回退成 qty，否则清仓单全是废单
                    _av = p.get("available")
                    qty = int(p.get("qty") or 0) if _av is None else int(_av or 0)
                    if not sym or qty <= 0:
                        continue
                    try:
                        r = b.sell(sym, price=None, qty=qty, order_type="market",
                                   reason="【急停清仓】用户手动一键清仓")
                        result["liquidated"].append({"symbol": sym, "qty": qty,
                                                     "ok": bool(r.get("ok")), "message": r.get("message")})
                    except Exception as e:  # noqa: BLE001
                        result["errors"].append("%s: %s" % (sym, e))
            except Exception as e:  # noqa: BLE001
                result["errors"].append(str(e))
        store.log("WARN", "engine", "触发急停（清仓=%s）" % liquidate)
        return result

    def status(self) -> dict:
        b = manager.active()
        cfg = config.load()
        return {
            "running": self.running,
            "start_time": self.start_time,
            "last_tick": self.last_tick,
            "last_error": self.last_error,
            "ticks": self.ticks,
            "orders_today": self.orders_today,
            "halted": bool(self.halted_reason),
            "halted_reason": self.halted_reason,
            "broker": b.name,
            "broker_display": b.display,
            "is_live": b.is_live,
            "live_enabled": bool(cfg["live"]["enabled"]),
            "poll_interval_sec": cfg["trading"]["poll_interval_sec"],
            "last_actions": self.last_actions[-15:],
        }

    # ---------------- 主循环 ----------------
    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self.last_error = "%s: %s" % (type(e).__name__, e)
                store.log("ERROR", "engine", "引擎循环出错: %s" % self.last_error,
                          detail=traceback.format_exc()[-1200:])
            interval = max(1, int(config.get("trading.poll_interval_sec", 5) or 5))
            self._stop.wait(interval)

    def tick(self) -> dict:
        b = manager.active()
        now = dt.datetime.now()
        today = now.date().isoformat()
        if today != self._day:
            self._day = today
            self.orders_today = 0
            self.halted_reason = ""
            self._cooldown.clear()
            store.log("INFO", "engine", "新交易日 %s，计数器已重置" % today)

        self.ticks += 1
        self.last_tick = store.now_str()
        actions: list = []

        try:
            b.refresh()
        except Exception as e:  # noqa: BLE001
            store.log("WARN", "engine", "刷新通道失败: %s" % str(e)[:150])

        bal = {}
        try:
            bal = b.balance()
        except Exception as e:  # noqa: BLE001
            store.log("WARN", "engine", "读取账户失败: %s" % str(e)[:150])
        if bal and "total_asset" in bal and time.time() - self.last_equity_save > 60:
            store.add_equity("live" if b.is_live else "paper", b.name,
                             float(bal.get("total_asset") or 0), float(bal.get("cash") or 0),
                             float(bal.get("market_value") or 0), float(bal.get("day_pnl") or 0))
            self.last_equity_save = time.time()

        self._check_halt(bal)

        self._expire_deployments()

        try:
            mstatus = ds.market_status()
        except Exception as e:  # noqa: BLE001
            store.log("WARN", "engine", "市场状态查询失败: %s" % str(e)[:150])
            mstatus = {}
        if not mstatus.get("session_open"):
            return {"ok": True, "skipped": "非交易时段（%s）" % mstatus.get("label", "未知"),
                    "market": mstatus}

        # 全局止损止盈（如果开启了）
        try:
            actions.extend(self.apply_stop_rules())
        except Exception as e:  # noqa: BLE001
            store.log("WARN", "engine", "止损止盈检查失败: %s" % str(e)[:150])

        # 实盘保险闸：通道是真实券商但总开关没开 -> 一张真实委托都不许下
        if b.is_live and not config.get("live.enabled", False):
            if time.time() - getattr(self, "_live_warn_ts", 0.0) > 120:
                self._live_warn_ts = time.time()
                store.log("WARN", "engine", "实盘通道 %s 已连接，但实盘总开关未打开，引擎不会下任何真实委托。"
                          % b.display)
            return {"ok": True, "skipped": "实盘总开关未打开", "market": mstatus}

        # 只有【已部署】的策略才会真的自动交易
        alls = store.list_strategies()
        strategies = [s for s in alls if s.get("deployed") and s.get("enabled")]
        if not strategies:
            n = len([x for x in alls if x.get("deployed")])
            return {"ok": True,
                    "skipped": "没有已部署的策略" if not n else "已部署的策略都过期了",
                    "market": mstatus}
        try:
            positions = b.positions()
        except Exception:
            positions = []

        for s in strategies:
            try:
                acts = self.run_strategy(s, bal, positions, dry_run=False)
                actions.extend(acts)
                positions = b.positions()
                bal = b.balance()
            except Exception as e:  # noqa: BLE001
                store.log("ERROR", "engine", "策略《%s》执行失败: %s" % (s["name"], str(e)[:200]),
                          detail=traceback.format_exc()[-1000:])
        self.last_actions = (self.last_actions + actions)[-50:]
        return {"ok": True, "actions": actions, "market": mstatus}

    def _expire_deployments(self) -> None:
        """授权到期的策略自动下线（不管是不是交易时段都要检查）"""
        now = store.now_str()
        for s in store.list_strategies():
            exp = str(s.get("expires_at") or "")
            if s.get("deployed") and exp and exp < now:
                store.save_strategy(dict(s, deployed=False, id=s["id"]))
                store.log("WARN", "engine", "策略《%s》的授权已于 %s 到期，已自动下线"
                          % (s["name"], exp))

    def _check_halt(self, bal: dict) -> None:
        if self.halted_reason:
            return
        if not config.get("risk.enabled", True):
            return
        lim = float(config.get("risk.daily_loss_limit_pct", 5.0) or 0)
        if lim <= 0:
            return
        day_pnl_pct = bal.get("day_pnl_pct")
        if day_pnl_pct is not None and float(day_pnl_pct) <= -lim:
            self.halted_reason = ("当日亏损 %.2f%% 触及熔断线 %.2f%%，已停止开新仓（卖出不受影响）"
                                  % (float(day_pnl_pct), lim))
            store.log("ERROR", "engine", self.halted_reason)

    # ---------------- 策略执行 ----------------
    def evaluate(self, s: dict) -> dict:
        """算出一个策略现在对每只股票的期望状态"""
        symbols = [x for x in (s.get("symbols") or []) if x]
        if not symbols:
            return {}
        try:
            strat = st.get_strategy(s["stype"], s.get("params") or {}, symbols)
        except Exception as e:  # noqa: BLE001
            store.log("ERROR", "engine", "策略类型不存在: %s" % e)
            return {}
        data = {}
        for sym in symbols:
            try:
                ks = ds.kline(sym, "daily", 1, 400)
                ks = [k for k in ks if k.get("close")]
                if len(ks) >= 30:
                    data[ds.normalize(sym)] = ks
            except Exception as e:  # noqa: BLE001
                store.log("WARN", "engine", "取 %s 历史数据失败: %s" % (sym, str(e)[:120]))
        if not data:
            return {}
        try:
            sig_map = strat.signals_multi(data)
        except Exception as e:  # noqa: BLE001
            store.log("ERROR", "engine", "策略信号计算失败: %s" % str(e)[:200])
            return {}
        max_age = max(1, int(config.get("trading.max_signal_age_bars", 1) or 1))
        # 轮动/组合类策略（momentum_rotation、factor_rank）的信号含义是
        # 「目标持仓，一直持有到下次调仓」，它可能落在几十根 K 线之前。
        # 这类策略不能用「信号在第几根 K 线上」判断新鲜度，
        # 否则它几乎永远被判「信号过期」，一只股票都买不进去。
        # 对它们改用「K 线本身是不是最新的」来判断。
        is_portfolio = bool(getattr(strat, "portfolio", False))
        today_d = dt.date.today()
        # 用户手动指定（或组合优化算出来）的每只股票权重，优先于策略默认权重
        sw = s.get("symbol_weights") or {}
        out = {}
        n_sym = max(len(data), 1)
        for sym, ks in data.items():
            sigs = sig_map.get(sym) or []
            if not sigs:
                out[sym] = {"target": "flat", "reason": "没有信号", "fresh": False, "weight": 100.0 / n_sym}
                continue
            last = sigs[-1]
            n = len(ks)
            if is_portfolio:
                try:
                    ld = dt.datetime.strptime(str(ks[-1].get("date"))[:10], "%Y-%m-%d").date()
                    fresh = (today_d - ld).days <= 7
                except Exception:
                    fresh = True
                # 组合策略的信号含义是「目标持仓状态」，每个调仓日都会重新评估。
                # 「信号日」应取最新 K 线日期（当前评估基准），而不是最后一次买卖事件的日期——
                # 否则一只股票几个月前掉出前 5 名后一直没再进出，就会一直显示几个月前的旧日期，
                # 和「信号是新的」自相矛盾，让用户误以为系统没更新。
                signal_date = str(ks[-1].get("date"))[:10] if ks else ""
            else:
                fresh = int(last.get("i", -1)) >= n - 1 - (max_age - 1)
                signal_date = ks[int(last.get("i", 0))]["date"][:10]
            w = last.get("weight")
            out[sym] = {
                "target": "long" if last.get("action") == "buy" else "flat",
                "reason": last.get("reason", ""),
                "signal_date": signal_date,
                "fresh": fresh,
                "weight": float(w) if w is not None else 100.0 / n_sym,
                "bar_index": int(last.get("i", -1)),
                "bars": n,
                "weight_source": "策略默认",
            }
            if sw and sym in sw:
                try:
                    out[sym]["weight"] = float(sw[sym])
                    out[sym]["weight_source"] = "自定义/优化器"
                except Exception:
                    pass
        return out

    def run_strategy(self, s: dict, bal: dict, positions: list, dry_run: bool = False) -> list:
        b = manager.active()
        state = self.evaluate(s)
        if not state:
            return []
        actions = []
        # 每个策略可以有自己的上限，取「策略设定」和「全局风控」里更严的那个
        g_single = float(config.get("risk.max_single_position_pct", 20.0))
        g_order = float(config.get("risk.max_order_amount", 50000.0))
        max_single = min(float(s.get("max_single_pct") or g_single), g_single)
        max_order = min(float(s.get("max_order_amount") or g_order), g_order)
        capital_pct = float(s.get("capital_pct") or 100.0)
        sl_pct = float(s.get("stop_loss_pct") or config.get("risk.stop_loss_pct", 0) or 0)
        tp_pct = float(s.get("take_profit_pct") or 0)
        # direct_send=True  -> 引擎直接把单子发出去（全自动）
        # direct_send=False -> 只生成待确认委托单，等用户点发送
        direct_send = bool(s.get("direct_send", True))
        need_confirm = not direct_send
        st_syms = set(ds.normalize(x) for x in (s.get("symbols") or []))
        deployed_value = sum(float(p.get("market_value") or 0)
                             for p in positions if ds.normalize(p.get("symbol") or "") in st_syms)
        total_asset = float(bal.get("total_asset") or 0)
        cap_value = total_asset * capital_pct / 100.0
        lot = int(config.get("trading.lot_size", 100) or 100)
        for sym, info in state.items():
            pos = next((p for p in positions if ds.normalize(p.get("symbol", "")) == sym), None)
            qty = int(pos.get("qty") or 0) if pos else 0
            # 关键：available=0 是「当天买入、T+1 锁定」的【有效值】，不是缺字段！
            # 写成 pos.get("available") or pos.get("qty") 会因为 0 是 falsy 而回退成总持仓，
            # 于是引擎误以为能卖 900 股，发出去的卖单必然被券商打成废单（可卖不足），
            # 冷却 30 秒后又来一次 —— 这就是用户看到的「反复卖、反复废单」死循环。
            if pos is None:
                avail = 0
            else:
                _av = pos.get("available")
                avail = qty if _av is None else int(_av or 0)
            want_long = info["target"] == "long"
            have = qty > 0
            today_s = dt.date.today().isoformat()
            prev_trade = self._traded.get(sym)
            if want_long and not have:
                # 今天刚卖过就别买回来了 —— 信号盘中抖动会造成「卖了又买、买了又卖」，
                # 白交手续费还容易踏空。当天不反向，是最省事也最稳的约束。
                if prev_trade and prev_trade[0] == "sell" and prev_trade[1] == today_s:
                    actions.append({"symbol": sym, "action": "skip",
                                    "reason": "今天已经卖过这只票，当天不再买回（防止信号抖动来回打）"})
                    continue
                if not info["fresh"]:
                    actions.append({"symbol": sym, "action": "skip",
                                    "reason": "策略看多，但信号是 %s 的、已经过期，不追高建仓"
                                              % info.get("signal_date", "?")})
                    continue
                try:
                    price = (ds.quote(sym) or {}).get("price")
                except Exception:
                    price = None
                if not price:
                    actions.append({"symbol": sym, "action": "skip", "reason": "拿不到实时价格"})
                    continue
                weight = min(float(info.get("weight") or 20.0), max_single)
                total = float(bal.get("total_asset") or 0)
                target_value = min(total * weight / 100.0, max_order)
                # 这个策略的额度还剩多少
                room = cap_value - deployed_value
                if room <= 0:
                    actions.append({"symbol": sym, "action": "skip",
                                    "reason": "策略《%s》的资金额度 %.0f 元已经用满（当前占用 %.0f 元）"
                                              % (s["name"], cap_value, deployed_value)})
                    continue
                target_value = min(target_value, room)
                n_qty = int(target_value / float(price) // lot) * lot
                if n_qty <= 0:
                    actions.append({"symbol": sym, "action": "skip",
                                    "reason": "按 %.1f%% 仓位算不足一手（股价 %.2f 太贵）" % (weight, price)})
                    continue
                if dry_run:
                    actions.append({"symbol": sym, "action": "buy", "qty": n_qty, "price": float(price),
                                    "reason": info["reason"], "dry_run": True, "weight": weight})
                    continue
                ok, why = risk.check_buy({"symbol": sym, "price": float(price), "qty": n_qty,
                                          "balance": bal, "positions": positions,
                                          "orders_today": self.orders_today,
                                          "halted": self.halted_reason})
                if not ok:
                    actions.append({"symbol": sym, "action": "blocked", "reason": why})
                    continue
                if not self._cooldown_ok(sym):
                    actions.append({"symbol": sym, "action": "skip", "reason": "距上次下单太近，冷却中"})
                    continue
                if need_confirm:
                    # 这个策略设的是「生成待确认单」，先放队列等你点发送
                    from . import live as live_mod
                    rk = {"建议仓位%": round(weight, 1), "风控": "通过",
                          "信号日": info.get("signal_date", "")}
                    tid = live_mod.make_ticket("策略", sym, "buy", n_qty, float(price),
                                               strategy_id=s["id"], strategy_name=s["name"],
                                               reason=info["reason"],
                                               risk_text=json.dumps(rk, ensure_ascii=False))
                    self._mark_cooldown(sym)
                    actions.append({"symbol": sym, "action": "ticket", "qty": n_qty,
                                    "price": float(price), "ok": True, "ticket_id": tid,
                                    "status": "待确认",
                                    "message": "已生成待确认委托单 #%d，去实盘交易页点发送" % tid,
                                    "reason": info["reason"]})
                    continue
                r = b.buy(sym, price=float(price), qty=n_qty, order_type="limit",
                          reason="[%s] %s" % (s["name"], info["reason"]), strategy_id=s["id"])
                self.orders_today += 1
                self._mark_cooldown(sym)
                if r.get("ok"):
                    self._traded[sym] = ("buy", today_s)
                actions.append({"symbol": sym, "action": "buy", "qty": n_qty, "price": float(price),
                                "ok": bool(r.get("ok")), "status": r.get("status"),
                                "message": r.get("message"), "reason": info["reason"]})
            elif want_long and have and (sl_pct > 0 or tp_pct > 0):
                # 策略自带的止损/止盈：比策略信号优先
                pnl_pct = pos.get("pnl_pct") if pos else None
                hit = ""
                if pnl_pct is not None:
                    if sl_pct > 0 and float(pnl_pct) <= -sl_pct:
                        hit = "策略止损触发（亏 %.2f%%，止损线 -%.1f%%）" % (float(pnl_pct), sl_pct)
                    elif tp_pct > 0 and float(pnl_pct) >= tp_pct:
                        hit = "策略止盈触发（赚 %.2f%%，止盈线 +%.1f%%）" % (float(pnl_pct), tp_pct)
                if hit and avail > 0:
                    if dry_run:
                        actions.append({"symbol": sym, "action": "sell", "qty": avail,
                                        "reason": hit, "dry_run": True})
                    else:
                        r = b.sell(sym, price=None, qty=avail, order_type="market",
                                   reason="[%s] %s" % (s["name"], hit), strategy_id=s["id"])
                        self.orders_today += 1
                        self._mark_cooldown(sym)
                        if not r.get("ok") or str(r.get("status") or "") == "废单":
                            self._block_sell(sym, r.get("message") or r.get("status") or "卖出被拒")
                        elif r.get("ok"):
                            self._traded[sym] = ("sell", today_s)
                        actions.append({"symbol": sym, "action": "sell", "qty": avail,
                                        "ok": bool(r.get("ok")), "status": r.get("status"),
                                        "message": r.get("message"), "reason": hit})
                    continue
                if pos:
                    actions.append({"symbol": sym, "action": "hold",
                                    "reason": "持有中（距止损 %.2f%%）"
                                              % (float(pnl_pct) + sl_pct if pnl_pct is not None else 0)})
            elif (not want_long) and have:
                if prev_trade and prev_trade[0] == "buy" and prev_trade[1] == today_s:
                    actions.append({"symbol": sym, "action": "skip",
                                    "reason": "今天刚买入，T+1 规则下不能卖（明天再处理）"})
                    continue
                if avail <= 0:
                    actions.append({"symbol": sym, "action": "skip",
                                    "reason": "策略要卖，但可用数量为 0（T+1 锁仓中，明天才能卖）"})
                    continue
                blocked = self._sell_blocked(sym)
                if blocked:
                    actions.append({"symbol": sym, "action": "skip",
                                    "reason": "上次卖出被拒（%s），已暂停重试" % blocked})
                    continue
                if dry_run:
                    actions.append({"symbol": sym, "action": "sell", "qty": avail, "price": None,
                                    "reason": info["reason"], "dry_run": True})
                    continue
                ok, why = risk.check_sell({"symbol": sym, "qty": avail, "orders_today": self.orders_today})
                if not ok:
                    actions.append({"symbol": sym, "action": "blocked", "reason": why})
                    continue
                if not self._cooldown_ok(sym):
                    actions.append({"symbol": sym, "action": "skip", "reason": "冷却中"})
                    continue
                if need_confirm:
                    from . import live as live_mod
                    tid = live_mod.make_ticket("策略", sym, "sell", avail, None,
                                               strategy_id=s["id"], strategy_name=s["name"],
                                               reason=info["reason"], order_type="market",
                                               risk_text=json.dumps({"类型": "清仓"}, ensure_ascii=False))
                    self._mark_cooldown(sym)
                    actions.append({"symbol": sym, "action": "ticket", "qty": avail, "ok": True,
                                    "ticket_id": tid, "status": "待确认",
                                    "message": "已生成待确认委托单 #%d，去实盘交易页点发送" % tid,
                                    "reason": info["reason"]})
                    continue
                r = b.sell(sym, price=None, qty=avail, order_type="market",
                           reason="[%s] %s" % (s["name"], info["reason"]), strategy_id=s["id"])
                self.orders_today += 1
                self._mark_cooldown(sym)
                # 卖单被券商拒（废单/可卖不足/停牌/跌停）就退避，别再每轮刷一张
                if not r.get("ok") or str(r.get("status") or "") == "废单":
                    self._block_sell(sym, r.get("message") or r.get("status") or "卖出被拒")
                elif r.get("ok"):
                    self._traded[sym] = ("sell", today_s)
                actions.append({"symbol": sym, "action": "sell", "qty": avail,
                                "ok": bool(r.get("ok")), "status": r.get("status"),
                                "message": r.get("message"), "reason": info["reason"]})
        try:
            store.touch_strategy(s["id"], "; ".join("%s %s" % (a["symbol"], a["action"]) for a in actions)[:400])
            if actions:
                store.execute("UPDATE strategies SET last_action_at=? WHERE id=?",
                              (store.now_str(), s["id"]))
        except Exception:
            pass
        return actions

    def _cooldown_ok(self, sym: str) -> bool:
        cd = float(config.get("trading.order_cooldown_sec", 30) or 0)
        return (time.time() - self._cooldown.get(sym, 0)) >= cd

    def _mark_cooldown(self, sym: str) -> None:
        self._cooldown[sym] = time.time()

    # ---------------- 卖出失败退避 ----------------
    def _sell_blocked(self, sym: str) -> str:
        """这只股票是否因为上次卖出被拒而处于退避期。返回原因或空串。"""
        item = self._sell_block.get(sym)
        if not item:
            return ""
        until, reason = item
        if time.time() >= until:
            self._sell_block.pop(sym, None)
            return ""
        return reason

    def _block_sell(self, sym: str, reason: str, seconds: float = 900.0) -> None:
        """卖出被拒后拉起退避，避免每轮重发废单。

        默认 15 分钟：既能把「一天几百张废单」压到个位数，
        又不会因为一次偶发失败就把这只票永久锁死。
        """
        self._sell_block[sym] = (time.time() + seconds, (reason or "卖出被拒")[:80])
        store.log("WARN", "engine", "%s 卖出被拒（%s），%d 分钟内不再尝试"
                  % (sym, (reason or "")[:60], int(seconds // 60)))

    # ---------------- 全局止损止盈 ----------------
    def apply_stop_rules(self, dry_run: bool = False) -> list:
        sl = float(config.get("risk.stop_loss_pct", 0) or 0)
        tp = float(config.get("risk.take_profit_pct", 0) or 0)
        if sl <= 0 and tp <= 0:
            return []
        b = manager.active()
        out = []
        try:
            positions = b.positions()
        except Exception:
            return []
        for p in positions:
            pnl_pct = p.get("pnl_pct")
            if pnl_pct is None:
                continue
            sym = p.get("symbol")
            qty = int(p.get("available") or 0)
            if qty <= 0:
                continue
            if sl > 0 and float(pnl_pct) <= -sl:
                if dry_run:
                    out.append({"symbol": sym, "action": "sell", "reason": "全局止损 %.2f%%" % pnl_pct})
                else:
                    r = b.sell(sym, price=None, qty=qty, order_type="market", reason="【风控】全局止损")
                    out.append({"symbol": sym, "action": "sell", "ok": bool(r.get("ok")),
                                "message": r.get("message"), "reason": "全局止损 %.2f%%" % pnl_pct})
            elif tp > 0 and float(pnl_pct) >= tp:
                if dry_run:
                    out.append({"symbol": sym, "action": "sell", "reason": "全局止盈 %.2f%%" % pnl_pct})
                else:
                    r = b.sell(sym, price=None, qty=qty, order_type="market", reason="【风控】全局止盈")
                    out.append({"symbol": sym, "action": "sell", "ok": bool(r.get("ok")),
                                "message": r.get("message"), "reason": "全局止盈 %.2f%%" % pnl_pct})
        return out


engine = TradingEngine()
