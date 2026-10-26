# -*- coding: utf-8 -*-
"""REST API —— 网页上所有按钮最终都是调这里的接口

设计原则：任何异常都不能把服务器搞崩，统一返回 JSON。
"""
from __future__ import annotations

import datetime as dt
import datetime as _dt
import json
import traceback

from . import backtest as backtest_mod
from . import config
from . import datasource as ds
from . import screener as scr
from . import fundamental as fnd
from . import factorlab as lab
from . import factors as fac
from . import live
from . import dlfactor as dlf
from . import optimizer as opt
from . import secret
from . import strategies as st
from . import strategies as st_mod
from . import store
from .brokers import BROKER_INFO, manager
from .engine import engine

DEFAULT_LIMIT = 200


def _i(v, d=0):
    try:
        return int(float(v))
    except Exception:
        return d


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _sl(v, d=None):
    if v is None:
        return d
    if isinstance(v, list):
        return [str(x) for x in v]
    return [x.strip() for x in str(v).replace("，", ",").split(",") if x.strip()]


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
def build_id() -> str:
    """构建指纹：版本号 + 关键源码的【内容哈希】。

    用途：启动时如果发现某个端口上跑着旧代码，就不要再连它，换个端口起新版本。

    为什么用内容哈希而不是只数因子/策略/接口的数量？
      因为两个版本可能数量一模一样（v2.2.0 和 v2.3.0 就踩过这个坑），
      而且同一个小版本里改一个 bug 也不会变数量。
      用源码的（大小+修改时间）哈希，只要代码动过就能识别出来。
    """
    try:
        import hashlib
        from pathlib import Path
        from . import __version__
        h = hashlib.md5()
        base = Path(__file__).resolve().parent
        for name in ("api.py", "engine.py", "store.py", "strategies.py", "factors.py",
                     "server.py", "live.py", "backtest.py", "optimizer.py", "factorlab.py",
                     "assistant.py", "secret.py", "config.py", "datasource.py",
                     "indicators.py", "fees.py", "mdview.py", "main.py"):
            p = base / name
            if p.exists():
                st = p.stat()
                h.update(("%s:%d:%d;" % (name, st.st_size, st.st_mtime_ns)).encode())
        return "%s.%s" % (__version__, h.hexdigest()[:10])
    except Exception:
        return "unknown"


def h_about(q, b):
    from . import CHANGELOG, __version__, __version_name__
    import platform as _pf
    return 200, {
        "version": __version__, "version_name": __version_name__,
        "changelog": CHANGELOG,
        "python": _pf.python_version(),
        "platform": _pf.platform(),
        "factors": len(fac.FACTORS),
        "alphas": len([k for k in fac.FACTORS if k.startswith("alpha")]),
        "strategies": len(st.REGISTRY),
        "routes": len(ROUTES),
        "build": build_id(),
    }


def h_health(q, b):
    from . import __version__, __version_name__
    return 200, {"ok": True, "app": "A股量化交易平台",
                 "version": __version__, "version_name": __version_name__,
                 "build": build_id(),
                 "time": store.now_str(), "pid": __import__("os").getpid()}


def h_bootstrap(q, b):
    """一次请求把首页需要的所有东西都返回，减少等待"""
    brokers = manager.all_status()
    try:
        mstatus = ds.market_status()
    except Exception as e:  # noqa: BLE001
        mstatus = {"state": "unknown", "label": "状态获取失败: %s" % str(e)[:80]}
    idx = []
    try:
        idx = ds.indices()
    except Exception:
        pass
    return 200, {
        "config": config.public_config(),
        "brokers": brokers,
        "active_broker": manager.active_name,
        "engine": engine.status(),
        "market": mstatus,
        "indices": idx,
        "templates": st.all_templates(),
        "version": __import__("app", fromlist=["x"]).__version__,
        "version_name": __import__("app", fromlist=["x"]).__version_name__,
    }


def h_settings_get(q, b):
    return 200, {"config": config.public_config(),
                 "paths": {"base_dir": str(config.BASE_DIR), "data_dir": str(config.DATA_DIR),
                           "db": str(config.DB_FILE)}}


def _sanitize_settings(node, default_node, path=""):
    """按默认配置的类型校验输入，数字字段填了垃圾（比如 abc）就直接拒绝"""
    errors = []
    if isinstance(default_node, dict):
        if not isinstance(node, dict):
            return node, ["%s 应该是对象" % (path or "根")]
        out = {}
        for k, v in node.items():
            if k not in default_node:
                out[k] = v                       # 未知字段放行（可能是新功能提前写入）
                continue
            clean, errs = _sanitize_settings(v, default_node[k], path + "." + k if path else k)
            errors.extend(errs)
            if not errs:
                out[k] = clean
        return out, errors
    if isinstance(default_node, bool):
        if isinstance(node, bool):
            return node, []
        if isinstance(node, str):
            return node.strip().lower() in ("1", "true", "yes", "on", "是"), []
        return bool(node), []
    if isinstance(default_node, (int, float)):
        if isinstance(node, bool):
            return node, []
        try:
            if isinstance(default_node, int):
                return int(float(node)), []
            return float(node), []
        except (TypeError, ValueError):
            return None, ["%s 必须是数字，你填的是 %r" % (path, node)]
    return node, []


def h_settings_set(q, b):
    if not isinstance(b, dict):
        return 400, {"ok": False, "message": "请求体必须是 JSON 对象"}
    clean, errors = _sanitize_settings(b, config.DEFAULTS)
    if errors:
        return 200, {"ok": False, "message": "有字段不对，没有保存：%s" % "；".join(errors[:6]),
                     "errors": errors}
    cfg = config.update(clean)
    store.log("INFO", "settings", "配置已更新: %s" % ", ".join(list(b.keys())))
    return 200, {"ok": True, "config": cfg, "message": "设置已保存"}


def h_settings_reset(q, b):
    return 200, {"ok": True, "config": config.reset(), "message": "已恢复默认设置"}


def h_logs(q, b):
    limit = _i(q.get("limit"), DEFAULT_LIMIT)
    rows = store.recent_logs(limit, q.get("level") or None, q.get("source") or None)
    return 200, {"logs": rows}


def h_logs_clear(q, b):
    store.clear_logs()
    return 200, {"ok": True}


# ---------------------------------------------------------------------------
# 行情
# ---------------------------------------------------------------------------
def h_market_status(q, b):
    return 200, ds.market_status()


def h_market_indices(q, b):
    return 200, {"indices": ds.indices()}


def h_market_quote(q, b):
    syms = _sl(q.get("symbols")) or []
    if not syms and b:
        syms = _sl(b.get("symbols")) or []
    if not syms:
        return 400, {"ok": False, "message": "缺少 symbols 参数"}
    return 200, {"quotes": ds.quotes(syms[:80]), "requested": syms}


def h_market_kline(q, b):
    sym = q.get("symbol") or (b or {}).get("symbol")
    if not sym:
        return 400, {"ok": False, "message": "缺少 symbol"}
    period = q.get("period") or "daily"
    adjust = _i(q.get("adjust"), 1)
    limit = _i(q.get("limit"), 300)
    with_ind = str(q.get("indicators", "1")) not in ("0", "false", "False")
    ks = ds.kline(sym, period, adjust, limit)
    out = {"symbol": ds.normalize(sym), "period": period, "klines": ks, "count": len(ks)}
    if with_ind and ks:
        from . import indicators as ind
        out["indicators"] = ind.compute_set(ks)
    return 200, out


def h_market_minute(q, b):
    sym = q.get("symbol")
    if not sym:
        return 400, {"ok": False, "message": "缺少 symbol"}
    ks = ds.minute(sym)
    return 200, {"symbol": ds.normalize(sym), "klines": ks}


def h_market_search(q, b):
    kw = q.get("q") or ""
    return 200, {"results": ds.search(kw, _i(q.get("limit"), 20))}


