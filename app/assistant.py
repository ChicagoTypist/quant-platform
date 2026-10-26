# -*- coding: utf-8 -*-
"""AI 选股助手

两种工作模式：

【本地模式】不需要任何 API Key，完全离线可跑。
  它会把你的问题分类，然后用平台自己算出来的真实数据回答：
    * 今日选股   -> 用因子实验室扫描出来的最强因子给股票池打分排序，
                    再用「回调买入法」给出具体的买点/止损/建议仓位
    * 个股分析   -> 报价 + 技术指标 + 因子得分 + 支撑压力 + 回调买入评分
    * 持仓体检   -> 每一笔的盈亏、占比、离止损多远
    * 大盘状态   -> 指数 + 市场状态 + 涨跌家数
    * 因子问答   -> 当前哪些因子有效

【大模型模式】你可以在设置里填一个 OpenAI 兼容的 API（比如 DeepSeek、通义、Kimi）。
  注意顺序：平台【先算好所有数据】，再把数据交给大模型润色成大白话。
  大模型只负责组织语言，不允许自己编数字 —— 这样它就没法瞎忽悠你。

不管哪种模式，最终建议都只是统计结果，不构成投资建议。
"""
from __future__ import annotations

import json
import math
import re
import time
import urllib.request

from . import config
from . import datasource as ds
from . import factorlab as lab
from . import factors as fac
from . import store

_pick_cache: dict = {}


# ---------------------------------------------------------------------------
# 因子权重：优先用上次扫描出来的"当前有效因子"
# ---------------------------------------------------------------------------
def _weights_from_scan(limit: int = 6):
    """从上次的全因子扫描里挑出当前最有效的几个因子，按 |IC| 给权重

    IC 为负说明这个因子要反着用，所以权重带负号。
    """
    res = lab.get_scan()
    if not res or not res.get("all"):
        return None, ""
    rows = [r for r in res["all"]
            if abs(r.get("t_stat") or 0) >= 1.5 and abs(r.get("ic_mean") or 0) >= 0.02]
    if not rows:
        return None, ""
    rows.sort(key=lambda r: abs(r["ic_mean"]), reverse=True)
    weights = {}
    for r in rows[:limit]:
        w = abs(r["ic_mean"]) * (1.0 if r["ic_mean"] > 0 else -1.0)
        weights[r["key"]] = round(w, 5)
    names = "、".join(r["name"] for r in rows[:limit])
    return weights, names


def current_model() -> tuple:
    """返回 (因子权重, 来源说明)"""
    w, names = _weights_from_scan()
    if w:
        return w, "由上次因子扫描自动选出：%s" % names
    p = fac.get_preset("preset_pullback")
    return dict(p["factors"]), "还没扫描过因子，先用内置的「%s」" % p["name"]


# ---------------------------------------------------------------------------
# 今日选股
# ---------------------------------------------------------------------------
def _screener_pool(n: int = 40) -> list:
    """用【自动选股】做全市场粗筛，拿到候选池。

    这样 AI 助手问「今天买什么」时，看的是全市场 5900 只，
    而不是内置的那几十只 —— 覆盖面完全不一样。
    """
    try:
        from . import screener as scr
        res = scr.run(preset="multi_factor", top_n=n, with_tech=False)
        if res.get("ok") and res.get("picked"):
            return [p["symbol"] for p in res["picked"] if p.get("symbol")]
    except Exception as e:  # noqa: BLE001
        try:
            store.log("WARN", "assistant", "自动选股取池失败，回退内置池: %s" % str(e)[:120])
        except Exception:
            pass
    return []


