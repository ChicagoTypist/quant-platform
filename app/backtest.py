# -*- coding: utf-8 -*-
"""回测引擎

规则尽量贴近 A 股真实情况：
  * T+1：当天买入的股票当天不能卖
  * 一手 100 股，买入必须是 100 的整数倍
  * 手续费：佣金（有最低 5 元）+ 印花税（卖出）+ 过户费 + 滑点
  * 信号在第 i 根 K 线收盘产生，第 i+1 根开盘成交（避免用到未来数据）

v2.6 起拆成三段，方便复用：
  * _prepare()   取数 + 算信号（只做一次）
  * _simulate()  对任意日期区间逐日模拟（walk-forward 会反复调用它）
  * run()        完整跑一遍并保存；walk_forward() 滚动样本外验证
"""
from __future__ import annotations

import datetime as dt
import math

from . import config
from . import datasource as ds
from . import fees as fee_mod
from . import indicators as ind
from . import strategies as st
from . import store

BENCHMARK = "000300.SH"
MAX_BARS = 1500


def _round_lot(shares: float, lot: int, mode: str = "down") -> int:
    if lot <= 0:
        lot = 100
    if mode == "down":
        return int(shares // lot) * lot
    return int(math.ceil(shares / lot) * lot)


# 手续费统一由 app/fees.py 提供，回测和模拟盘用同一套算法
buy_cost = fee_mod.buy_fee
sell_cost = fee_mod.sell_fee
_fee_cfg = fee_mod.fee_config


def fetch_data(symbols: list, adjust: int = 1, bars: int = MAX_BARS) -> tuple:
    """拉取多只股票的历史K线，返回 (data, errors)"""
    data, errors = {}, []
    for s in symbols:
        try:
            ks = ds.kline(s, "daily", adjust, bars)
            ks = [k for k in ks if k.get("close")]
            if len(ks) < 30:
                errors.append({"symbol": s, "error": "历史数据太少(%d根)，无法回测" % len(ks)})
                continue
            data[ds.normalize(s)] = ks
        except Exception as e:  # noqa: BLE001
            errors.append({"symbol": s, "error": str(e)[:120]})
    return data, errors


def _prepare(strategy_key: str, params: dict | None, symbols: list,
             capital: float | None, fee_overrides: dict | None,
             bars: int, participation_rate: float, symbol_weights: dict | None) -> dict | None:
    """取数 + 建策略 + 算信号。只做一次，供 run / walk_forward 共享。

    返回上下文 dict；股票全都没数据时返回 None，由调用方决定怎么报错。
    """
    symbols = [s for s in (symbols or []) if s]
    if not symbols:
        raise ValueError("至少要选一只股票")
    capital = float(capital if capital is not None else config.get("paper.initial_capital", 200000.0))
    fees = _fee_cfg(fee_overrides)
    lot = int(config.get("trading.lot_size", 100))
    try:
        participation_rate = float(participation_rate)
    except Exception:  # noqa: BLE001
        participation_rate = 0.10
    participation_rate = min(max(participation_rate, 0.01), 1.0)

    data, errors = fetch_data(symbols, 1, bars)
    if not data:
        return None

    strat = st.get_strategy(strategy_key, params or {}, list(data.keys()))
    try:
        sig_map = strat.signals_multi(data)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("策略计算失败: %s" % e)

    # 用户指定的每只股票权重优先于策略默认
    if symbol_weights:
        for sym, sigs in sig_map.items():
            if sym not in symbol_weights:
                continue
            try:
                wv = float(symbol_weights[sym])
            except Exception:
                continue
            for sg in sigs:
                if sg.get("action") == "buy":
                    sg["weight"] = wv

    return {
        "symbols": symbols, "data": data, "errors": errors, "strat": strat,
        "sig_map": sig_map, "capital": capital, "fees": fees, "lot": lot,
        "participation_rate": participation_rate, "bars": bars,
    }


def _regress(x: list, y: list) -> tuple:
    """一元线性回归 y = alpha + beta*x，返回 (beta, alpha)"""
    n = len(x)
    if n < 3:
        return 1.0, 0.0
    mx = sum(x) / n
    my = sum(y) / n
    vx = sum((v - mx) ** 2 for v in x)
    if vx <= 1e-15:
        return 0.0, my
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    beta = cov / vx
    return beta, my - beta * mx


def _attribution(equity_curve: list, benchmark_curve: list, capital: float,
                 trades: list, contrib: dict) -> dict:
    """收益归因：把总收益拆成「市场给的 / 择时赚的 / 选股赚的 / 费用拖累」。

    方法（可加总，不是拍脑袋的数字）：
      每天的组合收益 r_p 可以写成
          r_p = (1 - w现金)·r_基准  +  (1 - w现金)·(r_股票持仓 - r_基准)  +  w现金·0
      三项分别对应「跟随市场的部分」「选股超越基准的部分」「现金闲置的部分」。
      把它们各自复利到底，就得到 市场贡献 / 选股贡献 / 择时贡献，三者相加 = 总收益。
    """
    att: dict = {"fee_drag_pct": 0.0, "beta": None, "alpha_annualized_pct": None}
    if not equity_curve or capital <= 0:
        return att

    total_fee = sum(t["fee"] for t in trades)
    att["fee_drag_pct"] = round(total_fee / capital * 100.0, 3)

    bmap = {b["date"]: b["value"] for b in benchmark_curve}
    # 净值序列以初始资金为起点。equity_curve 首点已是「首日收盘净值」，
    # 而 benchmark_curve 首点被人为设为 capital（起点）。为了让二者对齐、可精确加总，
    # 这里把「capital → 首日收盘」这一段补进 rp；rb 对应补 0（基准首日无涨跌信息，
    # 首日基准涨跌对归因的影响可忽略）。
    rp = [equity_curve[0]["equity"] / capital - 1.0] if equity_curve else []
    cashw = [1.0] if equity_curve else []
    rb = [0.0] if equity_curve else []
    for i in range(1, len(equity_curve)):
        e0, e1 = equity_curve[i - 1], equity_curve[i]
        if e0["equity"] <= 0:
            continue
        rp.append(e1["equity"] / e0["equity"] - 1.0)
        cashw.append(e0["cash"] / e0["equity"] if e0["equity"] > 0 else 0.0)
        b0 = bmap.get(e0["date"])
        b1 = bmap.get(e1["date"])
        rb.append((b1 / b0 - 1.0) if (b0 and b1 and b0 > 0) else 0.0)

    if not rp:
        return att

    beta, alpha_daily = _regress(rb, rp)
    att["beta"] = round(beta, 3)
    att["alpha_annualized_pct"] = round(((1.0 + alpha_daily) ** 252 - 1.0) * 100.0, 2)

    # 三条复利路径
    v_bench = 1.0     # 满仓持基准
    v_timing = 1.0    # 持基准、但把现金权重空出来（择时/现金拖累）
    v_port = 1.0      # 实际组合
    for k in range(len(rp)):
        v_bench *= (1.0 + rb[k])
        v_timing *= (1.0 + (1.0 - cashw[k]) * rb[k])
        v_port *= (1.0 + rp[k])

    market_pct = (v_bench - 1.0) * 100.0
    timing_pct = (v_timing - 1.0) * 100.0
    total_pct = (v_port - 1.0) * 100.0

    att["market_return_pct"] = round(market_pct, 2)
    att["timing_contribution_pct"] = round(timing_pct - market_pct, 2)   # 相对满仓基准的择时贡献
    att["selection_contribution_pct"] = round(total_pct - timing_pct, 2)  # 选股（含费用）
    att["cash_drag_pct"] = round(market_pct - timing_pct, 2)              # 现金拖累（正=白等）

    # 个股贡献排名
    items = sorted(contrib.items(), key=lambda x: -x[1])
    att["top_winners"] = [{"symbol": s, "pnl": round(v, 2)} for s, v in items[:5] if v > 0]
    att["top_losers"] = [{"symbol": s, "pnl": round(v, 2)} for s, v in items[-5:] if v < 0][::-1]

    # 行业贡献
    ind_sum: dict = {}
    for s, v in contrib.items():
        ind_sum[ds.industry_of(s) or "未知"] = ind_sum.get(ds.industry_of(s) or "未知", 0.0) + v
    ind_rank = sorted(ind_sum.items(), key=lambda x: -x[1])
    att["industry_contribution"] = [{"industry": k, "pnl": round(v, 2)} for k, v in ind_rank[:8]]

    return att


def _simulate(ctx: dict, start: str, end: str, position_pct: float | None) -> dict:
    """对某个日期区间逐日模拟。ctx 由 _prepare 产出。

    返回完整的回测结果字典（不含 name、不保存）。walk_forward 会反复调用本函数，
    每次只切不同的 start/end，取数和信号都只做了一次，所以很快。
    """
    data = ctx["data"]
    sig_map = ctx["sig_map"]
    strat = ctx["strat"]
    capital = ctx["capital"]
    fees = ctx["fees"]
    lot = ctx["lot"]
    participation_rate = ctx["participation_rate"]
    errors = ctx["errors"]
    bars = ctx.get("bars", MAX_BARS)

    t0 = dt.datetime.now()

    def in_range(d: str) -> bool:
        dd = d[:10]
        if start and dd < start[:10]:
            return False
        if end and dd > end[:10]:
            return False
        return True

    # 每只股票：日期 -> 该日K线
    bar_map = {}
    for sym, ks in data.items():
        bar_map[sym] = {k["date"][:10]: k for k in ks if in_range(k["date"])}
    all_dates = sorted(set().union(*[set(m.keys()) for m in bar_map.values()])) if bar_map else []
    if len(all_dates) < 2:
        return {"ok": False, "error": "所选时间段内交易日太少，请放宽时间范围", "errors": errors}

    # ---- 处理回测区间起点：把区间之前的持仓状态带进来 ----
    sig_lists: dict[str, list] = {}
    for sym, ks in data.items():
        i_start = None
        for i, k in enumerate(ks):
            if in_range(k["date"]):
                i_start = i
                break
        if i_start is None:
            sig_lists[sym] = []
            continue
        sigs = sorted(sig_map.get(sym, []), key=lambda s: int(s.get("i", -1)))
        pre = [s for s in sigs if int(s.get("i", -1)) < i_start]
        kept = [s for s in sigs if int(s.get("i", -1)) >= i_start]
        if pre and getattr(strat, "carry_state", True) and pre[-1].get("action") == "buy":
            kept.append({"i": max(i_start - 1, 0), "action": "buy",
                         "weight": pre[-1].get("weight"),
                         "reason": "回测起点已持仓（延续起点前的买入信号）"})
        sig_lists[sym] = kept

    # ---- 生成执行计划：信号日 -> 下一根开盘成交 ----
    plan: dict[str, list] = {}
    for sym, ks in data.items():
        for sg in sig_lists.get(sym, []):
            i = int(sg.get("i", -1))
            if i < 0 or i + 1 >= len(ks):
                continue
            exec_bar = ks[i + 1]
            d = exec_bar["date"][:10]
            if not in_range(d):
                continue
            plan.setdefault(d, []).append({
                "symbol": sym, "action": sg["action"], "price": exec_bar["open"],
                "reason": sg.get("reason", ""), "weight": sg.get("weight"),
                # 分批卖（网格减一档）要带 fraction；目标仓位型信号要带 target_weight。
                # 这两个字段以前在这里被丢掉，导致"减一档"退化成"一次清仓"。
                "fraction": sg.get("fraction"),
                "target_weight": sg.get("target_weight"),
                "sig_date": ks[i]["date"][:10],
            })
    for d in plan:
        plan[d].sort(key=lambda x: (0 if x["action"] == "sell" else 1))

    # ---- 逐日模拟 ----
    cash = capital
    pos: dict = {}   # sym -> {qty, avail, cost, last}
    equity_curve = []
    trades = []
    last_close: dict = {}
    closed_pnls: list = []

    MAX_DEFER = 5
    pending: list = []
    unfilled: list = []
    rej_stat = {"涨停买不进": 0, "跌停卖不掉": 0, "停牌顺延超时": 0, "流动性不足": 0,
                "调整幅度不足一手": 0}

    def mark_price(sym: str, date: str) -> float:
        b = bar_map.get(sym, {}).get(date)
        if b:
            last_close[sym] = b["close"]
            return b["close"]
        return last_close.get(sym, 0.0)

    _prev: dict = {}
    for sym, ks in data.items():
        pm = {}
        lastc = None
        for k in ks:
            d = k["date"][:10]
            pm[d] = lastc if lastc is not None else k.get("close")
            lastc = k.get("close")
        _prev[sym] = pm

    def _limits(sym: str, date: str):
        pc = _prev.get(sym, {}).get(date)
        if not pc:
            return None, None
        b = bar_map.get(sym, {}).get(date) or {}
        try:
            pct = ds.limit_pct(sym, b.get("name") or "")
        except Exception:  # noqa: BLE001
            pct = 10.0
        return (round(float(pc) * (1 + pct / 100.0), 2),
                round(float(pc) * (1 - pct / 100.0), 2))

    def _vol_cap(sym: str, date: str) -> int:
        b = bar_map.get(sym, {}).get(date) or {}
        vol = float(b.get("volume") or 0)
        if vol <= 0:
            return 10 ** 12
        return max(0, int(vol * 100 * participation_rate))

    for di, date in enumerate(all_dates):
        for sym, p in pos.items():
            p["avail"] = p["qty"]

        for sym in pos:
            mark_price(sym, date)

        pending.extend(plan.get(date, []))
        still_pending: list = []
        for ev in pending:
            sym = ev["symbol"]
            bar = bar_map.get(sym, {}).get(date)
            if not bar:
                ev["_defer"] = int(ev.get("_defer", 0)) + 1
                if ev["_defer"] > MAX_DEFER:
                    rej_stat["停牌顺延超时"] += 1
                    unfilled.append({"symbol": sym, "action": ev["action"], "date": date,
                                     "reason": "连续 %d 个交易日无法成交（停牌或无行情）" % MAX_DEFER})
                else:
                    still_pending.append(ev)
                continue
            price = float(bar.get("open") or 0)
            if price <= 0:
                continue
            lu, ld = _limits(sym, date)

            # ---- 目标仓位型信号 ----
            # 策略只说「这只票应该占总资产的百分之几」，具体是买还是卖、买卖多少，
            # 由引擎拿【实际持仓】现算。这样策略内部记账和真实成交不会脱节：
            # 分批卖、整手取整、现金不足、前面被拒单，全都能自动自洽。
            tw = ev.get("target_weight")
            if tw is not None:
                try:
                    tw = float(tw)
                except (TypeError, ValueError):
                    tw = None
            is_target = tw is not None
            if is_target:
                p0 = pos.get(sym)
                cur_val = (p0["qty"] * (mark_price(sym, date) or price)) if p0 else 0.0
                eq_now = cash + sum(q * mark_price(s, date) for s, q in
                                    [(s2, p2["qty"]) for s2, p2 in pos.items()])
                if eq_now <= 0:
                    continue
                diff = eq_now * max(0.0, min(tw, 100.0)) / 100.0 - cur_val
                if diff >= 0:
                    ev["action"] = "buy"
                    ev["weight"] = diff / eq_now * 100.0
                    ev["fraction"] = None
                else:
                    if cur_val <= 0:
                        continue
                    ev["action"] = "sell"
                    ev["fraction"] = min(1.0, (-diff) / cur_val)
                    ev["weight"] = None

            if ev["action"] == "buy":
                if lu is not None and price >= lu - 0.005:
                    ev["_defer"] = int(ev.get("_defer", 0)) + 1
                    rej_stat["涨停买不进"] += 1
                    if ev["_defer"] > MAX_DEFER:
                        unfilled.append({"symbol": sym, "action": "buy", "date": date,
                                         "reason": "连续涨停 %d 天，买不进" % MAX_DEFER})
                    else:
                        still_pending.append(ev)
                    continue
                w = ev.get("weight")
                if w is None:
                    w = position_pct if position_pct is not None else 100.0 / max(len(data), 1)
                w = max(0.0, min(float(w), 100.0)) / 100.0
                target_value = (cash + sum(p["qty"] * last_close.get(s, 0.0) for s, p in pos.items())) * w
                px = price * (1.0 + fees["slippage"])
                affordable = cash * 0.995
                use_value = min(target_value, affordable)
                shares = _round_lot(use_value / px, lot)
                if shares <= 0:
                    # 目标仓位只差几十股、凑不满一手（高价股常见）：
                    # 这是真实的可执行性约束，不是程序错。记一笔，让报告能说清楚。
                    if is_target:
                        rej_stat["调整幅度不足一手"] += 1
                    continue
                cap = _vol_cap(sym, date)
                if cap < lot:
                    ev["_defer"] = int(ev.get("_defer", 0)) + 1
                    rej_stat["流动性不足"] += 1
                    if ev["_defer"] > MAX_DEFER:
                        unfilled.append({"symbol": sym, "action": "buy", "date": date,
                                         "reason": "当日成交量太小，买不到一手"})
                    else:
                        still_pending.append(ev)
                    continue
                if shares > cap:
                    shares = _round_lot(cap * 1.0, lot)
                    if shares <= 0:
                        still_pending.append(ev)
                        continue
                amount = shares * px
                fee = buy_cost(amount, fees)
                if amount + fee > cash:
                    shares = _round_lot((cash - fees["commission_min"]) / (px * (1 + fees["commission_rate"] + fees["transfer_fee_rate"])), lot)
                    if shares <= 0:
                        continue
                    amount = shares * px
                    fee = buy_cost(amount, fees)
                cash -= amount + fee
                p = pos.setdefault(sym, {"qty": 0, "avail": 0, "cost": 0.0, "last": px, "first_date": date})
                p["cost"] += amount + fee
                p["qty"] += shares
                p["last"] = px
                trades.append({"date": date, "symbol": sym, "side": "buy", "price": round(px, 3),
                               "qty": shares, "amount": round(amount, 2), "fee": round(fee, 2),
                               "pnl": 0.0, "reason": ev["reason"]})
            else:
                p = pos.get(sym)
                if not p or p["avail"] <= 0:
                    continue
                if ld is not None and price <= ld + 0.005:
                    ev["_defer"] = int(ev.get("_defer", 0)) + 1
                    rej_stat["跌停卖不掉"] += 1
                    if ev["_defer"] > MAX_DEFER:
                        unfilled.append({"symbol": sym, "action": "sell", "date": date,
                                         "reason": "连续跌停 %d 天，卖不掉" % MAX_DEFER})
                    else:
                        still_pending.append(ev)
                    continue
                avail_before = p["avail"]
                cap = _vol_cap(sym, date)
                # 这一单「想卖多少股」：默认全平（原行为不变）；
                # 信号带 fraction 的按比例卖 —— 网格减一档、组合调权重都要靠它。
                want = avail_before
                frac = ev.get("fraction")
                if frac is not None:
                    try:
                        f = float(frac)
                    except (TypeError, ValueError):
                        f = None
                    if f is not None and 0.0 < f < 1.0:
                        want = _round_lot(avail_before * f, lot)
                        # 仓位太小，按比例算出来不足一手 → 至少卖一手。
                        # 否则这个卖单每天都算出 0 股，永远卖不动，卡死在这里。
                        if want <= 0:
                            if is_target:
                                # 目标仓位只要求减一点点，连一手都不够 → 跳过并记账，
                                # 不能为了"卖得动"就硬卖一手（那会大幅超卖）。
                                rej_stat["调整幅度不足一手"] += 1
                                continue
                            want = min(avail_before, lot)
                if want <= 0:
                    continue
                shares = min(want, cap) if cap < want else want
                shares = _round_lot(shares * 1.0, lot)
                if shares <= 0:
                    ev["_defer"] = int(ev.get("_defer", 0)) + 1
                    rej_stat["流动性不足"] += 1
                    if ev["_defer"] > MAX_DEFER:
                        unfilled.append({"symbol": sym, "action": "sell", "date": date,
                                         "reason": "当日成交量太小，卖不出去"})
                    else:
                        still_pending.append(ev)
                    continue
                px = price * (1.0 - fees["slippage"])
                amount = shares * px
                fee = sell_cost(amount, fees)
                avg_cost = p["cost"] / p["qty"] if p["qty"] else 0.0
                pnl = amount - fee - avg_cost * shares
                cash += amount - fee
                p["qty"] -= shares
                p["avail"] -= shares
                p["cost"] = avg_cost * p["qty"]
                p["last"] = px
                closed_pnls.append(pnl)
                trades.append({"date": date, "symbol": sym, "side": "sell", "price": round(px, 3),
                               "qty": shares, "amount": round(amount, 2), "fee": round(fee, 2),
                               "pnl": round(pnl, 2), "reason": ev["reason"]})
                if p["qty"] <= 0:
                    pos.pop(sym, None)
                elif shares < want:
                    # 只有「因为成交量不够而没卖完这一单」才顺延到下一日；
                    # 按 fraction 分批卖时，卖够比例就算这一单完成，不能补单。
                    still_pending.append(dict(ev))
        dedup: dict = {}
        for ev in still_pending:
            dedup[(ev["symbol"], ev["action"])] = ev
        pending = list(dedup.values())

        mv = 0.0
        for sym, p in pos.items():
            mv += p["qty"] * mark_price(sym, date)
        eq = cash + mv
        equity_curve.append({"date": date, "equity": round(eq, 2), "cash": round(cash, 2),
                             "market_value": round(mv, 2)})

    # ---- 个股累计贡献（已实现 + 未实现）----
    contrib: dict = {}
    for t in trades:
        contrib[t["symbol"]] = contrib.get(t["symbol"], 0.0) + (t.get("pnl") or 0.0)
    for sym, p in pos.items():
        contrib[sym] = contrib.get(sym, 0.0) + (p["qty"] * last_close.get(sym, 0.0) - p["cost"])

    # ---- 基准 ----
    bench_curve = []
    bench_ret = 0.0
    try:
        bks = ds.kline(BENCHMARK, "daily", 0, bars)
        bmap = {k["date"][:10]: k["close"] for k in bks}
        first = None
        for d in all_dates:
            if d in bmap:
                if first is None:
                    first = bmap[d]
                bench_curve.append({"date": d, "value": round(bmap[d] / first * capital, 2)})
            elif bench_curve:
                bench_curve.append({"date": d, "value": bench_curve[-1]["value"]})
        if bench_curve:
            bench_ret = (bench_curve[-1]["value"] / capital - 1.0) * 100.0
    except Exception:
        pass

    # ---- 绩效指标 ----
    eqs = [e["equity"] for e in equity_curve]
    total_ret = (eqs[-1] / capital - 1.0) * 100.0 if eqs else 0.0
    d0 = dt.date.fromisoformat(all_dates[0])
    d1 = dt.date.fromisoformat(all_dates[-1])
    days = max((d1 - d0).days, 1)
    daily_rets = []
    for i in range(1, len(eqs)):
        if eqs[i - 1] > 0:
            daily_rets.append(eqs[i] / eqs[i - 1] - 1.0)
    mdd = ind.max_drawdown(eqs)
    wins = [x for x in closed_pnls if x > 0]
    losses = [x for x in closed_pnls if x <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    benchmark_for_curve = [b["value"] for b in bench_curve]
    bdd = ind.max_drawdown(benchmark_for_curve) if benchmark_for_curve else 0.0

    metrics = {
        "initial_capital": round(capital, 2),
        "final_equity": round(eqs[-1], 2) if eqs else capital,
        "total_return_pct": round(total_ret, 2),
        "annual_return_pct": round(ind.annualized_return(eqs, days), 2),
        "max_drawdown_pct": round(mdd, 2),
        "benchmark_return_pct": round(bench_ret, 2),
        "benchmark_max_drawdown_pct": round(bdd, 2),
        "excess_return_pct": round(total_ret - bench_ret, 2),
        "sharpe": round(ind.sharpe(daily_rets), 2),
        "sortino": round(ind.sortino(daily_rets), 2),
        "trade_count": len(trades),
        "closed_trades": len(closed_pnls),
        "win_count": len(wins),
        "lose_count": len(losses),
        "win_rate_pct": round(len(wins) / len(closed_pnls) * 100.0, 2) if closed_pnls else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "total_fee": round(sum(t["fee"] for t in trades), 2),
        "days": days,
        "start": all_dates[0],
        "end": all_dates[-1],
        "strategy": strat.name,
        "strategy_key": strat.key if hasattr(strat, "key") else "",
        "params": strat.p,
        "symbols": list(data.keys()),
        "participation_rate_pct": round(participation_rate * 100, 1),
        "unfilled_count": len(unfilled),
        "unfilled_breakdown": dict(rej_stat),
        "unfilled_samples": unfilled[:10],
    }

    # ---- 收益归因 ----
    attribution = _attribution(equity_curve, bench_curve, capital, trades, contrib)

    # ---- 友好提示 ----
    warnings: list = []
    traded_syms = {}
    for t in trades:
        traded_syms[t["symbol"]] = traded_syms.get(t["symbol"], 0) + 1
    for sym in data:
        n_sig = len(sig_lists.get(sym, []))
        if n_sig > 0 and traded_syms.get(sym, 0) == 0:
            last_close_v = None
            for k in reversed(data[sym]):
                if in_range(k["date"]):
                    last_close_v = float(k["close"])
                    break
            if last_close_v:
                need = last_close_v * lot
                warnings.append(
                    "%s 产生了 %d 个买卖信号，但一次都没成交。原因：它股价约 %.2f 元，"
                    "买 1 手（%d 股）就要 %.0f 元，超出了这只股票分到的资金。"
                    "可以加大初始资金、把资金分散到更多只股票、或换价格低一些的标的。"
                    % (sym, n_sig, last_close_v, lot, need))
        elif n_sig == 0:
            warnings.append("%s 在这段时间内没有产生任何信号，可以调整策略参数或换标的。" % sym)
    if not trades:
        warnings.insert(0, "本次回测一笔都没成交，请先看下面的原因说明再调整。")
    if len(unfilled) > 0:
        parts = ["%s %d 笔" % (k, v) for k, v in rej_stat.items() if v > 0]
        warnings.append(
            "有 %d 笔订单最终没能成交（%s）。这些是【真实交易也会遇到的】限制："
            "涨停买不进、跌停卖不掉、停牌、当日成交量太小。"
            "回测已经把它们算作【没成交】，所以收益比不设限时更保守、更接近实盘。"
            % (len(unfilled), "、".join(parts) or "原因见明细"))
    elif any(v > 0 for v in rej_stat.values()):
        parts = ["%s %d 次" % (k, v) for k, v in rej_stat.items() if v > 0]
        warnings.append("成交过程中触发了交易限制：%s（部分订单顺延后成交）。" % "、".join(parts))
    warnings.append(
        "幸存者偏差提醒：回测只覆盖了「至今仍在交易」的股票，"
        "历史上退市、长期停牌后摘牌的那些不会出现在你的股票池里。"
        "所以回测收益系统性偏乐观——真实持有到退市的亏损没有被统计进来。")

    return {
        "ok": True,
        "metrics": metrics,
        "attribution": attribution,
        "equity_curve": equity_curve,
        "benchmark_curve": bench_curve,
        "trades": trades,
        "errors": errors,
        "warnings": warnings,
        "elapsed_sec": round((dt.datetime.now() - t0).total_seconds(), 2),
    }


def run(strategy_key: str, params: dict | None = None, symbols: list | None = None,
        start: str = "", end: str = "", capital: float | None = None,
        position_pct: float | None = None, fee_overrides: dict | None = None,
        bars: int = MAX_BARS, save: bool = True, name: str = "",
        symbol_weights: dict | None = None,
        participation_rate: float = 0.10) -> dict:
    """执行一次回测，返回完整结果字典"""
    try:
        ctx = _prepare(strategy_key, params, symbols, capital, fee_overrides,
                       bars, participation_rate, symbol_weights)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if ctx is None:
        return {"ok": False, "error": "所有股票都没取到数据", "errors": []}

    result = _simulate(ctx, start, end, position_pct)
    if not result.get("ok"):
        return result

    strat = ctx["strat"]
    result["name"] = name or ("%s · %s" % (strat.name, "、".join(list(ctx["data"].keys())[:3])))
    if save:
        try:
            result["id"] = store.save_backtest(result["name"], strategy_key, strat.p,
                                               list(ctx["data"].keys()), result)
        except Exception as e:  # noqa: BLE001
            store.log("WARN", "backtest", "回测结果保存失败: %s" % e)
    return result


# ---------------------------------------------------------------------------
# Walk-forward 滚动样本外验证
# ---------------------------------------------------------------------------
def walk_forward(strategy_key: str, params: dict | None = None, symbols: list | None = None,
                 start: str = "", end: str = "", capital: float | None = None,
                 position_pct: float | None = None, fee_overrides: dict | None = None,
                 bars: int = MAX_BARS, train_days: int = 252, test_days: int = 63,
                 step_days: int = 63, participation_rate: float = 0.10,
                 symbol_weights: dict | None = None) -> dict:
    """滚动样本外（walk-forward）验证。

    为什么需要它：普通回测是「用一整段历史同时选参数 + 看结果」，等于考试前先看答案，
    很容易过拟合——样本内很好看，实盘一塌糊涂。walk-forward 模拟真实使用场景：
    用【过去一段】训练/选参数，用【紧接着的下一段】样本外检验，然后窗口往前滚动。
    只看样本外的表现，才能判断策略是真的有效还是只是背下了历史。

    返回每个窗口的样本内(IS)/样本外(OOS)收益与夏普，以及稳健性评分和过拟合判定。
    """
    try:
        ctx = _prepare(strategy_key, params, symbols, capital, fee_overrides,
                       bars, participation_rate, symbol_weights)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if ctx is None:
        return {"ok": False, "error": "所有股票都没取到数据"}

    data = ctx["data"]

    def in_range(d: str) -> bool:
        dd = d[:10]
        if start and dd < start[:10]:
            return False
        if end and dd > end[:10]:
            return False
        return True

    all_dates = sorted(set().union(*[{k["date"][:10] for k in ks} for ks in data.values()]))
    all_dates = [d for d in all_dates if in_range(d)]
    n = len(all_dates)
    train_days = max(20, int(train_days))
    test_days = max(10, int(test_days))
    step_days = max(1, int(step_days))
    if n < train_days + test_days:
        return {"ok": False, "error": "历史数据不够：滚动验证至少需要 %d 个交易日，"
                                      "当前区间只有 %d 个。请拉长开始日期或缩短训练/测试窗口。"
                                      % (train_days + test_days, n)}

    # 生成滚动窗口：训练段 [i-train, i-1]，测试段 [i, i+test-1]
    windows = []
    i = train_days
    while i + test_days <= n:
        windows.append({
            "train_start": all_dates[i - train_days], "train_end": all_dates[i - 1],
            "test_start": all_dates[i], "test_end": all_dates[i + test_days - 1],
        })
        i += step_days

    win_rows = []
    for w in windows:
        is_res = _simulate(ctx, w["train_start"], w["train_end"], position_pct)
        oos_res = _simulate(ctx, w["test_start"], w["test_end"], position_pct)
        if not is_res.get("ok") or not oos_res.get("ok"):
            continue
        im, om = is_res["metrics"], oos_res["metrics"]
        win_rows.append({
            "train_start": w["train_start"], "train_end": w["train_end"],
            "test_start": w["test_start"], "test_end": w["test_end"],
            "is_return_pct": im["total_return_pct"],
            "is_sharpe": im["sharpe"],
            "oos_return_pct": om["total_return_pct"],
            "oos_sharpe": om["sharpe"],
            "oos_maxdd_pct": om["max_drawdown_pct"],
            "oos_excess_pct": om["excess_return_pct"],
            "oos_annualized_pct": round(om["annual_return_pct"], 2),
        })

    if not win_rows:
        return {"ok": False, "error": "没有任何一个窗口能跑完，请检查数据或时间范围"}

    oos_rets = [w["oos_return_pct"] for w in win_rows]
    is_rets = [w["is_return_pct"] for w in win_rows]
    oos_sharpes = [w["oos_sharpe"] for w in win_rows]
    avg_oos = sum(oos_rets) / len(oos_rets)
    avg_is = sum(is_rets) / len(is_rets)
    win_rate = sum(1 for r in oos_rets if r > 0) / len(oos_rets)
    # 样本内/样本外收益比：越接近 1 越稳，越大说明越过拟合
    is_oos_ratio = (avg_is / avg_oos) if abs(avg_oos) > 1e-9 else (999.0 if avg_is > 0 else 0.0)
    # 年化（每段测试约 test_days/252 年，粗略折算）
    avg_oos_ann = avg_oos * (252.0 / test_days)

    if avg_oos > 0 and win_rate >= 0.6 and 0 < is_oos_ratio < 2.0:
        verdict = "样本外表现稳健：多数测试段都能赚钱，样本内外的落差也不大，过拟合风险较低。"
        level = "good"
    elif avg_oos > 0 and win_rate >= 0.4:
        verdict = "样本外勉强为正，但窗口间不太稳定，或样本内外落差偏大，存在过拟合迹象，建议谨慎、缩小仓位。"
        level = "warn"
    else:
        verdict = "样本外表现差或不稳定：这个策略很可能只是在训练段背下了历史，实盘大概率失效，不建议直接上。"
        level = "bad"

    return {
        "ok": True,
        "strategy": ctx["strat"].name,
        "strategy_key": strategy_key,
        "params": ctx["strat"].p,
        "symbols": list(data.keys()),
        "train_days": train_days, "test_days": test_days, "step_days": step_days,
        "window_count": len(win_rows),
        "windows": win_rows,
        "summary": {
            "oos_avg_return_pct": round(avg_oos, 2),
            "oos_avg_annualized_pct": round(avg_oos_ann, 2),
            "oos_win_rate_pct": round(win_rate * 100, 1),
            "oos_avg_sharpe": round(sum(oos_sharpes) / len(oos_sharpes), 2),
            "oos_worst_return_pct": round(min(oos_rets), 2),
            "is_avg_return_pct": round(avg_is, 2),
            "is_oos_ratio": round(is_oos_ratio, 2),
        },
        "verdict": verdict,
        "level": level,
        "note": ("把历史切成 %d 个滚动窗口，每个窗口都用「前 %d 个交易日训练、后 %d 个交易日样本外检验」。"
                 "看「样本外」这一列，才接近真实上实盘的表现。"
                 % (len(win_rows), train_days, test_days)),
    }