def h_market_rank(q, b):
    """涨跌幅 / 成交额 / 换手率排行

    返回结构：{list, source, stale, message}
      source  eastmoney=东财主源 / sina=新浪备源 / snapshot=本地快照 / ""=全挂
      stale   数据是否为降级的旧数据（前端应提示用户）
    """
    r = ds.rank_with_meta(q.get("type") or "gainers", _i(q.get("limit"), 20),
                          force=(q.get("force") in ("1", "true", "True")))
    return 200, r


def h_watchlist(q, b):
    rows = store.list_watchlist()
    syms = [r["symbol"] for r in rows]
    try:
        qs = ds.quotes(syms) if syms else {}
    except Exception:
        qs = {}
    out = []
    for r in rows:
        item = dict(qs.get(r["symbol"]) or {"symbol": r["symbol"], "name": r["name"]})
        item["added_at"] = r["added_at"]
        out.append(item)
    return 200, {"watchlist": out}


def h_watchlist_set(q, b):
    b = b or {}
    sym = b.get("symbol")
    if not sym:
        return 400, {"ok": False, "message": "缺少 symbol"}
    action = b.get("action") or "add"
    s = ds.normalize(sym)
    if action == "remove":
        store.remove_watch(s)
        return 200, {"ok": True, "message": "已从自选删除"}
    name = ""
    try:
        name = (ds.quote(s) or {}).get("name", "")
    except Exception:
        pass
    store.add_watch(s, name)
    return 200, {"ok": True, "message": "已加入自选"}


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------
def h_templates(q, b):
    return 200, {"templates": st.all_templates()}


def h_strategies(q, b):
    return 200, {"strategies": store.list_strategies()}