def _default_pool() -> tuple:
    """选股票池，返回 (池子, 来源说明)

    优先级：
      1. 全市场自动选股（覆盖 5900 只，最推荐）
      2. 上次因子扫描用的池子（和你在因子实验室看到的一致）
      3. 内置池（断网兜底）
    """
    sp = _screener_pool(40)
    if len(sp) >= 10:
        return sp, "全市场自动选股（多因子模板）选出的前 %d 只" % len(sp)
    res = lab.get_scan()
    if res and res.get("params", {}).get("symbols"):
        p = res["params"]["symbols"]
        if len(p) >= 10:
            return p[:30], "上次因子扫描用的股票池（%d 只）" % min(len(p), 30)
    return lab.DEFAULT_POOL[:24], "内置默认池（网络不太好，没能做全市场选股）"


def daily_picks(top_n: int = 8, pool: list | None = None, refresh: bool = False) -> dict:
    """算出整个股票池的打分排序，然后取前 top_n 名。

    注意缓存是按【股票池】缓存的，跟 top_n 无关，
    这样问「今天买什么」和「有什么回调股」不会重复算两遍（第一次要几十秒）。
    """
    pool_source = "你指定的股票池"
    if not pool:
        pool, pool_source = _default_pool()
    pool = [ds.normalize(s) for s in pool if s]
    key = ",".join(sorted(pool))
    cached = _pick_cache.get(key)
    if not refresh and cached and time.time() - cached["ts"] < 900:
        full = cached["data"]
    else:
        full = _compute_picks(pool)
        _pick_cache[key] = {"ts": time.time(), "data": full}
    out = dict(full)
    out["top"] = full["rows"][:top_n]
    out["pool_source"] = pool_source
    return out


def warm_picks() -> None:
    """后台先把选股结果算好，用户第一次问的时候就不用等"""
    def _run():
        try:
            daily_picks(10)
        except Exception:
            pass
    import threading
    threading.Thread(target=_run, name="warm-picks", daemon=True).start()


def _compute_picks(pool: list) -> dict:
    weights, source = current_model()
    panel = lab.load_panel(pool, 320)
    rows = []
    for sym, ks in panel.items():
        try:
            comp = lab.composite(ks, weights, "rank")
            score = fac.ts_rank(comp, 120)[-1]
            pb = fac.pullback_state(ks)[-1]
            close = ks[-1]["close"]
            prev = ks[-2]["close"] if len(ks) > 1 else close
            rows.append({
                "symbol": sym, "name": ks[-1].get("name") or sym,
                "industry": ds.industry_of(sym),
                "close": round(close, 2),
                "change_pct": round((close / prev - 1) * 100, 2) if prev else 0.0,
                "score": round(score, 4) if score is not None else None,
                "strong": pb["strong"] if pb else False,
                "pullback_pct": pb["pullback_pct"] if pb else None,
                "support": pb["support"] if pb else None,
                "stop": pb["stop"] if pb else None,
                "risk_pct": pb["risk_pct"] if pb else None,
                "near_support": pb["near_support"] if pb else False,
                "trigger": pb["trigger"] if pb else False,
                "shrink": pb["shrink"] if pb else False,
                "pb_score": pb["score"] if pb else 0.0,
            })
        except Exception:
            continue
    rows.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
        # 给出一个可执行性判断
        if r["strong"] and r["near_support"] and r["trigger"] and (r["risk_pct"] or 99) <= 8:
            r["verdict"] = "可以关注：回调到位且已启稳"
        elif r["strong"] and r["near_support"]:
            r["verdict"] = "值得观察：已到支撑，等确认阳线"
        elif r["strong"]:
            r["verdict"] = "趋势好但还没回调到位，先等"
        else:
            r["verdict"] = "不在上升趋势，暂不考虑"
    return {"generated_at": store.now_str(), "source": source, "weights": weights,
            "pool_size": len(panel), "rows": rows}


