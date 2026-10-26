# -*- coding: utf-8 -*-
"""策略库

每个策略做一件事：给它一只股票的完整历史K线（从最早到今天，按时间升序），
它返回一串买卖信号。信号里带 bar 序号 i，表示"第 i 根K线收盘时决定了要买/要卖"，
真正成交会放在第 i+1 根开盘（回测时），避免用到未来数据。

约定：
    signals(symbol, klines) -> [{"i": int, "action": "buy"/"sell",
                                 "reason": str, "weight": float|None}, ...]
    weight 表示这一笔想用总资产的百分之多少（0.2 = 20%），None 表示用默认值。
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any

from . import indicators as ind
from . import datasource as ds
from . import factors as fac
from . import factorlab as flab
from . import optimizer as opt


# ---------------------------------------------------------------------------
# 基类与注册表
# ---------------------------------------------------------------------------
def day_no(d: str) -> int:
    """日期 -> 连续日序号（自 2000-01-01 起的天数）。

    调仓日必须用「日历序号」判断，不能用 all_dates 的数组下标 di。
    因为 all_dates 是所有股票日期的【并集】：股票池里只要有一只票停牌、
    或者盘中数据还没补齐，这个数组的长度就变了，同一个日期算出来的 di
    也跟着变，di % reb 的结果直接翻转 —— 策略就会在「该调仓」和
    「不该调仓」之间反复横跳，表现就是同一只股票被反复买进卖出，
    而且当天买的当天卖（T+1 又卖不掉），一天刷出几百张废单。
    """
    try:
        return (dt.date.fromisoformat(str(d)[:10]) - dt.date(2000, 1, 1)).days
    except Exception:  # noqa: BLE001
        return 0


class Strategy:
    key = ""
    name = ""
    desc = ""
    defaults: dict = {}
    schema: list = []
    portfolio = False   # True 表示需要同时看多只股票（如轮动）
    carry_state = True  # 回测起点之前若已发出买入信号，回测开始时是否自动建仓

    def __init__(self, params: dict | None = None, symbols: list | None = None):
        self.p = dict(self.defaults)
        self.p.update(params or {})
        self.symbols = symbols or []

    # 参数读取工具 ---------------------------------------------------------
    def num(self, k, default=0.0):
        try:
            return float(self.p.get(k, default))
        except Exception:
            return float(default)

    def integer(self, k, default=0):
        try:
            return int(float(self.p.get(k, default)))
        except Exception:
            return int(default)

    def flag(self, k, default=False):
        v = self.p.get(k, default)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(v)

    def text(self, k, default=""):
        v = self.p.get(k, default)
        return str(v) if v is not None else default

    # 子类实现 -------------------------------------------------------------
    def signals(self, symbol: str, klines: list) -> list:
        raise NotImplementedError

    def signals_multi(self, data: dict) -> dict:
        """默认逐只股票独立算；组合策略（如轮动）可覆盖"""
        out = {}
        for sym, ks in data.items():
            try:
                out[sym] = self.signals(sym, ks)
            except Exception as e:  # noqa: BLE001
                from . import store
                store.log("ERROR", "strategy", "[%s] 计算 %s 的信号时出错: %s" % (self.key, sym, e))
                out[sym] = []
        return out

    def describe(self) -> dict:
        sch = self.schema() if callable(self.schema) else self.schema
        return {"key": self.key, "name": self.name, "desc": self.desc,
                "defaults": self.defaults, "schema": sch,
                "portfolio": self.portfolio}


REGISTRY: dict[str, type] = {}


def register(cls):
    REGISTRY[cls.key] = cls
    return cls


def get_strategy(key: str, params=None, symbols=None) -> Strategy:
    cls = REGISTRY.get(key)
    if cls is None:
        raise KeyError("没有这个策略: %s（可用: %s）" % (key, ",".join(REGISTRY)))
    return cls(params, symbols)


def all_templates() -> list:
    out = []
    for cls in REGISTRY.values():
        out.append(cls().describe())
    return sorted(out, key=lambda x: x["name"])


def _closes(klines):
    return [k["close"] for k in klines]


def _highest(vals, n):
    return ind.highest(vals, n)


def _lowest(vals, n):
    return ind.lowest(vals, n)


# ---------------------------------------------------------------------------
# 1. 双均线交叉
# ---------------------------------------------------------------------------
@register
class MaCross(Strategy):
    key = "ma_cross"
    name = "双均线交叉"
    desc = "快线上穿慢线买入（金叉），快线下穿慢线卖出（死叉）。最经典的趋势策略，适合有明显趋势的行情。"
    defaults = {"fast": 5, "slow": 20, "ma_type": "ema", "stop_loss_pct": 0.0, "take_profit_pct": 0.0}
    schema = [
        {"name": "fast", "label": "快线周期", "type": "int", "default": 5, "min": 2, "max": 120,
         "help": "反应更快的均线，数字越小越敏感"},
        {"name": "slow", "label": "慢线周期", "type": "int", "default": 20, "min": 3, "max": 250,
         "help": "反应更慢的均线，用作趋势方向"},
        {"name": "ma_type", "label": "均线类型", "type": "select", "default": "ema",
         "options": [{"value": "ema", "label": "EMA 指数均线"}, {"value": "sma", "label": "SMA 简单均线"}]},
        {"name": "stop_loss_pct", "label": "止损(%)", "type": "float", "default": 0.0, "min": 0, "max": 50,
         "help": "0 表示不启用。例如填 8 表示从买入价跌 8% 就卖出"},
        {"name": "take_profit_pct", "label": "止盈(%)", "type": "float", "default": 0.0, "min": 0, "max": 200,
         "help": "0 表示不启用"},
    ]

    def signals(self, symbol, klines):
        c = _closes(klines)
        if len(c) < 5:
            return []
        f, s = self.integer("fast", 5), self.integer("slow", 20)
        if f >= s:
            f = max(2, s - 1)
        fn = ind.ema if self.text("ma_type", "ema") == "ema" else ind.sma
        fa, sa = fn(c, f), fn(c, s)
        sl = self.num("stop_loss_pct", 0.0)
        tp = self.num("take_profit_pct", 0.0)
        out = []
        entry = None
        for i in range(1, len(c)):
            if entry is not None and (sl > 0 or tp > 0):
                ret = (c[i] / entry - 1.0) * 100.0
                if sl > 0 and ret <= -sl:
                    out.append({"i": i, "action": "sell", "reason": "止损 -%.2f%%" % abs(ret)})
                    entry = None
                    continue
                if tp > 0 and ret >= tp:
                    out.append({"i": i, "action": "sell", "reason": "止盈 +%.2f%%" % ret})
                    entry = None
                    continue
            if ind.crossover(fa, sa, i):
                out.append({"i": i, "action": "buy",
                            "reason": "%s%d 上穿 %s%d" % (self.text("ma_type", "ema").upper(), f,
                                                          self.text("ma_type", "ema").upper(), s)})
                entry = c[i]
            elif ind.crossunder(fa, sa, i):
                out.append({"i": i, "action": "sell",
                            "reason": "%s%d 下穿 %s%d" % (self.text("ma_type", "ema").upper(), f,
                                                          self.text("ma_type", "ema").upper(), s)})
                entry = None
        return out


# ---------------------------------------------------------------------------
# 2. MACD 金叉死叉
# ---------------------------------------------------------------------------
@register
class MacdCross(Strategy):
    key = "macd_cross"
    name = "MACD 金叉死叉"
    desc = "DIF 上穿 DEA（金叉）买入，DIF 下穿 DEA（死叉）卖出。可只在水下/水上金叉时交易。"
    defaults = {"fast": 12, "slow": 26, "signal": 9, "below_zero_only": False, "above_zero_only": False}
    schema = [
        {"name": "fast", "label": "快线 EMA", "type": "int", "default": 12, "min": 2, "max": 100},
        {"name": "slow", "label": "慢线 EMA", "type": "int", "default": 26, "min": 3, "max": 200},
        {"name": "signal", "label": "信号线", "type": "int", "default": 9, "min": 2, "max": 100},
        {"name": "below_zero_only", "label": "只做零轴下金叉", "type": "bool", "default": False,
         "help": "严格一些，只做超跌反弹"},
        {"name": "above_zero_only", "label": "只做零轴上金叉", "type": "bool", "default": False,
         "help": "只做强势股"},
    ]

    def signals(self, symbol, klines):
        c = _closes(klines)
        if len(c) < 35:
            return []
        dif, dea, _ = ind.macd(c, self.integer("fast", 12), self.integer("slow", 26), self.integer("signal", 9))
        out = []
        for i in range(1, len(c)):
            if ind.crossover(dif, dea, i):
                if self.flag("below_zero_only") and not (dif[i] is not None and dif[i] < 0):
                    continue
                if self.flag("above_zero_only") and not (dif[i] is not None and dif[i] > 0):
                    continue
                out.append({"i": i, "action": "buy", "reason": "MACD 金叉 (DIF=%.3f)" % (dif[i] or 0)})
            elif ind.crossunder(dif, dea, i):
                out.append({"i": i, "action": "sell", "reason": "MACD 死叉 (DIF=%.3f)" % (dif[i] or 0)})
        return out


# ---------------------------------------------------------------------------
# 3. RSI 超买超卖
# ---------------------------------------------------------------------------
@register
class RsiReversal(Strategy):
    key = "rsi_reversal"
    name = "RSI 超买超卖"
    desc = "RSI 从超卖区向上抬升买入，从超买区向下回落卖出。震荡市效果好，单边趋势中容易反复。"
    defaults = {"n": 14, "buy_below": 30, "sell_above": 70, "stop_loss_pct": 8.0}
    schema = [
        {"name": "n", "label": "RSI 周期", "type": "int", "default": 14, "min": 2, "max": 60},
        {"name": "buy_below", "label": "买入阈值", "type": "float", "default": 30, "min": 5, "max": 50,
         "help": "RSI 从下方上穿该值买入"},
        {"name": "sell_above", "label": "卖出阈值", "type": "float", "default": 70, "min": 50, "max": 95,
         "help": "RSI 从上方下穿该值卖出"},
        {"name": "stop_loss_pct", "label": "止损(%)", "type": "float", "default": 8.0, "min": 0, "max": 50},
    ]

    def signals(self, symbol, klines):
        c = _closes(klines)
        n = self.integer("n", 14)
        if len(c) < n + 3:
            return []
        r = ind.rsi(c, n)
        bb, sa = self.num("buy_below", 30), self.num("sell_above", 70)
        sl = self.num("stop_loss_pct", 0.0)
        out, entry = [], None
        for i in range(1, len(c)):
            if r[i] is None or r[i - 1] is None:
                continue
            if entry is not None and sl > 0:
                ret = (c[i] / entry - 1.0) * 100.0
                if ret <= -sl:
                    out.append({"i": i, "action": "sell", "reason": "止损 %.2f%%" % ret})
                    entry = None
                    continue
            if r[i - 1] <= bb < r[i]:
                out.append({"i": i, "action": "buy", "reason": "RSI 上穿 %.0f (当前 %.1f)" % (bb, r[i])})
                entry = c[i]
            elif r[i - 1] >= sa > r[i]:
                out.append({"i": i, "action": "sell", "reason": "RSI 下穿 %.0f (当前 %.1f)" % (sa, r[i])})
                entry = None
        return out


# ---------------------------------------------------------------------------
# 4. 布林带
# ---------------------------------------------------------------------------
@register
class BollStrategy(Strategy):
    key = "boll"
    name = "布林带突破 / 回归"
    desc = "突破模式：收盘上穿布林上轨追涨，跌破中轨离场。回归模式：跌破下轨抄底，回到中轨卖出。"
    defaults = {"n": 20, "k": 2.0, "mode": "breakout"}
    schema = [
        {"name": "n", "label": "周期", "type": "int", "default": 20, "min": 5, "max": 120},
        {"name": "k", "label": "带宽倍数", "type": "float", "default": 2.0, "min": 0.5, "max": 4.0, "step": 0.1},
        {"name": "mode", "label": "模式", "type": "select", "default": "breakout",
         "options": [{"value": "breakout", "label": "突破追涨"}, {"value": "revert", "label": "均值回归"}]},
    ]

    def signals(self, symbol, klines):
        c = _closes(klines)
        n = self.integer("n", 20)
        if len(c) < n + 3:
            return []
        mid, up, low = ind.boll(c, n, self.num("k", 2.0))
        mode = self.text("mode", "breakout")
        out, holding = [], False
        for i in range(1, len(c)):
            if (mid[i] is None or up[i] is None or low[i] is None
                    or mid[i - 1] is None or up[i - 1] is None or low[i - 1] is None):
                continue
            if mode == "breakout":
                if not holding and c[i - 1] <= up[i - 1] and c[i] > up[i]:
                    out.append({"i": i, "action": "buy", "reason": "向上突破布林上轨 %.2f" % up[i]})
                    holding = True
                elif holding and c[i - 1] >= mid[i - 1] and c[i] < mid[i]:
                    out.append({"i": i, "action": "sell", "reason": "跌破布林中轨 %.2f" % mid[i]})
                    holding = False
            else:
                if not holding and c[i - 1] >= low[i - 1] and c[i] < low[i]:
                    out.append({"i": i, "action": "buy", "reason": "跌破布林下轨 %.2f 抄底" % low[i]})
                    holding = True
                elif holding and c[i - 1] <= mid[i - 1] and c[i] > mid[i]:
                    out.append({"i": i, "action": "sell", "reason": "回到布林中轨 %.2f 卖出" % mid[i]})
                    holding = False
        return out


# ---------------------------------------------------------------------------
# 5. 唐奇安通道突破（海龟简化版）
# ---------------------------------------------------------------------------
@register
class DonchianBreakout(Strategy):
    key = "donchian"
    name = "唐奇安通道突破（海龟）"
    desc = "收盘价创 N 日新高买入，跌破 M 日新低卖出，可选 ATR 动态止损。趋势跟踪的经典做法。"
    defaults = {"entry_n": 20, "exit_n": 10, "atr_n": 14, "atr_stop": 2.0, "use_atr_stop": False}
    schema = [
        {"name": "entry_n", "label": "入场周期", "type": "int", "default": 20, "min": 3, "max": 250,
         "help": "突破多少日最高价买入"},
        {"name": "exit_n", "label": "出场周期", "type": "int", "default": 10, "min": 2, "max": 250,
         "help": "跌破多少日最低价卖出"},
        {"name": "use_atr_stop", "label": "启用ATR止损", "type": "bool", "default": False},
        {"name": "atr_n", "label": "ATR周期", "type": "int", "default": 14, "min": 3, "max": 60},
        {"name": "atr_stop", "label": "ATR止损倍数", "type": "float", "default": 2.0, "min": 0.5, "max": 8.0, "step": 0.1},
    ]

    def signals(self, symbol, klines):
        if len(klines) < 30:
            return []
        c = _closes(klines)
        hs = [k["high"] for k in klines]
        ls = [k["low"] for k in klines]
        en, xn = self.integer("entry_n", 20), self.integer("exit_n", 10)
        hh = _highest(hs, en)
        ll = _lowest(ls, xn)
        use_atr = self.flag("use_atr_stop")
        atr = ind.atr(hs, ls, c, self.integer("atr_n", 14)) if use_atr else None
        mult = self.num("atr_stop", 2.0)
        out, holding, peak = [], False, None
        for i in range(1, len(c)):
            if hh[i - 1] is None or ll[i - 1] is None:
                continue
            if holding:
                peak = max(peak or c[i], c[i])
                if use_atr and atr and atr[i] is not None and c[i] < peak - mult * atr[i]:
                    out.append({"i": i, "action": "sell", "reason": "ATR 跟踪止损 (回撤超 %.1f倍ATR)" % mult})
                    holding, peak = False, None
                    continue
                if c[i] < ll[i - 1]:
                    out.append({"i": i, "action": "sell", "reason": "跌破 %d 日最低价 %.2f" % (xn, ll[i - 1])})
                    holding, peak = False, None
            else:
                if c[i] > hh[i - 1]:
                    out.append({"i": i, "action": "buy", "reason": "突破 %d 日最高价 %.2f" % (en, hh[i - 1])})
                    holding, peak = True, c[i]
        return out


# ---------------------------------------------------------------------------
# 6. 择时网格（ATR 分档）
#
# 复现：广发证券金融工程《网格交易叠加多维度行业优选：提升行业配置组合收益风险比》
#      （量化资产配置研究之二十八，2026/10/21）
#
# 原文三个部件，这里一一对应：
#   1) ATR / NATR：TR_t = max(H-L, |H-C_{t-1}|, |L-C_{t-1}|)，ATR 取 N 日简单平均；
#      价格中枢 = N 日收盘均值；NATR = (C - 中枢) / ATR，即"现在离中枢几个波动单位"。
#   2) 网格权重表（原文表1）：每 1 个 ATR 一档，每档调整 10%，最多 ±5 档（±50%）。
#      高于中枢减仓、低于中枢加仓。
#   3) 择时信号：ATR 下行（趋势加强）时，价格涨→看多、跌→看空；ATR 上行（波动加剧）时无信号。
#      择时通过两种方式作用到网格（原文表2）：
#        策略1 spacing —— 看多：上方格距 ×2、下方 ×0.5（少减仓、多加仓）；看空反之。
#        策略2 center  —— 看多：权重中枢调到 60%；看空：调到 40%。
#
# 与原文的差异（如实说明）：
#   · 原文作用在申万一级行业指数、等权组合上；这里作用在个股/ETF 的 K 线上，方法通用但
#     标的波动特征不同，参数要重新调。
#   · 原文是"调整配置权重"，回测引擎原来只会全平卖出。这里给卖单加了 fraction 分批卖，
#     所以能做出"减一档"而不是"一次清仓"。
#   · 原文没有止损；stop_loss_pct 默认 0（关闭），需要时再开。
# ---------------------------------------------------------------------------
@register
class GridStrategy(Strategy):
    key = "grid"
    name = "择时网格（ATR分档）"
    desc = ("按「价格离中枢几个 ATR」分档调仓位：跌得越深仓位越重、涨得越高仓位越轻，"
            "再用 ATR 趋势做择时（看多放宽上格距/抬高中枢）。"
            "复现广发金工《网格交易叠加多维度行业优选》（2026/10/21）。")
    defaults = {
        "atr_n": 20, "center_weight": 50.0, "step_weight": 10.0, "max_step": 5,
        "step_mode": "relative", "timing_mode": "none",
        "bull_center": 60.0, "bear_center": 40.0,
        "spacing_up": 2.0, "spacing_down": 0.5,
        "min_delta": 0.0, "hyst": 0.1, "stop_loss_pct": 0.0,
    }
    carry_state = False
    schema = [
        {"name": "atr_n", "label": "ATR/中枢周期", "type": "int", "default": 20, "min": 5, "max": 250,
         "help": "原文：N 日 TR 简单平均做 ATR，N 日收盘均值做价格中枢"},
        {"name": "center_weight", "label": "权重中枢(%)", "type": "float", "default": 50.0, "min": 0, "max": 100,
         "step": 1.0,
         "help": "价格正好在中枢时，这只票目标占总资产多少。单只回测常设 50；多只一起回测时总和别超 100"},
        {"name": "step_weight", "label": "每档调整幅度(%)", "type": "float", "default": 10.0, "min": 0.5, "max": 50,
         "step": 0.5, "help": "原文表1：每偏离 1 个 ATR 调整 10%"},
        {"name": "max_step", "label": "最多几档", "type": "int", "default": 5, "min": 1, "max": 20,
         "help": "原文表1：最多 ±5 档，即 ±50%"},
        {"name": "step_mode", "label": "档位口径", "type": "select", "default": "relative",
         "options": [{"value": "relative", "label": "相对中枢（原文口径：中枢×(1±50%)）"},
                     {"value": "absolute", "label": "绝对百分点（中枢±50pp，可到 0 或 100）"}]},
        {"name": "timing_mode", "label": "择时方式", "type": "select", "default": "none",
         "options": [{"value": "none", "label": "不择时（纯网格，原文对照组）"},
                     {"value": "spacing", "label": "策略1：看多放宽上格距/收紧下格距"},
                     {"value": "center", "label": "策略2：看多中枢60% / 看空40%"}]},
        {"name": "bull_center", "label": "看多时中枢(%)", "type": "float", "default": 60.0, "min": 0, "max": 100,
         "step": 1.0},
        {"name": "bear_center", "label": "看空时中枢(%)", "type": "float", "default": 40.0, "min": 0, "max": 100,
         "step": 1.0},
        {"name": "spacing_up", "label": "上方格距倍数", "type": "float", "default": 2.0, "min": 0.1, "max": 5.0,
         "step": 0.1, "help": "择时策略1：原文看多时上方格距×2、下方×0.5；看空时反过来"},
        {"name": "spacing_down", "label": "下方格距倍数", "type": "float", "default": 0.5, "min": 0.1, "max": 5.0,
         "step": 0.1},
        {"name": "min_delta", "label": "最小调仓幅度(pp)", "type": "float", "default": 0.0, "min": 0, "max": 20,
         "step": 0.5, "help": "目标仓位变化小于这个值就不动。默认 0（完全按原文）"},
        {"name": "hyst", "label": "档位迟滞(个ATR)", "type": "float", "default": 0.1, "min": 0, "max": 1.0,
         "step": 0.05,
         "help": "NATR 卡在档位边界(如刚好1.0)会来回穿、每次都触发买卖。设 0.1 表示要越过边界 0.1 个 ATR 才换档，能大幅减少无谓交易。0 = 原文严格口径"},
        {"name": "stop_loss_pct", "label": "回撤止损(%)", "type": "float", "default": 0.0, "min": 0, "max": 90,
         "step": 1.0,
         "help": "原文没有这一项，默认 0 关闭。>0 时从建网以来最高点回撤这么多就清仓，等 NATR 回到 -1 以内再重新开网"},
    ]

    def _timing(self, atr_series, closes, i):
        """原文择时信号。

        ATR 下行（波动收敛、趋势加强）才给信号：
          价格涨 → +1（看多）；价格跌 → -1（看空）
        ATR 上行（波动放大）→ 0（无信号，不动）
        原文用 1 日均线刻画趋势，等价于直接和上一根比。
        """
        if i < 1:
            return 0
        a0, a1 = atr_series[i], atr_series[i - 1]
        if a0 is None or a1 is None:
            return 0
        if a0 >= a1:          # ATR 上行或持平 → 无择时信号
            return 0
        if closes[i] > closes[i - 1]:
            return 1
        if closes[i] < closes[i - 1]:
            return -1
        return 0

    def _target_weight(self, natr_v, sig, level_cur=None, hyst=0.0):
        """按 NATR 分档算目标仓位（原文表1），择时按表2 改变格距或中枢。

        返回 (目标权重%, 档位 level)。level 带符号：正=在中枢上方(减仓)，负=下方(加仓)。

        迟滞(hyst)：NATR 刚好卡在 1.0 这种档位边界上时会来回跳，
        每跳一次就买卖一次。hyst 让"升档"要越过边界多走一点才算，
        把这种噪声挡掉。hyst=0 就是原文的严格口径。
        """
        mode = self.text("timing_mode", "none")
        center = self.num("center_weight", 50.0)
        step_up, step_down = 1.0, 1.0

        if mode == "spacing":
            up_m = max(0.05, self.num("spacing_up", 2.0))
            dn_m = max(0.05, self.num("spacing_down", 0.5))
            if sig > 0:            # 看多：上方放宽(少减仓)、下方收紧(多加仓)
                step_up, step_down = up_m, dn_m
            elif sig < 0:          # 看空：反过来
                step_up, step_down = dn_m, up_m
        elif mode == "center":
            if sig > 0:
                center = self.num("bull_center", 60.0)
            elif sig < 0:
                center = self.num("bear_center", 40.0)

        max_step = max(1, self.integer("max_step", 5))
        # 换算成"已经走了几个格子"：上方用 step_up 量，下方用 step_down 量
        x = natr_v / step_up if natr_v >= 0 else natr_v / step_down
        side = 1 if x >= 0 else -1
        m = abs(x)

        if level_cur is None:
            km = int(m)
        elif hyst <= 0:
            km = int(m)                       # 原文严格口径：直接按区间取整
        else:
            km_raw = int(m)
            km_cur = abs(level_cur)
            if side != (1 if level_cur >= 0 else -1):
                km = km_raw                   # 穿过中枢（正负翻转），重新起算
            elif km_raw > km_cur:
                # 升档：必须越过"当前档上边界"再走 hyst 才算数；
                # 一次跨好几档就直接到位（否则每档一笔，白交手续费）
                km = km_raw if m >= km_cur + 1 + hyst else km_cur
            elif km_raw < km_cur:
                km = km_raw if m <= km_cur - hyst else km_cur
            else:
                km = km_cur
        km = max(0, min(km, max_step))
        level = side * km

        amt = max(0.0, self.num("step_weight", 10.0))
        if self.text("step_mode", "relative") == "absolute":
            w = center - level * amt
        else:
            w = center * (1.0 - level * amt / 100.0)
        return max(0.0, min(100.0, w)), level

    def signals(self, symbol, klines):
        n = max(2, self.integer("atr_n", 20))
        if len(klines) < n + 2:
            return []
        closes = [float(k["close"]) for k in klines]
        highs = [float(k.get("high") or k["close"]) for k in klines]
        lows = [float(k.get("low") or k["close"]) for k in klines]

        natr = ind.natr(highs, lows, closes, n)
        atr_ = ind.atr_sma(highs, lows, closes, n)

        eps = max(0.0, self.num("min_delta", 0.0)) + 1e-9
        sl = max(0.0, self.num("stop_loss_pct", 0.0))

        out = []
        prev_w = 0.0        # 上一次下发的目标仓位（只用来判断"变没变"，不参与算股数）
        level_cur = None    # 当前档位，迟滞用
        hyst = max(0.0, self.num("hyst", 0.1))
        halt = False        # 止损后暂停开网
        peak = None
        for i in range(len(klines)):
            v = natr[i]
            if v is None:
                continue

            # ---- 可选止损（原文没有，默认关闭）----
            if sl > 0.0:
                peak = closes[i] if peak is None else max(peak, closes[i])
                if not halt and peak > 0 and (closes[i] / peak - 1.0) * 100.0 <= -sl:
                    if prev_w > 1e-9:
                        out.append({"i": i, "action": "sell", "target_weight": 0.0,
                                    "reason": "回撤止损：自最高 %.2f 跌 %.1f%%，清仓"
                                              % (peak, (closes[i] / peak - 1.0) * 100.0)})
                        prev_w = 0.0
                    halt = True
                    peak = None
                    level_cur = None
                    continue
                if halt:
                    # 价格回到中枢下方 1 个 ATR 以内，视为重新具备开网条件
                    if v > -1.0:
                        halt = False
                        peak = closes[i]
                    else:
                        continue

            sig = self._timing(atr_, closes, i)
            target, level = self._target_weight(v, sig, level_cur, hyst)
            level_cur = level
            if abs(target - prev_w) <= eps:
                continue    # 目标没变，不动

            # 只下发「目标仓位」，买多少/卖多少交给回测引擎按实际持仓算。
            # 这样不会出现「策略以为只减了 5%，实际被一次性清仓」这种脱节。
            out.append({"i": i,
                        "action": "sell" if target < prev_w else "buy",
                        "target_weight": round(target, 4),
                        "reason": "NATR=%.2f 第%+d档 → 目标仓位 %.0f%%" % (v, level, target)})
            prev_w = target
        return out


# ---------------------------------------------------------------------------
# 7. 双推力 Dual Thrust
# ---------------------------------------------------------------------------
@register
class DualThrust(Strategy):
    key = "dual_thrust"
    name = "双推力 Dual Thrust"
    desc = "以前 N 日的高低波动幅度算出上下轨，突破上轨做多、跌破下轨平仓。经典的日内/短线突破策略。"
    defaults = {"n": 5, "k1": 0.5, "k2": 0.5}
    schema = [
        {"name": "n", "label": "回看天数", "type": "int", "default": 5, "min": 1, "max": 60},
        {"name": "k1", "label": "上轨系数 k1", "type": "float", "default": 0.5, "min": 0.05, "max": 2.0, "step": 0.05},
        {"name": "k2", "label": "下轨系数 k2", "type": "float", "default": 0.5, "min": 0.05, "max": 2.0, "step": 0.05},
    ]

    def signals(self, symbol, klines):
        if len(klines) < self.integer("n", 5) + 3:
            return []
        n = self.integer("n", 5)
        k1, k2 = self.num("k1", 0.5), self.num("k2", 0.5)
        out, holding = [], False
        for i in range(n, len(klines)):
            seg = klines[i - n:i]
            hh = max(x["high"] for x in seg)
            ll = min(x["low"] for x in seg)
            hc = max(x["close"] for x in seg)
            lc = min(x["close"] for x in seg)
            rng = max(hh - lc, hc - ll)
            op = klines[i]["open"]
            upper = op + k1 * rng
            lower = op - k2 * rng
            cl = klines[i]["close"]
            if not holding and cl > upper:
                out.append({"i": i, "action": "buy", "reason": "突破上轨 %.2f (收盘 %.2f)" % (upper, cl)})
                holding = True
            elif holding and cl < lower:
                out.append({"i": i, "action": "sell", "reason": "跌破下轨 %.2f (收盘 %.2f)" % (lower, cl)})
                holding = False
        return out


# ---------------------------------------------------------------------------
# 8. 动量轮动（组合策略）
# ---------------------------------------------------------------------------
@register
class MomentumRotation(Strategy):
    key = "momentum_rotation"
    name = "动量轮动（多股票）"
    desc = "每隔 N 天给所有候选股票按涨幅排名，买入最强的 K 只，卖掉掉出榜的。需要选多只股票，适合做 ETF 轮动。"
    defaults = {"lookback": 20, "top_k": 2, "rebalance_days": 5, "min_momentum": -999.0}
    schema = [
        {"name": "lookback", "label": "动量回看天数", "type": "int", "default": 20, "min": 3, "max": 250},
        {"name": "top_k", "label": "持有前几名", "type": "int", "default": 2, "min": 1, "max": 10},
        {"name": "rebalance_days", "label": "调仓间隔(天)", "type": "int", "default": 5, "min": 1, "max": 60},
        {"name": "min_momentum", "label": "最低动量(%)", "type": "float", "default": -999.0,
         "help": "动量为负且低于该值就空仓，设 0 表示只在上涨时持有"},
    ]
    portfolio = True

    def signals_multi(self, data):
        out = {s: [] for s in data}
        # 用所有股票都有的日期序列对齐
        date_index = {}
        for sym, ks in data.items():
            date_index[sym] = {k["date"]: i for i, k in enumerate(ks)}
        all_dates = sorted(set().union(*[set(v.keys()) for v in date_index.values()])) if date_index else []
        if not all_dates:
            return out
        lb = self.integer("lookback", 20)
        topk = self.integer("top_k", 2)
        reb = max(1, self.integer("rebalance_days", 5))
        minm = self.num("min_momentum", -999.0)
        holding: dict = {}
        for di, date in enumerate(all_dates):
            # 预热期用数组下标（语义是「至少要有 lb 根K线」）；
            # 调仓日改用日历序号，保证同一个日期永远是同一个判断结果。
            if di < lb or day_no(date) % reb != 0:
                continue
            scores = []
            for sym, ks in data.items():
                idx = date_index[sym].get(date)
                if idx is None or idx < lb:
                    continue
                mom = (ks[idx]["close"] / ks[idx - lb]["close"] - 1.0) * 100.0
                scores.append((mom, sym, idx))
            scores.sort(reverse=True)
            picked = {s for m, s, _ in scores[:topk] if m >= minm}
            for sym in list(holding):
                if sym not in picked:
                    idx = date_index[sym].get(date)
                    if idx is not None:
                        out[sym].append({"i": idx, "action": "sell", "reason": "掉出动量前 %d 名" % topk})
                    holding.pop(sym, None)
            weight = 100.0 / max(len(picked), 1)   # 单位是"占总资产的百分比"
            for sym in picked:
                if sym in holding:
                    continue
                idx = date_index[sym].get(date)
                if idx is None:
                    continue
                mom = next(m for m, s, _ in scores if s == sym)
                out[sym].append({"i": idx, "action": "buy",
                                 "reason": "动量排名入选 (%.1f%%)" % mom, "weight": weight})
                holding[sym] = True
        # 收尾：最后一根K线清掉所有持仓（回测会按最后价格结算，这里只是标记）
        return out


# ---------------------------------------------------------------------------
# 9. 买入持有（基准）
# ---------------------------------------------------------------------------
@register
class BuyHold(Strategy):
    key = "buy_hold"
    name = "买入并持有（基准）"
    desc = "第一天买入一直拿到最后。用来对比：如果你的策略跑不赢它，那就别费劲了。"
    defaults = {}
    schema = []

    def signals(self, symbol, klines):
        if len(klines) < 2:
            return []
        return [{"i": 0, "action": "buy", "reason": "买入持有"}]


# ---------------------------------------------------------------------------
# 10. 均线趋势 + 大盘过滤
# ---------------------------------------------------------------------------
@register
class TrendFilter(Strategy):
    key = "trend_filter"
    name = "趋势跟随 + 均线过滤"
    desc = "价格在长期均线上方才允许买入，快线上穿慢线建仓；跌破长期均线立刻清仓。多一层过滤，减少熊市里的无效交易。"
    defaults = {"fast": 10, "slow": 30, "filter_ma": 120, "stop_loss_pct": 7.0}
    schema = [
        {"name": "fast", "label": "快线", "type": "int", "default": 10, "min": 2, "max": 120},
        {"name": "slow", "label": "慢线", "type": "int", "default": 30, "min": 3, "max": 250},
        {"name": "filter_ma", "label": "过滤均线", "type": "int", "default": 120, "min": 0, "max": 250,
         "help": "0 表示不用过滤。常用 120 日（半年线）或 250 日（年线）"},
        {"name": "stop_loss_pct", "label": "止损(%)", "type": "float", "default": 7.0, "min": 0, "max": 50},
    ]

    def signals(self, symbol, klines):
        c = _closes(klines)
        f, s = self.integer("fast", 10), self.integer("slow", 30)
        fm = self.integer("filter_ma", 120)
        if len(c) < max(s, fm if fm > 0 else 0) + 3:
            return []
        fa, sa = ind.ema(c, f), ind.ema(c, s)
        filt = ind.sma(c, fm) if fm > 0 else None
        sl = self.num("stop_loss_pct", 0.0)
        out, holding, entry = [], False, None
        for i in range(1, len(c)):
            if fa[i] is None or sa[i] is None:
                continue
            if holding:
                if sl > 0 and entry and (c[i] / entry - 1.0) * 100.0 <= -sl:
                    out.append({"i": i, "action": "sell", "reason": "止损 %.2f%%" % ((c[i] / entry - 1) * 100)})
                    holding, entry = False, None
                    continue
                if filt is not None and filt[i] is not None and c[i] < filt[i]:
                    out.append({"i": i, "action": "sell", "reason": "跌破 %d 日均线" % fm})
                    holding, entry = False, None
                    continue
                if ind.crossunder(fa, sa, i):
                    out.append({"i": i, "action": "sell", "reason": "快线下穿慢线"})
                    holding, entry = False, None
            else:
                if filt is not None and (filt[i] is None or c[i] < filt[i]):
                    continue
                if ind.crossover(fa, sa, i):
                    out.append({"i": i, "action": "buy", "reason": "趋势向上且快线上穿慢线"})
                    holding, entry = True, c[i]
        return out


# ===========================================================================
# 11. 单因子择时
# ===========================================================================
def _factor_options():
    """把 75 个因子做成下拉选项"""
    return [{"value": f["key"], "label": "%s · %s" % (f["family"], f["name"])}
            for f in fac.list_factors()]


def _preset_options():
    opts = [{"value": p["key"], "label": "%s — %s" % (p["name"], p["desc"][:22] + "…")}
            for p in fac.list_presets()]
    opts.append({"value": "custom", "label": "自定义因子组合（在下面手动填）"})
    return opts


def parse_combo(preset: str, custom: str) -> dict:
    """解析因子组合：可以用内置预设，也可以自己写「因子名:权重」"""
    if preset and preset != "custom":
        p = fac.get_preset(preset)
        if p:
            return dict(p["factors"])
    out = {}
    for part in str(custom or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            k, _, w = part.partition(":")
            k = k.strip()
            try:
                wv = float(w)
            except Exception:
                wv = 1.0
        else:
            k, wv = part, 1.0
        if k in fac.FACTORS:
            out[k] = wv
    return out


def _factor_score(symbol, klines, factor_key, lookback, direction):
    meta = fac.FACTORS.get(factor_key)
    if meta is None:
        raise KeyError("没有这个因子: %s" % factor_key)
    raw = fac.compute(factor_key, klines)
    d = meta["direction"]
    if direction == "positive":
        d = 1
    elif direction == "negative":
        d = -1
    signed = [None if v is None else v * d for v in raw]
    return fac.ts_rank(signed, int(lookback))


@register
class FactorTiming(Strategy):
    key = "factor_ts"
    name = "单因子择时"
    desc = ("挑一个因子，把它转成「当前值在过去 N 天里的百分位」(0~1)。"
            "百分位升到买入线以上就持有，跌破卖出线就空仓。"
            "这是把因子直接变成买卖信号最简单的方式。"
            "强烈建议先去【因子实验室】确认这个因子确实有 IC 再来用。")
    defaults = {"factor": "turnover_20_neg", "lookback": 120,
                "buy_threshold": 0.8, "sell_threshold": 0.2, "direction": "auto"}

    def schema(self):
        return [
            {"name": "factor", "label": "选择因子", "type": "select",
             "default": "turnover_20_neg", "options": _factor_options()},
            {"name": "lookback", "label": "百分位窗口(天)", "type": "int", "default": 120, "min": 20, "max": 500,
             "help": "当前因子值在过去多少天里的相对位置"},
            {"name": "buy_threshold", "label": "买入线(0~1)", "type": "float", "default": 0.8,
             "min": 0.5, "max": 0.99, "step": 0.05, "help": "因子百分位升到这条线以上就买入"},
            {"name": "sell_threshold", "label": "卖出线(0~1)", "type": "float", "default": 0.2,
             "min": 0.01, "max": 0.5, "step": 0.05, "help": "跌到这条线以下就卖出"},
            {"name": "direction", "label": "方向", "type": "select", "default": "auto",
             "options": [{"value": "auto", "label": "自动（按因子先验方向）"},
                         {"value": "positive", "label": "正向（值越大越买）"},
                         {"value": "negative", "label": "反向（值越小越买）"}]},
        ]

    def signals(self, symbol, klines):
        look = self.integer("lookback", 120)
        if len(klines) < look + 5:
            return []
        score = _factor_score(symbol, klines, self.text("factor"), look, self.text("direction", "auto"))
        buy_t = self.num("buy_threshold", 0.8)
        sell_t = self.num("sell_threshold", 0.2)
        out, holding = [], False
        for i in range(1, len(score)):
            if score[i] is None or score[i - 1] is None:
                continue
            if not holding and score[i - 1] <= buy_t < score[i]:
                out.append({"i": i, "action": "buy",
                            "reason": "因子百分位升到 %.2f（买入线 %.2f）" % (score[i], buy_t)})
                holding = True
            elif holding and score[i - 1] >= sell_t > score[i]:
                out.append({"i": i, "action": "sell",
                            "reason": "因子百分位跌破 %.2f（卖出线 %.2f）" % (score[i], sell_t)})
                holding = False
        return out


# ===========================================================================
# 12. 多因子综合打分
# ===========================================================================
@register
class FactorCombo(Strategy):
    key = "factor_multi"
    name = "多因子综合打分"
    desc = ("把几个因子按权重合成一个总分：每个因子先转成时序百分位，再按权重加权。"
            "总分高就持有。比单因子稳，因为不同因子之间可以互相纠错。"
            "可以直接用内置组合，也可以自己写「因子名:权重」。")
    defaults = {"preset": "preset_reversal", "custom": "", "lookback": 120,
                "buy_threshold": 0.8, "sell_threshold": 0.2}

    def schema(self):
        return [
            {"name": "preset", "label": "因子组合", "type": "select",
             "default": "preset_reversal", "options": _preset_options()},
            {"name": "custom", "label": "自定义组合", "type": "str", "default": "",
             "help": "只有上面选「自定义」时才生效。格式：因子名:权重，逗号分隔。"
                     "例如 rev_1:0.3,turnover_20_neg:0.3,mom_20:0.4"},
            {"name": "lookback", "label": "百分位窗口(天)", "type": "int", "default": 120,
             "min": 20, "max": 500},
            {"name": "buy_threshold", "label": "买入线(0~1)", "type": "float", "default": 0.8,
             "min": 0.5, "max": 0.99, "step": 0.05},
            {"name": "sell_threshold", "label": "卖出线(0~1)", "type": "float", "default": 0.2,
             "min": 0.01, "max": 0.5, "step": 0.05},
        ]

    def signals(self, symbol, klines):
        look = self.integer("lookback", 120)
        if len(klines) < look + 5:
            return []
        weights = parse_combo(self.text("preset"), self.text("custom"))
        if not weights:
            return []
        comp = flab.composite(klines, weights, "rank")
        score = fac.ts_rank(comp, look)
        buy_t = self.num("buy_threshold", 0.8)
        sell_t = self.num("sell_threshold", 0.2)
        out, holding = [], False
        for i in range(1, len(score)):
            if score[i] is None or score[i - 1] is None:
                continue
            if not holding and score[i - 1] <= buy_t < score[i]:
                out.append({"i": i, "action": "buy", "reason": "综合分升到 %.2f" % score[i]})
                holding = True
            elif holding and score[i - 1] >= sell_t > score[i]:
                out.append({"i": i, "action": "sell", "reason": "综合分跌破 %.2f" % score[i]})
                holding = False
        return out


# ===========================================================================
# 13. 多因子选股轮动（组合策略）
# ===========================================================================
@register
class FactorRank(Strategy):
    key = "factor_rank"
    name = "多因子选股轮动"
    desc = ("每隔 N 天给股票池里所有股票按因子总分排名，买入得分最高的 K 只，"
            "卖掉掉出榜单的。这是真正的「因子选股」玩法，股票池最好选 5 只以上。"
            "建议先用因子实验室确认因子确实有区分度。")
    defaults = {"preset": "preset_reversal", "custom": "", "top_k": 5,
                "rebalance_days": 10, "min_score": -999.0,
                "weight_mode": "risk_parity", "total_pct": 90.0, "max_single_pct": 20.0,
                "max_industry_pct": 0.0, "max_adv_pct": 0.0, "max_turnover_pct": 0.0,
                "capital": 200000.0, "ortho": False}
    portfolio = True

    def schema(self):
        opts = [{"value": m["key"], "label": "%s — %s" % (m["name"], m["desc"][:20] + "…")}
                for m in opt.METHODS]
        return [
            {"name": "preset", "label": "因子组合", "type": "select",
             "default": "preset_reversal", "options": _preset_options()},
            {"name": "custom", "label": "自定义组合", "type": "str", "default": "",
             "help": "格式：因子名:权重，逗号分隔"},
            {"name": "ortho", "label": "因子正交化", "type": "bool", "default": False,
             "help": "合成前先把高度相关的因子去相关，避免同族因子（如多个动量）重复计数"},
            {"name": "top_k", "label": "持有前几名", "type": "int", "default": 5, "min": 1, "max": 20},
            {"name": "rebalance_days", "label": "调仓间隔(天)", "type": "int", "default": 10,
             "min": 1, "max": 60},
            {"name": "weight_mode", "label": "权重分配方式（组合优化）", "type": "select",
             "default": "risk_parity", "options": opts,
             "help": "选出来的股票，每只买多少钱。风险平价和最小方差长期更稳，因子加权更激进"},
            {"name": "total_pct", "label": "总仓位上限(%)", "type": "float", "default": 90.0,
             "min": 10, "max": 100},
            {"name": "max_single_pct", "label": "单只上限(%)", "type": "float", "default": 20.0,
             "min": 2, "max": 100},
            {"name": "max_industry_pct", "label": "单行业上限(%)", "type": "float", "default": 0.0,
             "min": 0, "max": 100,
             "help": "0=不限制。例如 30 表示任一行业总仓位不超过 30%，避免整个组合押注单一行业"},
            {"name": "max_adv_pct", "label": "流动性上限(%成交额)", "type": "float", "default": 0.0,
             "min": 0, "max": 50,
             "help": "0=不限制。例如 10 表示单只仓位不超过该股日均成交额的 10%，防止建仓把自己买成对手盘"},
            {"name": "capital", "label": "资金规模(元)", "type": "float", "default": 200000.0,
             "min": 1000, "max": 1000000000,
             "help": "算流动性上限时用。请和回测/实盘的初始资金保持一致，资金越大流动性约束越紧"},
            {"name": "max_turnover_pct", "label": "换手上限(%)", "type": "float", "default": 0.0,
             "min": 0, "max": 100,
             "help": "0=不限制。每次调仓的买卖总变动不超过总资产的这个比例，控制交易成本"},
            {"name": "min_score", "label": "最低分数门槛", "type": "float", "default": -999.0,
             "help": "总分低于这个值就不买。填 -999 等于不限制；填 0.5 表示只在综合分较高时才持有"},
        ]

    def signals_multi(self, data):
        out = {s: [] for s in data}
        weights = parse_combo(self.text("preset"), self.text("custom"))
        if not weights:
            return out
        ortho = self.flag("ortho", False)
        scores, date_index = {}, {}
        for sym, ks in data.items():
            scores[sym] = flab.composite(ks, weights, "rank", ortho=ortho)
            date_index[sym] = {k["date"][:10]: i for i, k in enumerate(ks)}
        all_dates = sorted(set(d for m in date_index.values() for d in m.keys()))
        if not all_dates:
            return out
        topk = self.integer("top_k", 3)
        reb = max(1, self.integer("rebalance_days", 10))
        min_score = self.num("min_score", -999.0)
        # 硬约束参数（0 = 关闭）
        max_ind_pct = self.num("max_industry_pct", 0.0)
        max_adv_pct = self.num("max_adv_pct", 0.0)
        max_to_pct = self.num("max_turnover_pct", 0.0)
        capital = self.num("capital", 0.0)  # 未显式传入则用默认资金估流动性
        holding: dict = {}
        prev_wmap: dict = {}   # 上一次调仓的目标权重，算换手率用
        for di, date in enumerate(all_dates):
            # 预热期用数组下标；调仓日改用日历序号（理由见 day_no 的注释）
            if di < 130 or day_no(date) % reb != 0:
                continue
            ranked = []
            for sym in data:
                i = date_index[sym].get(date)
                if i is None:
                    continue
                sc = scores[sym][i]
                if sc is None:
                    continue
                ranked.append((sc, sym, i))
            ranked.sort(reverse=True)
            picked = {s for sc, s, _ in ranked[:topk] if sc >= min_score}
            for sym in list(holding):
                if sym not in picked:
                    i = date_index[sym].get(date)
                    if i is not None:
                        out[sym].append({"i": i, "action": "sell", "reason": "掉出因子前 %d 名" % topk})
                    holding.pop(sym, None)
            # ---- 权重分配：等权 or 组合优化 ----
            mode = self.text("weight_mode", "equal")
            if mode == "equal" or len(picked) <= 1:
                wmap = {s: 100.0 / max(len(picked), 1) for s in picked}
            else:
                rets = {}
                for sym in picked:
                    ks = data[sym]
                    c = [k["close"] for k in ks]
                    end = date_index[sym].get(date, len(c) - 1)
                    seg = c[max(0, end - 120):end + 1]
                    rets[sym] = [seg[j] / seg[j - 1] - 1.0 for j in range(1, len(seg)) if seg[j - 1]]
                scs = {sym: next(s for s, x, _ in ranked if x == sym) for sym in picked}
                # 机构级硬约束的数据准备：行业 / 流动性(ADV) / 换手
                ind_map = {s: ds.industry_of(s) or "其他" for s in picked}
                adv_map = {}
                if max_adv_pct > 0:
                    for sym in picked:
                        ks = data[sym]
                        end = date_index[sym].get(date, len(ks) - 1)
                        amts = []
                        for k in ks[max(0, end - 20):end + 1]:
                            a = k.get("amount")
                            if not a:
                                a = (k.get("close") or 0) * (k.get("volume") or 0) * 100.0
                            if a:
                                amts.append(float(a))
                        adv_map[sym] = sum(amts) / len(amts) if amts else 0.0
                try:
                    ores = opt.optimize(
                        mode, sorted(picked), rets, scs,
                        total_pct=self.num("total_pct", 90.0),
                        max_single_pct=self.num("max_single_pct", 20.0),
                        industry_map=ind_map if max_ind_pct > 0 else None,
                        max_industry_pct=max_ind_pct if max_ind_pct > 0 else None,
                        adv_map=adv_map if max_adv_pct > 0 else None,
                        max_adv_pct=max_adv_pct if max_adv_pct > 0 else None,
                        capital=capital if max_adv_pct > 0 else None,
                        current_weights=prev_wmap if max_to_pct > 0 else None,
                        max_turnover_pct=max_to_pct if max_to_pct > 0 else None)
                    wmap = ores["weights"] if ores.get("ok") else \
                        {s: 100.0 / max(len(picked), 1) for s in picked}
                except Exception:
                    wmap = {s: 100.0 / max(len(picked), 1) for s in picked}
            prev_wmap = dict(wmap)
            for sym in picked:
                if sym in holding:
                    continue
                i = date_index[sym].get(date)
                if i is None:
                    continue
                sc = next(s for s, x, _ in ranked if x == sym)
                out[sym].append({"i": i, "action": "buy",
                                 "reason": "因子得分 %.3f 排名前 %d，权重 %.1f%%"
                                           % (sc, topk, wmap.get(sym, 0.0)),
                                 "weight": wmap.get(sym, 100.0 / max(len(picked), 1))})
                holding[sym] = True
        return out


# ===========================================================================
# 14. 回调买入法（强势股回踩支撑，重启上涨时买入，买点贴近止损）
# ===========================================================================
@register
class PullbackBuy(Strategy):
    key = "pullback_buy"
    name = "回调买入法（强势股回踩支撑）"
    desc = ("找已经在上升趋势里的强势股 -> 等它回调到支撑位附近 -> 价格重新掉头向上时买入。"
            "止损就放在支撑下面一点点，所以【买点离止损很近】，单笔亏损小、盈亏比高。"
            "仓位用「单笔风险预算 ÷ 止损距离」自动算出来：止损越近，允许买的越多，"
            "这样每一笔亏掉的钱都是固定的。")
    defaults = {
        "trend_ma": 20, "long_ma": 60, "min_mom20": 3.0,
        "min_pullback": 1.5, "max_pullback": 15.0, "support_tol": 5.0,
        "use_volume_shrink": True, "confirm_days": 1,
        "stop_buffer": 2.0, "max_risk": 10.0,
        "risk_per_trade_pct": 1.0, "take_profit_r": 3.0, "exit_on_ma_break": True,
    }

    def schema(self):
        return [
            {"name": "trend_ma", "label": "趋势均线(天)", "type": "int", "default": 20, "min": 5, "max": 120,
             "help": "价格要站在这条均线上方才算强势"},
            {"name": "long_ma", "label": "长期均线(天)", "type": "int", "default": 60, "min": 10, "max": 250},
            {"name": "min_mom20", "label": "20日最小涨幅(%)", "type": "float", "default": 3.0, "min": 0, "max": 50,
             "help": "20 天涨幅要达到这个数才算强势股"},
            {"name": "min_pullback", "label": "最小回调幅度(%)", "type": "float", "default": 1.5, "min": 0, "max": 30,
             "help": "从近期高点至少要回撤这么多，否则不算回调"},
            {"name": "max_pullback", "label": "最大回调幅度(%)", "type": "float", "default": 15.0, "min": 2, "max": 60,
             "help": "回撤超过这个数就当成破位，不买了"},
            {"name": "support_tol", "label": "离支撑多远算贴近(%)", "type": "float", "default": 5.0, "min": 0.5, "max": 15},
            {"name": "use_volume_shrink", "label": "要求回调缩量", "type": "bool", "default": True,
             "help": "缩量回调说明没人急着跑，更健康"},
            {"name": "confirm_days", "label": "回升确认(天)", "type": "int", "default": 1, "min": 1, "max": 5,
             "help": "收盘价要突破前几天的最高价才算重新向上"},
            {"name": "stop_buffer", "label": "止损缓冲(%)", "type": "float", "default": 2.0, "min": 0.3, "max": 10,
             "help": "止损放在支撑位下方多少"},
            {"name": "max_risk", "label": "单笔最大风险(%)", "type": "float", "default": 10.0, "min": 1, "max": 25,
             "help": "买点到止损的距离超过这个数就不买"},
            {"name": "risk_per_trade_pct", "label": "单笔风险预算(%总资产)", "type": "float",
             "default": 1.0, "min": 0.1, "max": 10,
             "help": "每笔交易最多亏总资产的百分之多少。仓位 = 这个数 ÷ 止损距离"},
            {"name": "take_profit_r", "label": "止盈(倍盈亏比)", "type": "float", "default": 3.0, "min": 0, "max": 10,
             "help": "0=不启用；填 3 表示赚到 3 倍止损距离就卖。"
                     "回调买入法靠的就是「亏小赚大」，所以默认设 3 倍"},
            {"name": "exit_on_ma_break", "label": "跌破趋势均线就卖", "type": "bool", "default": True},
        ]

    def signals(self, symbol, klines):
        st = fac.pullback_state(klines, {
            "trend_ma": self.integer("trend_ma", 20), "long_ma": self.integer("long_ma", 60),
            "min_mom20": self.num("min_mom20", 3.0), "min_pullback": self.num("min_pullback", 2.0),
            "max_pullback": self.num("max_pullback", 15.0), "support_tol": self.num("support_tol", 4.0),
            "shrink_ratio": 0.85, "stop_buffer": self.num("stop_buffer", 2.0),
            "max_risk": self.num("max_risk", 8.0),
            "use_volume_shrink": self.flag("use_volume_shrink", True),
            "confirm_days": self.integer("confirm_days", 1),
        })
        c = _closes(klines)
        ma = ind.sma(c, self.integer("trend_ma", 20))
        need_shrink = self.flag("use_volume_shrink", True)
        max_risk = self.num("max_risk", 8.0)
        risk_budget = self.num("risk_per_trade_pct", 1.0)
        tpr = self.num("take_profit_r", 0.0)
        exit_ma = self.flag("exit_on_ma_break", True)

        out = []
        holding = False
        entry = stop = 0.0
        for i in range(1, len(klines)):
            x = st[i]
            if x is None:
                continue
            if holding:
                if c[i] <= stop:
                    out.append({"i": i, "action": "sell",
                                "reason": "跌破止损 %.2f（亏 %.2f%%）" % (stop, (c[i] / entry - 1) * 100)})
                    holding = False
                    continue
                if tpr > 0 and entry > stop and c[i] >= entry + tpr * (entry - stop):
                    out.append({"i": i, "action": "sell",
                                "reason": "达到 %.1f 倍盈亏比止盈（赚 %.2f%%）" % (tpr, (c[i] / entry - 1) * 100)})
                    holding = False
                    continue
                if exit_ma and ma[i] is not None and c[i] < ma[i]:
                    out.append({"i": i, "action": "sell",
                                "reason": "跌破 %d 日趋势均线，趋势结束" % self.integer("trend_ma", 20)})
                    holding = False
                    continue
                continue
            # 空仓：找买点
            if not x["strong"]:
                continue
            if not (self.num("min_pullback", 2.0) <= x["pullback_pct"] <= self.num("max_pullback", 15.0)):
                continue
            if not x["near_support"]:
                continue
            if not x["trigger"]:
                continue
            if need_shrink and not x["shrink"]:
                continue
            if not (0 < x["risk_pct"] <= max_risk):
                continue
            # 仓位 = 单笔风险预算 / 止损距离（止损越近买得越多）
            w = min(max(risk_budget / max(x["risk_pct"], 0.2) * 100.0, 1.0), 100.0)
            out.append({"i": i, "action": "buy", "weight": round(w, 2),
                        "reason": ("回踩支撑 %.2f 后重启上涨｜止损 %.2f（风险仅 %.2f%%）｜建议仓位 %.1f%%"
                                   % (x["support"], x["stop"], x["risk_pct"], w)),
                        "stop": x["stop"], "support": x["support"], "risk_pct": x["risk_pct"]})
            holding = True
            entry = c[i]
            stop = x["stop"]
        return out