def h_strategy_save(q, b):
    b = b or {}
    if not b.get("stype"):
        return 400, {"ok": False, "message": "必须选择策略类型"}
    if b["stype"] not in st.REGISTRY:
        return 400, {"ok": False, "message": "没有这个策略类型: %s" % b["stype"]}
    b["symbols"] = _sl(b.get("symbols")) or []
    b["symbols"] = [ds.normalize(x) for x in b["symbols"]]
    if not b["symbols"]:
        return 400, {"ok": False, "message": "至少要选一只股票"}
    if b.get("mode") not in ("signal", "auto"):
        b["mode"] = "signal"
    if not b.get("name"):
        b["name"] = st.REGISTRY[b["stype"]].name
    # 编辑已部署的策略时，保留它的部署状态和风控授权（不要因为改个参数就悄悄下线）
    if b.get("id"):
        old = store.get_strategy(int(b["id"])) or {}
        for k in ("deployed", "deployed_at", "direct_send", "capital_pct", "max_single_pct",
                  "max_order_amount", "stop_loss_pct", "take_profit_pct", "expires_at",
                  "symbol_weights"):
            if b.get(k) is None and old.get(k) is not None:
                b[k] = old.get(k)
    # 规范化权重：键转标准代码，值转正数，扔掉无效项
    if "symbol_weights" in b and b["symbol_weights"] is not None:
        sw = {}
        raw = b["symbol_weights"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    sym = ds.normalize(k)
                    wv = float(v)
                    if wv > 0:
                        sw[sym] = round(wv, 3)
                except Exception:
                    continue
        b["symbol_weights"] = sw
    sid = store.save_strategy(b)
    store.log("INFO", "strategy", "保存策略《%s》(%s) 股票 %s"
              % (b["name"], b["stype"], ",".join(b["symbols"])))
    return 200, {"ok": True, "id": sid, "strategy": store.get_strategy(sid)}


def h_strategy_delete(q, b):
    sid = _i((b or {}).get("id") or q.get("id"))
    if not sid:
        return 400, {"ok": False, "message": "缺少 id"}
    store.delete_strategy(sid)
    return 200, {"ok": True, "message": "已删除"}


def h_strategy_toggle(q, b):
    b = b or {}
    sid = _i(b.get("id"))
    s = store.get_strategy(sid)
    if not s:
        return 404, {"ok": False, "message": "策略不存在"}
    enabled = bool(b.get("enabled"))
    store.save_strategy(dict(s, enabled=enabled, id=sid))
    store.log("INFO", "strategy", "%s 策略《%s》" % ("启用" if enabled else "停用", s["name"]))
    return 200, {"ok": True, "strategy": store.get_strategy(sid)}


def h_strategy_preview(q, b):
    b = b or {}
    sid = _i(b.get("id") or q.get("id"))
    s = store.get_strategy(sid) if sid else None
    if not s and b.get("stype"):
        s = {"id": 0, "name": b.get("name") or "未命名", "stype": b["stype"],
             "params": b.get("params") or {}, "symbols": _sl(b.get("symbols")) or []}
    if not s:
        return 404, {"ok": False, "message": "策略不存在"}
    state = engine.evaluate(s)
    return 200, {"ok": True, "state": state, "strategy": s}


def h_strategy_run(q, b):
    b = b or {}
    sid = _i(b.get("id") or q.get("id"))
    s = store.get_strategy(sid)
    if not s:
        return 404, {"ok": False, "message": "策略不存在"}
    dry = bool(b.get("dry_run", True))
    br = manager.active()
    bal = br.balance()
    pos = br.positions()
    acts = engine.run_strategy(s, bal, pos, dry_run=dry)
    return 200, {"ok": True, "actions": acts, "dry_run": dry}


# ---------------------------------------------------------------------------
# 回测
# ---------------------------------------------------------------------------
def h_backtest_run(q, b):
    b = b or {}
    stype = b.get("stype") or b.get("strategy")
    if not stype:
        return 400, {"ok": False, "message": "缺少策略类型 stype"}
    if stype not in st.REGISTRY:
        return 400, {"ok": False, "message": "没有这个策略: %s" % stype}
    symbols = [ds.normalize(x) for x in (_sl(b.get("symbols")) or [])]
    if not symbols:
        return 400, {"ok": False, "message": "至少要选一只股票"}
    res = backtest_mod.run(
        stype, b.get("params") or {}, symbols,
        start=b.get("start") or "", end=b.get("end") or "",
        capital=_f(b.get("capital"), float(config.get("paper.initial_capital", 200000.0))),
        position_pct=_f(b["position_pct"]) if b.get("position_pct") not in (None, "") else None,
        bars=_i(b.get("bars"), 1200), save=True, name=b.get("name") or "",
        symbol_weights=(b.get("symbol_weights") or None),
        # 单日成交量参与率：默认 10%，机构建仓都控这个，防止把自己买成对手盘
        participation_rate=_f(b.get("participation_rate"), 0.10),
    )
    if not res.get("ok"):
        return 200, res
    return 200, res


def h_backtest_list(q, b):
    return 200, {"list": store.list_backtests(_i(q.get("limit"), 50))}

def h_backtest_walkforward(q, b):
    """滚动样本外（walk-forward）验证：用训练段选参数、样本外段检验，滚动前进"""
    b = b or {}
    stype = b.get("stype") or b.get("strategy")
    if not stype:
        return 400, {"ok": False, "message": "缺少策略类型 stype"}
    if stype not in st.REGISTRY:
        return 400, {"ok": False, "message": "没有这个策略: %s" % stype}
    symbols = [ds.normalize(x) for x in (_sl(b.get("symbols")) or [])]
    if not symbols:
        return 400, {"ok": False, "message": "至少要选一只股票"}
    res = backtest_mod.walk_forward(
        stype, b.get("params") or {}, symbols,
        start=b.get("start") or "", end=b.get("end") or "",
        capital=_f(b.get("capital"), float(config.get("paper.initial_capital", 200000.0))),
        position_pct=_f(b["position_pct"]) if b.get("position_pct") not in (None, "") else None,
        bars=_i(b.get("bars"), 1200),
        train_days=_i(b.get("train_days"), 252),
        test_days=_i(b.get("test_days"), 63),
        step_days=_i(b.get("step_days"), 63),
        participation_rate=_f(b.get("participation_rate"), 0.10),
        symbol_weights=(b.get("symbol_weights") or None),
    )
    return 200, res

def h_backtest_get(q, b):
    bid = _i(q.get("id"))
    row = store.get_backtest(bid)
    if not row:
        return 404, {"ok": False, "message": "找不到这个回测结果"}
    return 200, row


def h_backtest_delete(q, b):
    store.delete_backtest(_i((b or {}).get("id") or q.get("id")))
    return 200, {"ok": True}


# ---------------------------------------------------------------------------
# 券商
# ---------------------------------------------------------------------------
def h_broker_list(q, b):
    return 200, {"brokers": manager.all_status(), "active": manager.active_name,
                 "infos": BROKER_INFO}


def h_broker_status(q, b):
    return 200, manager.active().snapshot()


def h_broker_switch(q, b):
    b = b or {}
    name = b.get("name")
    if name not in ("paper", "ths", "qmt"):
        return 400, {"ok": False, "message": "未知通道"}
    res = manager.switch(name, b.get("cfg"), do_connect=bool(b.get("connect", True)))
    return (200 if res.get("ok") else 200), res


def h_broker_connect(q, b):
    b = b or {}
    name = b.get("name") or manager.active_name
    br = manager.instance(name)
    res = br.connect(b.get("cfg") or config.get("broker.%s" % name, {}))
    if res.get("ok") and name != manager.active_name:
        manager.switch(name, None, do_connect=False)
    return 200, dict(res, status=br.status())


def h_broker_disconnect(q, b):
    br = manager.active()
    return 200, dict(br.disconnect(), status=br.status())


def h_broker_check(q, b):
    name = (b or {}).get("name") or q.get("name") or manager.active_name
    br = manager.instance(name)
    if not hasattr(br, "check_env"):
        return 200, {"ok": True, "items": [{"name": "该通道无需额外配置", "ok": True, "detail": ""}]}
    try:
        return 200, br.check_env()
    except Exception as e:  # noqa: BLE001
        return 200, {"ok": False, "items": [{"name": "自检失败", "ok": False, "detail": str(e)[:200]}]}


# ---------------------------------------------------------------------------
# 账户 / 交易
# ---------------------------------------------------------------------------
def h_account(q, b):
    br = manager.active()
    out = {"status": br.status()}
    try:
        out["balance"] = br.balance()
    except Exception as e:  # noqa: BLE001
        out["balance"] = {"error": str(e)[:200]}
    return 200, out


def h_positions(q, b):
    br = manager.active()
    try:
        pos = br.positions()
    except Exception as e:  # noqa: BLE001
        return 200, {"positions": [], "error": str(e)[:200]}
    return 200, {"positions": pos}


def h_positions_overview(q, b):
    """分通道持仓：模拟盘 / 实盘 分开返回，供前端拆成两个独立视图。

    以前「当前持仓」只取 manager.active()（当前激活通道）的数据，用户切到
    实盘通道后，同一个面板就悄悄变成实盘持仓，很容易把模拟盘的浮盈当成真钱
    （或反过来）。拆开后两个视图各看各的、来源写清楚，不再混淆。
    """
    out = {
        "active": (manager.active().name if manager.active() else "paper"),
        "paper": [], "paper_ok": True,
        "live": [], "live_ok": True, "live_broker": None, "live_display": "",
        "live_connected": False,
    }
    # 模拟盘：本地虚拟引擎，永远是 paper
    try:
        out["paper"] = manager.instance("paper").positions()
    except Exception as e:  # noqa: BLE001
        out["paper_ok"] = False
        out["paper_error"] = str(e)[:120]
    # 实盘：优先取「已连接」的实盘通道（ths / qmt）；都没连，则标出当前激活的实盘通道名
    live_name = None
    for name in ("ths", "qmt"):
        inst = manager.instance(name)
        if getattr(inst, "is_live", False) and getattr(inst, "connected", False):
            live_name = name
            break
    if not live_name:
        act = manager.active()
        live_name = act.name if getattr(act, "is_live", False) else "ths"
    inst = manager.instance(live_name)
    out["live_broker"] = live_name
    out["live_display"] = getattr(inst, "display", live_name) or live_name
    out["live_connected"] = bool(getattr(inst, "connected", False))
    try:
        out["live"] = inst.positions() if out["live_connected"] else []
    except Exception as e:  # noqa: BLE001
        out["live_ok"] = False
        out["live_error"] = str(e)[:120]
    return 200, out


def h_orders(q, b):
    br = manager.active()
    try:
        rows = br.orders()
    except Exception as e:  # noqa: BLE001
        rows = store.list_orders(_i(q.get("limit"), DEFAULT_LIMIT))
    return 200, {"orders": rows[: _i(q.get("limit"), DEFAULT_LIMIT)]}


def h_trades(q, b):
    br = manager.active()
    try:
        rows = br.trades()
    except Exception:
        rows = store.list_trades(_i(q.get("limit"), DEFAULT_LIMIT))
    return 200, {"trades": rows[: _i(q.get("limit"), DEFAULT_LIMIT)]}


def h_equity(q, b):
    mode = q.get("mode") or ("live" if manager.active().is_live else "paper")
    return 200, {"curve": store.equity_curve(mode, _i(q.get("limit"), 500))}


def h_order_place(q, b):
    b = b or {}
    sym = b.get("symbol")
    side = (b.get("side") or "").lower()
    if not sym or side not in ("buy", "sell"):
        return 400, {"ok": False, "message": "需要 symbol 和 side(buy/sell)"}
    qty = _i(b.get("qty"))
    if qty <= 0:
        return 400, {"ok": False, "message": "数量必须大于 0"}
    price = b.get("price")
    price = _f(price) if price not in (None, "", 0, "0") else None
    order_type = b.get("order_type") or ("limit" if price else "market")
    reason = b.get("reason") or "手动下单"
    br = manager.active()
    if br.is_live and not config.get("live.enabled", False):
        return 200, {"ok": False, "status": "被拒绝",
                     "message": "当前是实盘通道，但实盘总开关没打开。请先到【实盘交易】页确认启用实盘。"}
    try:
        r = br.buy(sym, price=price, qty=qty, order_type=order_type, reason=reason) if side == "buy" \
            else br.sell(sym, price=price, qty=qty, order_type=order_type, reason=reason)
    except Exception as e:  # noqa: BLE001
        return 200, {"ok": False, "message": "下单异常: %s" % str(e)[:200]}
    return 200, dict(r)


def h_order_cancel(q, b):
    oid = _i((b or {}).get("order_id") or q.get("order_id"))
    if not oid:
        return 400, {"ok": False, "message": "缺少 order_id"}
    try:
        return 200, manager.active().cancel(oid)
    except Exception as e:  # noqa: BLE001
        return 200, {"ok": False, "message": str(e)[:200]}


def h_paper_reset(q, b):
    br = manager.instance("paper")
    cap = _f((b or {}).get("capital"), float(config.get("paper.initial_capital", 200000.0)))
    return 200, br.reset(cap)


def h_paper_max(q, b):
    sym = q.get("symbol")
    if not sym:
        return 400, {"ok": False, "message": "缺少 symbol"}
    br = manager.instance("paper")
    return 200, {"max_buy": br.max_buy_shares(sym), "sellable": br.sellable_shares(sym)}


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
def h_engine_status(q, b):
    return 200, engine.status()


def h_engine_start(q, b):
    br = manager.active()
    if br.is_live and not config.get("live.enabled", False):
        return 200, {"ok": False, "message": "当前是实盘通道，请先打开实盘总开关，再启动引擎。"}
    if not br.connected:
        return 200, {"ok": False, "message": "通道 %s 还没连接" % br.display}
    return 200, engine.start()


def h_engine_stop(q, b):
    return 200, engine.stop()


def h_engine_tick(q, b):
    try:
        return 200, engine.tick()
    except Exception as e:  # noqa: BLE001
        return 200, {"ok": False, "message": str(e)[:300],
                     "trace": traceback.format_exc()[-800:]}


def h_engine_panic(q, b):
    liquidate = bool((b or {}).get("liquidate", False))
    return 200, engine.panic(liquidate)


def h_live_enable(q, b):
    b = b or {}
    phrase = str(b.get("phrase") or "").strip()
    need = str(config.get("live.confirm_phrase", "我确认启用实盘"))
    if phrase != need:
        return 200, {"ok": False, "message": "确认口令不对。请一字不差地输入：%s" % need}
    br = manager.active()
    if not br.is_live:
        return 200, {"ok": False, "message": "当前通道是模拟盘，不需要开实盘开关（模拟盘本来就安全）。"}
    if not br.connected:
        return 200, {"ok": False, "message": "通道还没连接成功，先连接再开实盘。"}
    pf = live.preflight()
    if not pf["ready"]:
        return 200, {"ok": False, "preflight": pf,
                     "message": "还有关键项没通过，先把它们解决掉再开实盘：%s"
                                % "、".join(pf["critical_failed"])}
    config.set_("live.enabled", True)
    config.set_("live.enabled_at", store.now_str())
    store.log("WARN", "live", "【实盘已启用】通道 %s，操作时间 %s" % (br.display, store.now_str()))
    return 200, {"ok": True, "message": "实盘已启用。请务必保持警惕，随时可以急停。"}


def h_live_disable(q, b):
    config.set_("live.enabled", False)
    config.set_("live.enabled_at", "")
    store.log("WARN", "live", "实盘已关闭")
    return 200, {"ok": True, "message": "实盘已关闭，引擎不会再下真实委托"}


# ---------------------------------------------------------------------------
# 因子实验室
# ---------------------------------------------------------------------------
def h_factors(q, b):
    return 200, {
        "factors": fac.list_factors(),
        "families": fac.FAMILIES,
        "presets": fac.list_presets(),
        "default_pool": lab.DEFAULT_POOL,
        "count": len(fac.FACTORS),
    }


def h_factor_detail(q, b):
    key = q.get("key") or (b or {}).get("key")
    meta = fac.FACTORS.get(key)
    if not meta:
        return 404, {"ok": False, "message": "没有这个因子: %s" % key}
    return 200, {"key": key, "name": meta["name"], "family": meta["family"],
                 "desc": meta["desc"], "direction": meta["direction"]}


def h_factor_analyze(q, b):
    b = b or {}
    key = b.get("factor") or q.get("factor")
    if not key:
        return 400, {"ok": False, "message": "缺少 factor 参数"}
    symbols = _sl(b.get("symbols")) or lab.DEFAULT_POOL[:12]
    try:
        res = lab.analyze(key, symbols, days=_i(b.get("days"), 400),
                          horizon=_i(b.get("horizon"), 5),
                          neutralize=(b.get("neutralize") or "none"))
    except Exception as e:  # noqa: BLE001
        return 200, {"ok": False, "message": "分析失败: %s" % str(e)[:250]}
    return 200, res


def h_factor_scan_start(q, b):
    b = b or {}
    symbols = _sl(b.get("symbols")) or lab.DEFAULT_POOL
    res = lab.start_scan(symbols, _i(b.get("days"), 300), _i(b.get("horizon"), 5),
                         bool(b.get("force")), b.get("neutralize") or "none")
    return 200, res


def h_factor_scan_status(q, b):
    return 200, lab.scan_status()


def h_factor_composite(q, b):
    """给一组股票用指定因子组合打分，直接看看今天该关注谁"""
    b = b or {}
    # 用户手填的池子里可能有写错的代码，别让它把整个请求打成 500
    symbols, bad = [], []
    for s in (_sl(b.get("symbols")) or lab.DEFAULT_POOL[:15]):
        try:
            symbols.append(ds.normalize(s))
        except Exception:  # noqa: BLE001
            bad.append(str(s))
    weights = st_mod.parse_combo(b.get("preset") or "", b.get("custom") or "")
    if not weights:
        return 400, {"ok": False, "message": "没有解析出任何因子，请检查 preset / custom"}
    from . import factors as _fac
    rows = []
    for s in symbols:
        try:
            ks = ds.kline(s, "daily", 1, 400)
            ks = [k for k in ks if k.get("close")]
            # ts_rank 要 120 根才有值，所以至少要 130 根；以前卡 200 根，
            # 数据稍短（比如降级用了不复权源）就整只被跳过，表现就是「一只都排不上」。
            if len(ks) < 130:
                continue
            comp = lab.composite(ks, weights, "rank")
            sc = _fac.ts_rank(comp, 120)
            cur = sc[-1]
            if cur is None:
                continue
            rows.append({"symbol": s, "name": ks[-1].get("name") or s,
                         "score": round(cur, 4), "close": ks[-1]["close"],
                         "date": ks[-1]["date"][:10]})
        except Exception:
            continue
    rows.sort(key=lambda x: x["score"], reverse=True)
    out = {"ok": True, "weights": weights, "ranking": rows}
    if bad:
        out["skipped"] = bad[:10]
    return 200, out


def h_factor_correlation(q, b):
    """检测一组因子两两相关性，找出冗余因子，并给出去重建议"""
    b = b or {}
    keys = _sl(b.get("factors")) or []
    if not keys:
        # 没传因子就默认检查「动量/反转」家族的几个常见因子
        keys = ["mom_5", "mom_10", "mom_20", "mom_60", "rev_1", "rev_5"]
    symbols = _sl(b.get("symbols")) or lab.DEFAULT_POOL[:20]
    res = lab.factor_correlation(keys, symbols, days=_i(b.get("days"), 400),
                                 threshold=_f(b.get("threshold"), 0.7))
    return 200, res


# ---------------------------------------------------------------------------
# 深度学习因子（AI 自动挖掘）
# ---------------------------------------------------------------------------
def h_dlfactor_train(q, b):
    """训练深度学习因子：task=cross_section(选股) / time_series(择时)"""
    b = b or {}
    task = b.get("task") or "cross_section"
    if task not in ("cross_section", "time_series"):
        return 400, {"ok": False, "message": "task 只能是 cross_section 或 time_series"}
    symbols = _sl(b.get("symbols")) or []
    days = _i(b.get("days"), 400)
    horizon = _i(b.get("horizon"), 5)
    population = _i(b.get("population"), 8)
    generations = _i(b.get("generations"), 3)
    r = dlf.train(symbols, days, horizon, task, population, generations)
    return (200 if r.get("ok") else 400), r


def h_dlfactor_status(q, b):
    return 200, dlf.status()


def h_dlfactor_result(q, b):
    r = dlf.get_result()
    if not r:
        return 200, {"ok": False, "message": "还没有训练过模型"}
    return 200, r


def h_dlfactor_preview(q, b):
    """用已训练的模型给股票池打当前分，看看今天 AI 因子看好谁"""
    symbols = _sl(q.get("symbols")) or lab.DEFAULT_POOL[:15]
    task = q.get("task") or "cross_section"
    if task not in ("cross_section", "time_series"):
        return 400, {"ok": False, "message": "task 只能是 cross_section 或 time_series"}
    if dlf.get_model(task) is None:
        return 200, {"ok": False, "message": "该任务还没训练过模型，请先 POST /api/dlfactor/train"}
    rows = []
    for s in [ds.normalize(x) for x in symbols]:
        try:
            ks = ds.kline(s, "daily", 1, 400)
            ks = [k for k in ks if k.get("close")]
            if len(ks) < 80:
                continue
            scores = dlf.ai_factor_score(ks, task)
            cur = scores[-1]
            if cur is None:
                continue
            rows.append({"symbol": s, "name": ks[-1].get("name") or s,
                         "score": round(cur, 4), "close": ks[-1]["close"],
                         "date": ks[-1]["date"][:10]})
        except Exception:
            continue
    rows.sort(key=lambda x: x["score"], reverse=True)
    return 200, {"ok": True, "task": task, "ranking": rows}


# ---------------------------------------------------------------------------
# 组合优化
# ---------------------------------------------------------------------------
def h_optimizer_methods(q, b):
    return 200, {"methods": opt.METHODS}


def h_optimizer_run(q, b):
    b = b or {}
    symbols = [ds.normalize(s) for s in (_sl(b.get("symbols")) or lab.DEFAULT_POOL[:12])]
    method = b.get("method") or "risk_parity"
    weights = st_mod.parse_combo(b.get("preset") or "preset_momentum", b.get("custom") or "")
    if not weights:
        return 400, {"ok": False, "message": "没有解析出任何因子"}
    panel = lab.load_panel(symbols, 300)
    if not panel:
        return 200, {"ok": False, "message": "没取到行情数据"}
    rets, scores, names = {}, {}, {}
    for sym, ks in panel.items():
        c = [k["close"] for k in ks]
        rets[sym] = [c[i] / c[i - 1] - 1.0 for i in range(1, len(c)) if c[i - 1]]
        names[sym] = ks[-1].get("name") or sym
        comp = lab.composite(ks, weights, "rank")
        sc = fac.ts_rank(comp, 120)
        scores[sym] = sc[-1] if sc[-1] is not None else 0.0
    order = sorted(panel.keys(), key=lambda s: -scores.get(s, 0.0))
    # 机构级硬约束：行业中性 / 流动性 / 换手（0 或缺省 = 关闭）
    max_ind = _f(b.get("max_industry_pct"), 0.0)
    max_adv = _f(b.get("max_adv_pct"), 0.0)
    max_to = _f(b.get("max_turnover_pct"), 0.0)
    capital = _f(b.get("capital"), float(config.get("paper.initial_capital", 200000.0)))
    ind_map = {s: ds.industry_of(s) or "其他" for s in panel} if max_ind > 0 else None
    adv_map = None
    if max_adv > 0:
        adv_map = {}
        for sym, ks in panel.items():
            amts = []
            for k in ks[-20:]:
                a = k.get("amount")
                if not a:
                    a = (k.get("close") or 0) * (k.get("volume") or 0) * 100.0
                if a:
                    amts.append(float(a))
            adv_map[sym] = sum(amts) / len(amts) if amts else 0.0
    cur_w = b.get("current_weights") or None
    res = opt.optimize(method, order, rets, scores,
                       total_pct=_f(b.get("total_pct"), 90.0),
                       max_single_pct=_f(b.get("max_single_pct"), 20.0),
                       industry_map=ind_map, max_industry_pct=max_ind if max_ind > 0 else None,
                       adv_map=adv_map, max_adv_pct=max_adv if max_adv > 0 else None,
                       capital=capital if max_adv > 0 else None,
                       current_weights=cur_w, max_turnover_pct=max_to if max_to > 0 else None)
    if not res.get("ok"):
        return 200, res
    rows = []
    for sym in res["symbols"]:
        rows.append({"symbol": sym, "name": names.get(sym, sym),
                     "industry": ds.industry_of(sym),
                     "score": round(scores.get(sym, 0.0), 4),
                     "weight_pct": res["weights"][sym],
                     "risk_contribution_pct": res["risk_contribution"].get(sym)})
    rows.sort(key=lambda x: -x["weight_pct"])
    return 200, dict(res, rows=rows, factor_weights=weights)


# ---------------------------------------------------------------------------
# AI 选股助手
# ---------------------------------------------------------------------------
def h_assistant_status(q, b):
    from . import assistant as ast
    cfg = config.get("assistant", {}) or {}
    return 200, {"enabled": bool(cfg.get("enabled")), "has_key": bool(cfg.get("api_key")),
                 "model": cfg.get("model"), "base_url": cfg.get("base_url"),
                 "mode": "大模型润色" if (cfg.get("enabled") and cfg.get("api_key")) else "本地模式（无需 API Key）"}


def h_assistant_ask(q, b):
    from . import assistant as ast
    question = (b or {}).get("question") or q.get("question") or ""
    if not question.strip():
        return 400, {"ok": False, "message": "请先输入问题"}
    return 200, ast.ask(question)


def h_assistant_picks(q, b):
    from . import assistant as ast
    b = b or {}
    top_n = _i(b.get("top_n") or q.get("top_n"), 10)
    pool = _sl(b.get("symbols"))
    return 200, ast.daily_picks(top_n, pool, refresh=bool(b.get("refresh")))


def h_assistant_test(q, b):
    from . import assistant as ast
    return 200, ast.test_llm()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 自动选股
# ---------------------------------------------------------------------------
def h_screener_presets(q, b):
    snap = scr.snapshot()
    return 200, {
        "ok": True,
        "presets": scr.presets(),
        "factors": scr.factors_list(),
        "default_filters": dict(scr.DEFAULT_FILTERS),
        "snapshot": snap["meta"],
    }


def h_screener_status(q, b):
    snap = scr.snapshot()
    return 200, {"ok": True, "snapshot": snap["meta"]}


def h_screener_refresh(q, b):
    snap = scr.snapshot(force=True)
    m = snap["meta"]
    if m.get("stale"):
        # 别再报「已重新抓取」——这次可能一只都没抓到，全是旧缓存
        msg = "行情源暂时不可用，已沿用旧数据：%d 只（数据时间 %s）" % (
            m.get("count", 0), m.get("fetched_at", "") or "未知")
    else:
        msg = "已重新抓取全市场行情：%d 只，%s" % (m.get("count", 0), m.get("fetched_at", ""))
    return 200, {"ok": True, "snapshot": m, "message": msg}


def h_screener_run(q, b):
    b = b or {}
    preset = str(b.get("preset") or "multi_factor")
    if preset not in scr.PRESETS:
        return 200, {"ok": False, "message": "没有这个选股模板：%s" % preset}
    top_n = max(1, min(_i(b.get("top_n"), 20), 100))
    tech_pool = max(1, min(_i(b.get("tech_pool"), 3), 8))

    flt = dict(scr.DEFAULT_FILTERS)
    user_flt = b.get("filters")
    if isinstance(user_flt, dict):
        for k, v in user_flt.items():
            if k in scr.DEFAULT_FILTERS:
                flt[k] = v
    for k in ("min_amount", "max_amount", "min_mktcap", "max_mktcap", "min_price", "max_price"):
        try:
            flt[k] = float(flt.get(k) or 0)
        except (TypeError, ValueError):
            return 200, {"ok": False, "message": "筛选条件 %s 必须是数字" % k}
    try:
        flt["min_listed_days"] = int(float(flt.get("min_listed_days") or 0))
    except (TypeError, ValueError):
        return 200, {"ok": False, "message": "上市天数必须是数字"}
    if not isinstance(flt.get("boards"), list) or not flt["boards"]:
        return 200, {"ok": False, "message": "至少要勾一个板块"}

    weights = b.get("weights") if isinstance(b.get("weights"), dict) else None
    if weights:
        clean = {}
        for k, v in weights.items():
            if k in scr.SCREEN_FACTORS:
                try:
                    fv = float(v)
                    if fv != 0:
                        clean[k] = fv
                except (TypeError, ValueError):
                    continue
        weights = clean or None

    res = scr.run(preset=preset, filters=flt, top_n=top_n, weights=weights,
                  neutralize_industry=bool(b.get("neutralize_industry")),
                  with_tech=bool(b.get("with_tech", True)), tech_pool=tech_pool,
                  with_fundamental=bool(b.get("with_fundamental", True)))
    if res.get("ok"):
        store.log("INFO", "screener",
                  "选股完成：模板=%s 全市场%s只->粗筛%s只->选出%d只，耗时%.1fs"
                  % (preset, res.get("universe", 0), res.get("after_filter", 0),
                     len(res.get("picked", [])), res.get("elapsed", 0)))
    return 200, res


# ---------------------------------------------------------------------------
# 基本面增强（可选，不参与回测主链路）
# ---------------------------------------------------------------------------
def _split_syms(raw) -> list:
    """把 '600519.SH,000001.SZ' / ['600519.SH'] 统一成去重列表"""
    if isinstance(raw, str):
        s = raw.replace("，", ",").replace(";", ",").replace("；", ",")
        s = s.replace("\n", ",").replace("\t", ",").replace(" ", ",")
        raw = [x for x in s.split(",") if x.strip()]
    if not isinstance(raw, list):
        return []
    out = []
    for x in raw:
        sym = fnd.normalize(x)
        if sym and sym not in out:
            out.append(sym)
    return out


def h_fundamental_status(q, b):
    """基本面增强状态：令牌是否可用、缓存了多少只、最近一次错误"""
    return 200, fnd.status()


def h_fundamental_fetch(q, b):
    """抓取指定股票的基本面（估值分位 / 申万行业 / 分红）。

    会联网，且接口有全局节流（约 0.25 秒/只）。单次最多 20 只，避免点一下等半天。
    """
    raw = b.get("symbols") if isinstance(b, dict) else None
    if raw is None and isinstance(b, dict):
        raw = b.get("symbol")
    syms = _split_syms(raw)
    if not syms:
        return 200, {"ok": False, "message": "至少要给一个有效股票代码，如 600519.SH"}
    if len(syms) > 20:
        return 200, {"ok": False,
                     "message": "一次最多抓 20 只（现在 %d 只）。接口有节流，多了会很慢。" % len(syms)}
    force = bool((b or {}).get("force")) if isinstance(b, dict) else False
    items = [fnd.get_all(s, force=force) for s in syms]
    return 200, {"ok": True, "items": items, "note": fnd.last_error()}


def h_fundamental_industry(q, b):
    """批量查行业名（只读缓存，绝不联网），给前端列表展示用"""
    raw = b.get("symbols") if isinstance(b, dict) else None
    syms = _split_syms(raw)
    out = {}
    for s in syms[:500]:
        out[s] = fnd.industry_name_cached(s)
    return 200, {"ok": True, "industries": out}


def h_fundamental_clear(q, b):
    """清空基本面缓存"""
    return 200, {"ok": True, "removed": fnd.clear_cache()}


def h_screener_to_strategy(q, b):
    """把选股结果一键变成策略：股票池 + 权重都填好"""
    b = b or {}
    picks = b.get("picks")
    if not isinstance(picks, list) or not picks:
        return 200, {"ok": False, "message": "还没有选股结果，先点【开始选股】"}
    symbols, weights = [], {}
    for x in picks:
        sym = x.get("symbol") if isinstance(x, dict) else str(x)
        if not sym:
            continue
        try:
            sym = ds.normalize(sym)
        except Exception:
            continue
        if sym in symbols:
            continue
        symbols.append(sym)
        try:
            wv = float(x.get("final")) if isinstance(x, dict) else 0.0
            if wv > 0:
                weights[sym] = wv
        except (TypeError, ValueError):
            pass
    if not symbols:
        return 200, {"ok": False, "message": "选股结果里没有有效代码"}
    if len(symbols) > 60:
        symbols = symbols[:60]
        weights = {k: v for k, v in weights.items() if k in symbols}

    # 按得分分配权重，再归一到 100，单只不超过 25%
    tot = sum(weights.values())
    if tot > 0:
        weights = {k: round(min(v / tot * 100.0, 25.0), 2) for k, v in weights.items()}

    stype = str(b.get("stype") or "factor_rank")
    if stype not in st.REGISTRY:
        stype = "factor_rank"
    params = b.get("params") if isinstance(b.get("params"), dict) else {}
    if stype == "factor_rank" and not params:
        params = {"preset": "preset_momentum", "top_k": min(len(symbols), 5),
                  "rebalance_days": 10, "weight_mode": "manual"}
    name = str(b.get("name") or "").strip() or ("自动选股·%s" % (b.get("preset_name") or ""))
    sid = store.save_strategy({
        "name": name[:40], "stype": stype, "params": params,
        "symbols": symbols, "symbol_weights": weights,
        "mode": "auto", "enabled": False, "note": "由自动选股生成",
    })
    store.log("INFO", "screener", "选股结果已生成策略 #%d《%s》，%d 只股票" % (sid, name, len(symbols)))
    return 200, {"ok": True, "id": sid, "symbols": symbols, "weights": weights,
                 "strategy": store.get_strategy(sid),
                 "message": "已生成策略《%s》，%d 只股票。去【策略】页确认后再部署。"
                            % (name, len(symbols))}


def h_screener_watch(q, b):
    """把选股结果加进自选"""
    b = b or {}
    picks = b.get("picks")
    if not isinstance(picks, list) or not picks:
        return 200, {"ok": False, "message": "还没有选股结果，先点【开始选股】"}
    n = 0
    errs = []
    for x in picks:
        sym = x.get("symbol") if isinstance(x, dict) else str(x)
        if not sym:
            continue
        try:
            sym = ds.normalize(sym)
        except Exception:
            continue
        try:
            # 注意 add_watch 只接受 (symbol, name) 两个参数
            store.add_watch(sym, (x.get("name") if isinstance(x, dict) else "") or "")
            n += 1
        except Exception as e:  # noqa: BLE001
            errs.append("%s: %s" % (sym, str(e)[:60]))
    if n == 0:
        return 200, {"ok": False, "count": 0,
                     "message": "没有可以加入自选的股票" + ("：%s" % "；".join(errs[:3]) if errs else "")}
    return 200, {"ok": True, "count": n,
                 "message": "已加入自选 %d 只%s" % (n, ("，%d 只失败" % len(errs)) if errs else "")}


# 策略部署（一次性授权，之后全自动）
# ---------------------------------------------------------------------------
def _deploy_preview(s: dict, bal: dict) -> dict:
    total = float(bal.get("total_asset") or 0)
    cap = total * float(s.get("capital_pct") or 0) / 100.0
    return {
        "total_asset": round(total, 2),
        "capital_amount": round(cap, 2),
        "per_order_amount": round(float(s.get("max_order_amount") or 0), 2),
        "max_single_pct": float(s.get("max_single_pct") or 0),
        "stop_loss_pct": float(s.get("stop_loss_pct") or 0),
        "symbol_count": len(s.get("symbols") or []),
    }


def h_strategy_deploy(q, b):
    b = b or {}
    sid = _i(b.get("id") or q.get("id"))
    s = store.get_strategy(sid)
    if not s:
        return 404, {"ok": False, "message": "策略不存在"}
    if not s["symbols"]:
        return 400, {"ok": False, "message": "这个策略没有股票池"}
    if s["stype"] not in st.REGISTRY:
        return 400, {"ok": False, "message": "策略类型无效"}

    # 授权必须要过风控这一关
    if not config.get("risk.enabled", True):
        return 200, {"ok": False, "message": "全局风控被关掉了。部署自动交易前请先在【设置】里打开风控。"}

    stop = _f(b.get("stop_loss_pct"), -1)
    if stop <= 0:
        return 200, {"ok": False,
                     "message": "必须设置止损（大于 0）。自动交易没有止损等于把风险敞口全开。"}

    br = manager.active()
    if br.is_live and not config.get("live.enabled", False):
        return 200, {"ok": False,
                     "message": "当前是实盘通道，但实盘总开关没打开。请先在【实盘交易】页确认启用实盘。"}

    phrase = str(b.get("phrase") or "").strip()
    direct = bool(b.get("direct_send", True))
    need = "我确认自动交易" if direct else "我确认托管交易"
    if phrase != need:
        return 200, {"ok": False, "phrase_required": need,
                     "message": "请一字不差地输入确认口令：%s" % need}

    days = _i(b.get("expires_days"), 30)
    exp = ""
    if days > 0:
        exp = (dt.datetime.now() + dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    patch = dict(s, id=sid,
                 deployed=True, deployed_at=store.now_str(), expires_at=exp,
                 direct_send=direct,
                 capital_pct=_f(b.get("capital_pct"), 30.0),
                 max_single_pct=_f(b.get("max_single_pct"), config.get("risk.max_single_position_pct", 20.0)),
                 max_order_amount=_f(b.get("max_order_amount"), config.get("risk.max_order_amount", 50000.0)),
                 stop_loss_pct=stop,
                 take_profit_pct=_f(b.get("take_profit_pct"), 0.0))
    store.save_strategy(patch)
    s2 = store.get_strategy(sid)
    store.log("WARN", "strategy",
              "【策略已部署】《%s》%s，资金额度 %.0f%%，止损 %.1f%%，%s，到期 %s"
              % (s2["name"], "全自动直发" if direct else "生成待确认单",
                 s2["capital_pct"], s2["stop_loss_pct"], br.display, exp or "长期"))
    return 200, {"ok": True, "message": "策略《%s》已部署%s" % (s2["name"], "（全自动）" if direct else "（待确认）"),
                 "strategy": s2, "preview": _deploy_preview(s2, br.balance() if br.connected else {})}


def h_strategy_undeploy(q, b):
    sid = _i((b or {}).get("id") or q.get("id"))
    s = store.get_strategy(sid)
    if not s:
        return 404, {"ok": False, "message": "策略不存在"}
    store.save_strategy(dict(s, id=sid, deployed=False))
    store.log("WARN", "strategy", "【策略已下线】《%s》" % s["name"])
    return 200, {"ok": True, "message": "策略《%s》已下线，引擎不会再为它下单" % s["name"],
                 "strategy": store.get_strategy(sid)}


def _today() -> str:
    return store.now_str()[:10]


def h_strategy_runtime(q, b):
    br = manager.active()
    try:
        bal = br.balance()
        positions = br.positions()
    except Exception:
        bal, positions = {}, []
    pmap = {ds.normalize(p.get("symbol") or ""): p for p in positions}
    today = _today()
    tk = [t for t in store.list_tickets(None, 500) if str(t.get("created_at", "")).startswith(today)]
    od = [o for o in store.list_orders(500) if str(o.get("ts", "")).startswith(today)]
    total_asset = float(bal.get("total_asset") or 0)
    out = []
    for s in store.list_strategies():
        syms = [ds.normalize(x) for x in (s.get("symbols") or [])]
        held = [pmap[x] for x in syms if x in pmap]
        mv = sum(float(p.get("market_value") or 0) for p in held)
        pnl = sum(float(p.get("pnl") or 0) for p in held)
        exp = str(s.get("expires_at") or "")
        # 这个策略的资金额度（用来解释「为什么只买了 2 只」）
        cap_pct = float(s.get("capital_pct") or 100.0)
        cap_value = total_asset * cap_pct / 100.0
        out.append({
            "id": s["id"], "name": s["name"], "stype": s["stype"],
            # 这几个必须有：前端点【编辑】时要靠它们回填表单，
            # 少了就会用默认值覆盖用户辛苦调好的参数（数据丢失）
            "params": s.get("params") or {},
            "mode": s.get("mode", "signal"),
            "note": s.get("note", ""),
            "symbol_weights": s.get("symbol_weights") or {},
            "enabled": s["enabled"], "deployed": s["deployed"],
            "direct_send": s.get("direct_send", True),
            "deployed_at": s.get("deployed_at", ""), "expires_at": exp,
            "expired": bool(exp and exp < store.now_str()),
            "capital_pct": s.get("capital_pct"), "stop_loss_pct": s.get("stop_loss_pct"),
            "take_profit_pct": s.get("take_profit_pct"),
            "max_single_pct": s.get("max_single_pct"),
            "max_order_amount": s.get("max_order_amount"),
            "symbols": s.get("symbols") or [],
            "holding_count": len(held), "market_value": round(mv, 2), "pnl": round(pnl, 2),
            # 持仓明细：前端直接摊开给用户看每只票买了多少、成本多少、盈亏多少
            "holdings": [{
                "symbol": p.get("symbol"), "name": p.get("name") or p.get("symbol"),
                "qty": int(p.get("qty") or 0),
                "available": int(p.get("available") or 0),
                "avg_cost": p.get("avg_cost"), "last_price": p.get("last_price"),
                "market_value": p.get("market_value"), "pnl": p.get("pnl"),
                "pnl_pct": p.get("pnl_pct"), "open_date": p.get("open_date"),
            } for p in held],
            # 额度使用情况：额度买满了就没法继续加仓，这是「只持仓 2 只」的最常见原因
            "capital_value": round(cap_value, 2),
            "capital_used": round(mv, 2),
            "capital_left": round(cap_value - mv, 2),
            "last_run": s.get("last_run", ""), "last_signal": s.get("last_signal", ""),
            "last_action_at": s.get("last_action_at", ""),
            "today_tickets": len([t for t in tk if t.get("strategy_id") == s["id"]]),
            "today_orders": len([o for o in od if o.get("strategy_id") == s["id"]]),
        })
    out.sort(key=lambda x: (not x["deployed"], -(x["market_value"] or 0)))
    return 200, {
        "strategies": out,
        "deployed_count": len([x for x in out if x["deployed"]]),
        "engine_running": __import__("app.engine", fromlist=["x"]).engine.running,
        "broker": br.display, "is_live": br.is_live,
        "live_enabled": bool(config.get("live.enabled")),
        "balance": bal,
    }


# ---------------------------------------------------------------------------
# 实盘操作
# ---------------------------------------------------------------------------
def h_live_preflight(q, b):
    return 200, live.preflight()


def h_tickets(q, b):
    return 200, live.list_tickets(q.get("status") or None)


def h_ticket_create(q, b):
    b = b or {}
    sym = b.get("symbol")
    side = (b.get("side") or "").lower()
    qty = _i(b.get("qty"))
    if not sym or side not in ("buy", "sell"):
        return 400, {"ok": False, "message": "需要 symbol 和 side(buy/sell)"}
    if qty <= 0:
        return 400, {"ok": False, "message": "数量必须大于 0"}
    price = b.get("price")
    price = _f(price) if price not in (None, "", 0, "0") else None
    tid = live.make_ticket("手动", sym, side, qty, price, reason=b.get("reason") or "手动生成委托单",
                           order_type=b.get("order_type") or ("limit" if price else "market"))
    return 200, {"ok": True, "ticket_id": tid, "message": "已生成待确认委托单 #%d" % tid}


def h_ticket_send(q, b):
    b = b or {}
    if b.get("all"):
        res = live.send_many(all_pending=True)
    elif b.get("ids"):
        res = live.send_many(ids=b["ids"])
    else:
        tid = _i(b.get("id") or q.get("id"))
        if not tid:
            return 400, {"ok": False, "message": "缺少 id 或 all"}
        r = live.send_ticket(tid, force=bool(b.get("force")))
        res = dict(r, results=[{"id": tid, "ok": bool(r.get("ok")),
                                "message": r.get("message"), "symbol": r.get("symbol")}])
    return 200, res


def h_ticket_ignore(q, b):
    tid = _i((b or {}).get("id") or q.get("id"))
    if not tid:
        return 400, {"ok": False, "message": "缺少 id"}
    return 200, live.ignore_ticket(tid)


def h_ticket_clear(q, b):
    return 200, live.clear_tickets(all_of_them=bool((b or {}).get("all")))


def h_live_stops(q, b):
    return 200, live.stop_monitor()


def h_live_stop_all(q, b):
    b = b or {}
    return 200, live.stop_all(_sl(b.get("symbols")), bool(b.get("dry_run")))


def h_live_sync(q, b):
    return 200, live.sync_orders()


def h_report_daily(q, b):
    return 200, live.daily_report()


# ---------------------------------------------------------------------------
# 券商凭据（账号密码）
# ---------------------------------------------------------------------------
def h_credential_get(q, b):
    name = q.get("name") or (b or {}).get("name") or "ths"
    cfg = config.get("broker.%s" % name, {}) or {}
    return 200, {
        "name": name,
        "username": cfg.get("username", ""),
        "auto_login": bool(cfg.get("auto_login")),
        "has_password": secret.has(name + "_password"),
        "encryption_available": secret.available(),
        "note": "密码用 Windows 系统加密（DPAPI）保存，只有这台电脑、这个 Windows 账号能解开，"
                "接口永远不会把密码返回给页面。",
    }


def h_credential_set(q, b):
    b = b or {}
    name = b.get("name") or "ths"
    patch = {}
    if "username" in b:
        patch["username"] = str(b.get("username") or "").strip()
    if "auto_login" in b:
        patch["auto_login"] = bool(b.get("auto_login"))
    if patch:
        config.update({"broker": {name: patch}})
    pwd = b.get("password")
    if pwd:
        r = secret.save(name + "_password", str(pwd))
        if not r.get("ok"):
            return 200, {"ok": False, "message": r["message"]}
        store.log("INFO", "secret", "已保存 %s 的登录密码（系统加密）" % name)
        return 200, {"ok": True, "message": "账号密码已保存。" + r["message"]}
    return 200, {"ok": True, "message": "已保存账号设置（没有修改密码）"}


def h_credential_clear(q, b):
    name = (b or {}).get("name") or q.get("name") or "ths"
    r = secret.clear(name + "_password")
    config.update({"broker": {name: {"auto_login": False}}})
    store.log("WARN", "secret", "已删除 %s 的登录密码" % name)
    return 200, r


# ---------------------------------------------------------------------------
# 路由表
# ---------------------------------------------------------------------------
ROUTES = {
    ("GET", "/api/health"): h_health,
    ("GET", "/api/about"): h_about,
    ("GET", "/api/bootstrap"): h_bootstrap,
    ("GET", "/api/settings"): h_settings_get,
    ("POST", "/api/settings"): h_settings_set,
    ("POST", "/api/settings/reset"): h_settings_reset,
    ("GET", "/api/logs"): h_logs,
    ("POST", "/api/logs/clear"): h_logs_clear,

    ("GET", "/api/market/status"): h_market_status,
    ("GET", "/api/market/indices"): h_market_indices,
    ("GET", "/api/market/quote"): h_market_quote,
    ("GET", "/api/market/kline"): h_market_kline,
    ("GET", "/api/market/minute"): h_market_minute,
    ("GET", "/api/market/search"): h_market_search,
    ("GET", "/api/market/rank"): h_market_rank,
    ("GET", "/api/watchlist"): h_watchlist,
    ("POST", "/api/watchlist"): h_watchlist_set,

    ("GET", "/api/factors"): h_factors,
    ("GET", "/api/factors/detail"): h_factor_detail,
    ("POST", "/api/factors/analyze"): h_factor_analyze,
    ("POST", "/api/factors/scan"): h_factor_scan_start,
    ("GET", "/api/factors/scan"): h_factor_scan_status,
    ("POST", "/api/factors/composite"): h_factor_composite,
    ("POST", "/api/factors/correlation"): h_factor_correlation,

    ("POST", "/api/dlfactor/train"): h_dlfactor_train,
    ("GET", "/api/dlfactor/status"): h_dlfactor_status,
    ("GET", "/api/dlfactor/result"): h_dlfactor_result,
    ("GET", "/api/dlfactor/preview"): h_dlfactor_preview,

    ("GET", "/api/optimizer/methods"): h_optimizer_methods,
    ("POST", "/api/optimizer/run"): h_optimizer_run,

    ("GET", "/api/live/preflight"): h_live_preflight,
    ("GET", "/api/tickets"): h_tickets,
    ("POST", "/api/tickets/create"): h_ticket_create,
    ("POST", "/api/tickets/send"): h_ticket_send,
    ("POST", "/api/tickets/ignore"): h_ticket_ignore,
    ("POST", "/api/tickets/clear"): h_ticket_clear,
    ("GET", "/api/live/stops"): h_live_stops,
    ("POST", "/api/live/stop-all"): h_live_stop_all,
    ("POST", "/api/live/sync"): h_live_sync,
    ("GET", "/api/report/daily"): h_report_daily,
    ("GET", "/api/broker/credential"): h_credential_get,
    ("POST", "/api/broker/credential"): h_credential_set,
    ("POST", "/api/broker/credential/clear"): h_credential_clear,

    ("GET", "/api/assistant/status"): h_assistant_status,
    ("POST", "/api/assistant/ask"): h_assistant_ask,
    ("POST", "/api/assistant/picks"): h_assistant_picks,
    ("POST", "/api/assistant/test"): h_assistant_test,

    ("GET", "/api/strategies/templates"): h_templates,
    ("GET", "/api/strategies"): h_strategies,
    ("POST", "/api/strategies"): h_strategy_save,
    ("DELETE", "/api/strategies"): h_strategy_delete,
    ("POST", "/api/strategies/toggle"): h_strategy_toggle,
    ("GET", "/api/screener/presets"): h_screener_presets,
    ("GET", "/api/screener/status"): h_screener_status,
    ("POST", "/api/screener/refresh"): h_screener_refresh,
    ("POST", "/api/screener/run"): h_screener_run,
    ("POST", "/api/screener/to-strategy"): h_screener_to_strategy,
    ("POST", "/api/screener/to-watch"): h_screener_watch,
    ("GET", "/api/fundamental/status"): h_fundamental_status,
    ("POST", "/api/fundamental/fetch"): h_fundamental_fetch,
    ("POST", "/api/fundamental/industry"): h_fundamental_industry,
    ("POST", "/api/fundamental/clear"): h_fundamental_clear,
    ("POST", "/api/strategies/deploy"): h_strategy_deploy,
    ("POST", "/api/strategies/undeploy"): h_strategy_undeploy,
    ("GET", "/api/strategies/runtime"): h_strategy_runtime,
    ("POST", "/api/strategies/preview"): h_strategy_preview,
    ("POST", "/api/strategies/run"): h_strategy_run,

    ("POST", "/api/backtest/run"): h_backtest_run,
    ("POST", "/api/backtest/walk_forward"): h_backtest_walkforward,
    ("GET", "/api/backtest/list"): h_backtest_list,
    ("GET", "/api/backtest/get"): h_backtest_get,
    ("POST", "/api/backtest/delete"): h_backtest_delete,

    ("GET", "/api/broker/list"): h_broker_list,
    ("GET", "/api/broker/status"): h_broker_status,
    ("POST", "/api/broker/switch"): h_broker_switch,
    ("POST", "/api/broker/connect"): h_broker_connect,
    ("POST", "/api/broker/disconnect"): h_broker_disconnect,
    ("POST", "/api/broker/check"): h_broker_check,

    ("GET", "/api/account"): h_account,
    ("GET", "/api/positions"): h_positions,
    ("GET", "/api/positions/overview"): h_positions_overview,
    ("GET", "/api/orders"): h_orders,
    ("GET", "/api/trades"): h_trades,
    ("GET", "/api/equity"): h_equity,
    ("POST", "/api/order"): h_order_place,
    ("POST", "/api/order/cancel"): h_order_cancel,
    ("POST", "/api/paper/reset"): h_paper_reset,
    ("GET", "/api/paper/max"): h_paper_max,

    ("GET", "/api/engine/status"): h_engine_status,
    ("POST", "/api/engine/start"): h_engine_start,
    ("POST", "/api/engine/stop"): h_engine_stop,
    ("POST", "/api/engine/tick"): h_engine_tick,
    ("POST", "/api/engine/panic"): h_engine_panic,
    ("POST", "/api/live/enable"): h_live_enable,
    ("POST", "/api/live/disable"): h_live_disable,
}


def handle(method: str, path: str, query: dict, body) -> tuple:
    fn = ROUTES.get((method.upper(), path))
    if fn is None:
        return 404, {"ok": False, "message": "没有这个接口: %s %s" % (method, path),
                     "available": sorted("%s %s" % k for k in ROUTES)}
    try:
        return fn(query, body)
    except Exception as e:  # noqa: BLE001
        detail = traceback.format_exc()[-1500:]
        store.log("ERROR", "api", "%s %s 出错: %s" % (method, path, str(e)[:200]), detail=detail)
        return 500, {"ok": False, "message": "%s: %s" % (type(e).__name__, str(e)[:300]),
                     "trace": detail}