# ---------------------------------------------------------------------------
# 个股分析
# ---------------------------------------------------------------------------
def analyze_stock(code: str) -> dict:
    sym = ds.normalize(code)
    ks = ds.kline(sym, "daily", 1, 400)
    ks = [k for k in ks if k.get("close")]
    if len(ks) < 60:
        return {"ok": False, "message": "取不到 %s 的历史数据" % sym}
    q = ds.quote(sym)
    c = [k["close"] for k in ks]
    from . import indicators as ind
    ma5, ma20, ma60 = ind.sma(c, 5), ind.sma(c, 20), ind.sma(c, 60)
    rsi = ind.rsi(c, 14)
    dif, dea, hist = ind.macd(c)
    pb = fac.pullback_state(ks)[-1]
    weights, source = current_model()
    comp = lab.composite(ks, weights, "rank")
    score = fac.ts_rank(comp, 120)[-1]
    hi = max(k["high"] for k in ks[-60:])
    lo = min(k["low"] for k in ks[-60:])
    return {
        "ok": True, "symbol": sym, "name": q.get("name") or ks[-1].get("name"),
        "industry": ds.industry_of(sym),
        "price": q.get("price") or c[-1], "change_pct": q.get("change_pct"),
        "ma5": round(ma5[-1], 2) if ma5[-1] else None,
        "ma20": round(ma20[-1], 2) if ma20[-1] else None,
        "ma60": round(ma60[-1], 2) if ma60[-1] else None,
        "rsi14": round(rsi[-1], 1) if rsi[-1] else None,
        "macd_hist": round(hist[-1], 3) if hist[-1] else None,
        "high60": round(hi, 2), "low60": round(lo, 2),
        "score": round(score, 4) if score is not None else None,
        "pullback": pb, "model_source": source,
    }


# ---------------------------------------------------------------------------
# 持仓体检
# ---------------------------------------------------------------------------
def portfolio_check() -> dict:
    from .brokers import manager
    b = manager.active()
    try:
        bal = b.balance()
        pos = b.positions()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": "读取账户失败: %s" % str(e)[:150]}
    total = float(bal.get("total_asset") or 0)
    rows = []
    for p in pos:
        mv = float(p.get("market_value") or 0)
        rows.append({
            "symbol": p.get("symbol"), "name": p.get("name"),
            "qty": p.get("qty"), "available": p.get("available"),
            "avg_cost": p.get("avg_cost"), "last": p.get("last_price"),
            "pnl": p.get("pnl"), "pnl_pct": p.get("pnl_pct"),
            "weight_pct": round(mv / total * 100, 2) if total else 0,
        })
    rows.sort(key=lambda x: x["pnl_pct"] or 0)
    return {"ok": True, "balance": bal, "positions": rows, "broker": b.display}


# ---------------------------------------------------------------------------
# 意图识别
# ---------------------------------------------------------------------------
def _has(text: str, *words) -> bool:
    return any(w in text for w in words)


def detect_intent(q: str) -> str:
    t = (q or "").strip()
    if not t:
        return "help"
    if _has(t, "你能做什么", "帮助", "怎么用", "help", "功能"):
        return "help"
    if _has(t, "持仓", "仓位", "我的股票", "赚了", "亏了", "账户"):
        return "positions"
    if _has(t, "大盘", "市场", "指数", "行情怎么样", "今天股市", "情绪"):
        return "market"
    if _has(t, "因子", "ic", "IC", "哪个指标", "什么指标有效"):
        return "factors"
    if _has(t, "回测", "测一下", "测测"):
        return "backtest"
    if _has(t, "回调买入", "回调", "支撑", "止损", "买点"):
        return "pullback"
    m = re.search(r"(\d{6})", t)
    if m and _has(t, "分析", "怎么样", "怎么看", "能买", "好不好", "看看", "评价", "值不值"):
        return "analyze:" + m.group(1)
    if m:
        return "analyze:" + m.group(1)
    if _has(t, "买什么", "推荐", "选股", "机会", "今日", "今天买", "有什么好", "标的", "建议"):
        return "picks"
    return "fallback"


# ---------------------------------------------------------------------------
# 生成回答
# ---------------------------------------------------------------------------
def _fmt_picks(picks: dict, top_n: int) -> dict:
    rows = []
    for r in picks["top"][:top_n]:
        rows.append([
            r["rank"], "%s %s" % (r["symbol"].split(".")[0], r["name"]), r["industry"] or "-",
            "%.2f" % r["close"], ("%+.2f%%" % r["change_pct"]) if r["change_pct"] is not None else "-",
            "%.3f" % (r["score"] or 0),
            ("%.2f" % r["support"]) if r["support"] else "-",
            ("%.2f" % r["stop"]) if r["stop"] else "-",
            ("%.2f%%" % r["risk_pct"]) if r["risk_pct"] is not None else "-",
            r["verdict"],
        ])
    actionable = [r for r in picks["top"] if r["verdict"].startswith("可以关注")]
    psrc = picks.get("pool_source") or "内置股票池"
    text = ("我按【%s】给股票池里的 %d 只股票打了分，选出前 %d 名。\n\n"
            "**股票池来源**：%s\n"
            "**打分依据**：%s\n\n" % ("当前有效因子", picks["pool_size"],
                                       min(top_n, len(picks["top"])), psrc, picks["source"]))
    if actionable:
        a = actionable[0]
        text += ("其中 **%s %s** 最符合「回调买入法」的买点条件：\n"
                 "  · 现价 %.2f，离支撑位 %.2f 很近\n"
                 "  · 止损放在 %.2f，风险只有 %.2f%%\n"
                 "  · 按单笔风险 1%% 计算，建议仓位约 %.1f%%\n\n"
                 % (a["symbol"].split(".")[0], a["name"], a["close"], a["support"] or 0,
                    a["stop"] or 0, a["risk_pct"] or 0,
                    min(100.0 / max(a["risk_pct"] or 1, 0.2), 25.0)))
    else:
        text += "今天没有出现「已经回调到位并启稳」的标的，可以再等等，不要硬买。\n\n"
    text += ("⚠️ 这只是基于历史统计的排序，不是投资建议。买入前请自己再看一眼基本面，"
             "并且务必设置止损。")
    return {"answer": text,
            "blocks": [
                {"type": "table", "title": "今日候选（按因子综合分排序）",
                 "columns": ["#", "股票", "行业", "现价", "涨跌", "因子分", "支撑", "止损", "风险", "判断"],
                 "rows": rows},
                {"type": "text", "text":
                 "说明：支撑/止损是按「回调买入法」算的 —— 止损就放在支撑下方一点，"
                 "所以风险一栏的数字越小，说明买点越贴近止损、这笔交易越划算。"},
            ],
            "data": {"picks": picks["top"][:top_n], "weights": picks["weights"],
                     "pool_source": picks.get("pool_source"),
                     "pool_size": picks.get("pool_size")}}


def _fmt_analyze(a: dict) -> dict:
    if not a.get("ok"):
        return {"answer": a.get("message", "分析失败"), "blocks": []}
    pb = a.get("pullback") or {}
    lines = [
        "**%s %s**（%s）" % (a["symbol"].split(".")[0], a["name"], a["industry"] or "行业未知"),
        "现价 %.2f，今日 %s" % (a["price"], ("%+.2f%%" % a["change_pct"]) if a["change_pct"] is not None else "—"),
        "",
        "技术面：",
        "  · 均线：MA5 %.2f / MA20 %.2f / MA60 %.2f —— %s"
        % (a["ma5"] or 0, a["ma20"] or 0, a["ma60"] or 0,
           "多头排列，趋势向上" if (a["ma5"] and a["ma20"] and a["ma5"] > a["ma20"]) else "短期均线在下，趋势偏弱"),
        "  · RSI(14)：%s —— %s" % (a["rsi14"],
                                    "超买区" if (a["rsi14"] or 50) > 70 else
                                    ("超卖区" if (a["rsi14"] or 50) < 30 else "中性")),
        "  · MACD 柱：%.3f —— %s" % (a["macd_hist"] or 0,
                                      "多头动能" if (a["macd_hist"] or 0) > 0 else "空头动能"),
        "  · 60 日区间：%.2f ~ %.2f" % (a["low60"], a["high60"]),
        "",
        "因子打分：%.3f（%s）" % (a["score"] or 0, a["model_source"]),
    ]
    if pb:
        lines += [
            "",
            "回调买入法诊断：",
            "  · 是否强势趋势：%s" % ("是" if pb["strong"] else "否"),
            "  · 距近期高点回调：%.2f%%" % pb["pullback_pct"],
            "  · 支撑位：%.2f，止损位：%.2f，单笔风险：%.2f%%"
            % (pb["support"], pb["stop"], pb["risk_pct"]),
            "  · 是否贴近支撑：%s｜是否缩量：%s｜是否启稳：%s"
            % ("是" if pb["near_support"] else "否", "是" if pb["shrink"] else "否",
               "是" if pb["trigger"] else "否"),
            "  · 综合评分：%.3f" % pb["score"],
            "",
            ("结论：符合回调买入条件，可以考虑，止损 %.2f。" % pb["stop"]) if
            (pb["strong"] and pb["near_support"] and pb["trigger"])
            else ("结论：暂时不满足买点。" +
                  ("趋势不错，但还没回调到支撑，耐心等。" if pb["strong"] else "不在上升趋势里，不建议用这个方法。")),
        ]
    lines += ["", "⚠️ 以上只是基于历史数据的统计描述，不是投资建议。"]
    return {"answer": "\n".join(lines), "blocks": [{"type": "text", "text": "\n".join(lines)}], "data": a}


def _fmt_positions(p: dict) -> dict:
    if not p.get("ok"):
        return {"answer": p.get("message"), "blocks": []}
    bal = p["balance"]
    rows = [[r["symbol"].split(".")[0], r["name"], r["qty"], r["available"],
             "%.3f" % (r["avg_cost"] or 0), "%.3f" % (r["last"] or 0),
             ("%+.2f" % (r["pnl"] or 0)), ("%+.2f%%" % (r["pnl_pct"] or 0)),
             "%.1f%%" % r["weight_pct"]] for r in p["positions"]]
    text = ("当前通道：%s\n总资产 %.2f 元，可用 %.2f 元，持仓市值 %.2f 元，"
            "当日盈亏 %+.2f 元（%+.2f%%），累计 %+.2f 元。\n"
            % (p["broker"], bal.get("total_asset", 0), bal.get("available_cash", 0),
               bal.get("market_value", 0), bal.get("day_pnl", 0), bal.get("day_pnl_pct", 0),
               bal.get("total_pnl", 0)))
    if not rows:
        text += "\n现在是空仓。"
    else:
        worst = p["positions"][0]
        best = p["positions"][-1]
        text += ("\n表现最好：%s（%+.2f%%）；表现最差：%s（%+.2f%%）。\n"
                 % (best["name"], best["pnl_pct"] or 0, worst["name"], worst["pnl_pct"] or 0))
        over = [r for r in p["positions"] if r["weight_pct"] > float(config.get("risk.max_single_position_pct", 20))]
        if over:
            text += "\n⚠️ 有 %d 只股票超过了单只仓位上限：%s\n" % (
                len(over), "、".join(r["name"] for r in over))
        losers = [r for r in p["positions"] if (r["pnl_pct"] or 0) < -8]
        if losers:
            text += "\n⚠️ 有 %d 只亏损超过 8%%，建议检查止损：" % len(losers)
            text += "、".join("%s(%+.2f%%)" % (r["name"], r["pnl_pct"]) for r in losers) + "\n"
    return {"answer": text, "blocks": [
        {"type": "text", "text": text},
        {"type": "table", "title": "持仓明细",
         "columns": ["代码", "名称", "持仓", "可卖", "成本", "现价", "盈亏", "盈亏%", "占比"],
         "rows": rows}], "data": p}


def _fmt_market() -> dict:
    st = ds.market_status()
    idx = ds.indices()
    lines = ["现在是 %s（%s）。" % (st.get("label"), st.get("now"))]
    for i in idx:
        lines.append("  · %s %.2f（%+.2f%%）" % (i.get("name"), i.get("price") or 0, i.get("change_pct") or 0))
    try:
        up = ds.rank("gainers", 3)
        dn = ds.rank("losers", 3)
        lines.append("")
        lines.append("涨幅居前：" + "、".join("%s(%+.2f%%)" % (x["name"], x["change_pct"] or 0) for x in up))
        lines.append("跌幅居前：" + "、".join("%s(%+.2f%%)" % (x["name"], x["change_pct"] or 0) for x in dn))
    except Exception:
        pass
    lines += ["", "⚠️ 指数数据来自东方财富公开接口，可能略有延迟。"]
    return {"answer": "\n".join(lines), "blocks": [
        {"type": "text", "text": "\n".join(lines)},
        {"type": "table", "title": "主要指数",
         "columns": ["指数", "点位", "涨跌%"],
         "rows": [[i.get("name"), "%.2f" % (i.get("price") or 0), "%+.2f%%" % (i.get("change_pct") or 0)]
                  for i in idx]}], "data": {"status": st, "indices": idx}}


def _fmt_factors() -> dict:
    res = lab.get_scan()
    if not res:
        return {"answer": ("还没有做过因子扫描。请到【因子实验室】点一下【开始扫描】，"
                           "扫完之后我就能告诉你现在哪些因子最有效。"),
                "blocks": [{"type": "text", "text": "还没有扫描结果，先去【因子实验室】扫一次。"}]}
    rows = []
    for r in res["top"][:12]:
        rows.append([r["rank"], r["name"], r["family"], "%+.4f" % r["ic_mean"],
                     "%+.3f" % r["icir"], "%+.2f" % r["t_stat"],
                     ("%+.3f%%" % r["long_short_pct"]) if r["long_short_pct"] is not None else "-",
                     r["verdict"]])
    text = ("上次扫描时间 %s，股票池 %d 只，共测了 %d 个因子。\n\n当前最有效的几个：\n"
            % (res.get("generated_at"), res["params"]["pool_size"], res["params"]["factor_count"]))
    for r in res["top"][:5]:
        text += "  · %s（IC %+.4f，t %+.2f）—— %s\n" % (r["name"], r["ic_mean"], r["t_stat"], r["verdict"])
    text += "\n⚠️ IC 会随时间变化，今天有效的因子过几个月可能就失效了，记得定期重扫。"
    return {"answer": text, "blocks": [
        {"type": "text", "text": text},
        {"type": "table", "title": "当前因子排行（按 |IC| 排序）",
         "columns": ["#", "因子", "家族", "IC", "ICIR", "t值", "多空收益", "结论"], "rows": rows}],
        "data": res["top"][:12]}


def _fmt_pullback(q: str) -> dict:
    picks = daily_picks(15)
    cand = [r for r in picks["top"] if r["strong"]]
    if not cand:
        return {"answer": ("当前股票池里没有处于上升趋势的股票，回调买入法暂时没有机会。"
                           "这个方法的逻辑是「只在强势股回调时买」，没有机会就该空仓等。"),
                "blocks": [{"type": "text", "text": "暂时没有符合条件的标的。"}]}
    best = sorted(cand, key=lambda x: -(x["pb_score"] or 0))[:5]
    rows = [[r["symbol"].split(".")[0], r["name"], "%.2f" % r["close"],
             "%.2f%%" % r["pullback_pct"], "%.2f" % (r["support"] or 0),
             "%.2f" % (r["stop"] or 0), "%.2f%%" % (r["risk_pct"] or 0),
             "是" if r["near_support"] else "否", "是" if r["trigger"] else "否",
             "%.2f%%" % min(100.0 / max(r["risk_pct"] or 1, 0.2), 25.0)] for r in best]
    text = ("回调买入法的核心：**买在离止损最近的地方**。\n\n"
            "先找上升趋势的强势股，等它回踩到支撑位附近、并且重新掉头向上时再买，"
            "止损放在支撑下方一点。这样单笔亏损很小，错几次也不伤。\n\n"
            "当前最接近买点的几个：\n")
    for r in best[:3]:
        text += ("  · %s %s：现价 %.2f，支撑 %.2f，止损 %.2f，"
                 "风险 %.2f%%，建议仓位 %.1f%%\n"
                 % (r["symbol"].split(".")[0], r["name"], r["close"], r["support"] or 0,
                    r["stop"] or 0, r["risk_pct"] or 0,
                    min(100.0 / max(r["risk_pct"] or 1, 0.2), 25.0)))
    text += "\n买入条件（三个都要满足）：① 上升趋势里 ② 回调到支撑附近（且缩量） ③ 出现回升阳线。"
    return {"answer": text, "blocks": [
        {"type": "text", "text": text},
        {"type": "table", "title": "回调买入法候选",
         "columns": ["代码", "名称", "现价", "回调", "支撑", "止损", "风险", "贴近支撑", "已启稳", "建议仓位"],
         "rows": rows}], "data": best}


HELP = """我是这个平台内置的选股助手，所有数字都来自平台自己算出来的真实数据（不瞎编）。

你可以这样问我：

【今日选股】  「今天买什么」「推荐几只」
     -> 用当前最有效的因子给股票池打分排序，并用回调买入法给出买点、止损和建议仓位

【个股分析】  「分析一下 600519」「000001 怎么样」
     -> 报价 + 均线/RSI/MACD + 因子分 + 支撑压力 + 回调买入法诊断

【回调买入】  「有什么回调到支撑的股票」「止损怎么设」
     -> 专门列出符合回调买入法的标的，每个都给出支撑位、止损位和风险

【持仓体检】  「我的持仓怎么样」
     -> 每只的盈亏、占比，超仓位上限和亏损过大的会提醒你

【大盘状态】  「今天大盘怎么样」
     -> 指数、涨跌榜、市场状态

【因子咨询】  「现在哪个因子有效」
     -> 上一次因子扫描的排行榜

我在本地就能全部回答，不需要联网、不需要 API Key。
如果你在【设置】里填了大模型 API（DeepSeek / OpenAI 兼容的都行），
我会先算好数据、再让大模型把这些数据写成人话 —— 它只能组织语言，不能编数字。

⚠️ 最后提醒一句：我给的是统计结果，不是投资建议。买之前自己再看一眼，务必设止损。"""


def _fmt_help() -> dict:
    return {"answer": HELP, "blocks": [{"type": "text", "text": HELP}]}


def _fmt_backtest() -> dict:
    t = ("回测在【回测】标签页：\n\n"
         "1. 选策略（比如「回调买入法」或「多因子选股轮动」）\n"
         "2. 填股票池，比如 000001.SZ,600036.SH,600519.SH\n"
         "3. 选个开始日期，点【开始回测】\n"
         "4. 重点看：年化收益、最大回撤、夏普比率、对比沪深300\n\n"
         "记住两件事：\n"
         "  · 回撤比收益重要。回撤 40% 意味着本金要腰斩。\n"
         "  · 跑不赢沪深300 的策略，别拿去实盘。\n\n"
         "另外：如果股价太贵、资金买不起一手（100 股），会一笔都不成交，"
         "回测结果里会用黄框告诉你原因。")
    return {"answer": t, "blocks": [{"type": "text", "text": t}]}


# ---------------------------------------------------------------------------
# 大模型润色（可选）
# ---------------------------------------------------------------------------
def _llm(prompt: str, facts: str) -> str:
    cfg = config.get("assistant", {}) or {}
    if not cfg.get("api_key") or not cfg.get("enabled"):
        return ""
    base = (cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    model = cfg.get("model") or "gpt-4o-mini"
    timeout = float(cfg.get("timeout", 60) or 60)
    sys_prompt = (
        "你是一个 A 股量化助手。下面会给你一段由本地程序算出来的真实数据。"
        "请把数据整理成简明易懂的中文回答，像跟一个刚入门的朋友说话。"
        "【硬性要求】只能使用给定数据里的数字，绝对不许自己编造或推测任何数值、"
        "股票代码、价格或涨跌幅。如果数据不够回答问题，就直接说数据不够。"
        "最后一定要提醒：这只是统计结果，不构成投资建议。"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": "【我的问题】%s\n\n【平台算出的真实数据】\n%s" % (prompt, facts)},
        ],
        "temperature": 0.3,
        "max_tokens": int(cfg.get("max_tokens", 1200) or 1200),
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % cfg["api_key"]},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            js = json.loads(r.read().decode("utf-8", "replace"))
        return (js.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    except Exception as e:  # noqa: BLE001
        store.log("WARN", "assistant", "调用大模型失败: %s" % str(e)[:200])
        return ""


def test_llm() -> dict:
    cfg = config.get("assistant", {}) or {}
    if not cfg.get("api_key"):
        return {"ok": False, "message": "还没填 API Key"}
    out = _llm("请回复：连接成功", "模型名=%s" % cfg.get("model"))
    if out:
        return {"ok": True, "message": "连接成功", "reply": out[:200]}
    return {"ok": False, "message": "调用失败，请检查 API 地址 / Key / 模型名，或看看日志"}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def ask(question: str) -> dict:
    q = (question or "").strip()
    intent = detect_intent(q)
    try:
        if intent == "help":
            res = _fmt_help()
        elif intent == "positions":
            res = _fmt_positions(portfolio_check())
        elif intent == "market":
            res = _fmt_market()
        elif intent == "factors":
            res = _fmt_factors()
        elif intent == "pullback":
            res = _fmt_pullback(q)
        elif intent == "backtest":
            res = _fmt_backtest()
        elif intent.startswith("analyze:"):
            res = _fmt_analyze(analyze_stock(intent.split(":", 1)[1]))
        elif intent == "picks":
            res = _fmt_picks(daily_picks(8), 8)
        else:
            res = {"answer": ("我没太听懂这个问题。你可以换成这几种问法试试：\n"
                              "  · 今天买什么\n  · 分析一下 600519\n"
                              "  · 我的持仓怎么样\n  · 今天大盘怎么样\n"
                              "  · 现在哪个因子有效\n\n输入「帮助」看完整说明。"),
                   "blocks": [{"type": "text", "text": "输入「帮助」查看我能回答什么。"}]}
    except Exception as e:  # noqa: BLE001
        store.log("ERROR", "assistant", "回答失败: %s" % str(e)[:200])
        return {"ok": False, "intent": intent, "answer": "出错了：%s" % str(e)[:200], "blocks": []}

    res["ok"] = True
    res["intent"] = intent
    res["model_source"] = ""
    # 可选：交给大模型润色（数据仍然是平台算的）
    cfg = config.get("assistant", {}) or {}
    if cfg.get("enabled") and cfg.get("api_key"):
        facts = json.dumps(res.get("data", {}), ensure_ascii=False, default=str)[:6000]
        polished = _llm(q, res.get("answer", "") + "\n\n原始数据：\n" + facts)
        if polished:
            res["answer"] = polished
            res["blocks"] = [{"type": "text", "text": polished}] + res.get("blocks", [])
            res["llm_used"] = True
    return res
