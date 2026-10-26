# -*- coding: utf-8 -*-
"""因子库

一个"因子"就是给每根 K 线算出一个分数。分数高 = 这个模型认为后面会涨。

本文件里的因子分几大类：
    动量/反转     价格涨得快不快、是不是短期涨过头了
    波动率        波动大不大（A 股长期看，低波动反而跑得好）
    成交量/流动性  换手、量比、资金流向、非流动性
    技术指标      RSI / MACD / KDJ / 布林 / 乖离 / DMI
    形态          K 线实体、上下影线、跳空
    Alpha101     WorldQuant 那 101 个公开公式里能用在单只股票上的部分，
                 原始公式里的横截面 rank 换成了时序 ts_rank

重要说明：
    * 每个因子都带一个 direction（先验方向）：+1 表示"值越大越看多"，
      -1 表示"值越小越看多"。这只是先验，真正好不好用得用因子实验室里的
      IC（和信息系数的关系）实测。
    * 没有一个因子能永远赚钱。任何声称"稳定赚钱的因子"都是在骗人。
      本平台给你的是"自己去测"的工具。

所有函数输入 klines（按时间升序的列表，每项含 open/high/low/close/volume/amount），
输出等长列表，数据不够的位置是 None。
"""
from __future__ import annotations

import math


# ===========================================================================
# 基础工具
# ===========================================================================
def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def arr(klines, key):
    return [_f(k.get(key)) for k in klines]


def safe_div(a, b, default=0.0, eps=1e-12):
    """安全除法：分母接近 0 时返回 default，避免除零崩溃"""
    return a / b if abs(b) > eps else default


def _ok(seg):
    return all(v is not None for v in seg)


# ---- 时序算子 -------------------------------------------------------------
def delay(x, n):
    out = [None] * len(x)
    for i in range(n, len(x)):
        out[i] = x[i - n]
    return out


def delta(x, n):
    out = [None] * len(x)
    for i in range(n, len(x)):
        if x[i] is not None and x[i - n] is not None:
            out[i] = x[i] - x[i - n]
    return out


def tsum(x, n):
    out = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < n:
            continue
        seg = x[i - n + 1:i + 1]
        if _ok(seg):
            out[i] = sum(seg)
    return out


def sma(x, n):
    out = [None] * len(x)
    s = 0.0
    bad = 0
    for i, v in enumerate(x):
        if v is None:
            bad += 1
        else:
            s += v
        if i >= n:
            old = x[i - n]
            if old is None:
                bad -= 1
            else:
                s -= old
        if i >= n - 1 and bad == 0:
            out[i] = s / n
    return out


def std(x, n):
    out = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < n:
            continue
        seg = x[i - n + 1:i + 1]
        if not _ok(seg):
            continue
        m = sum(seg) / n
        out[i] = math.sqrt(sum((v - m) ** 2 for v in seg) / n)
    return out


def ts_min(x, n):
    out = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < n:
            continue
        seg = x[i - n + 1:i + 1]
        if _ok(seg):
            out[i] = min(seg)
    return out


def ts_max(x, n):
    out = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < n:
            continue
        seg = x[i - n + 1:i + 1]
        if _ok(seg):
            out[i] = max(seg)
    return out


def ts_argmax(x, n):
    """窗口内最大值是第几根（0 = 最近一根）"""
    out = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < n:
            continue
        seg = x[i - n + 1:i + 1]
        if _ok(seg):
            out[i] = float(n - 1 - max(range(n), key=lambda j: seg[j]))
    return out


def ts_argmin(x, n):
    out = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < n:
            continue
        seg = x[i - n + 1:i + 1]
        if _ok(seg):
            out[i] = float(n - 1 - min(range(n), key=lambda j: seg[j]))
    return out


def ts_rank(x, n):
    """当前值在最近 n 期里的百分位（0~1）"""
    out = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < n:
            continue
        seg = x[i - n + 1:i + 1]
        if not _ok(seg) or seg[-1] is None:
            continue
        v = seg[-1]
        less = sum(1 for s in seg if s < v)
        eq = sum(1 for s in seg if s == v)
        out[i] = (less + 0.5 * eq) / n
    return out


def decay_linear(x, n):
    """线性加权平均，越近的权重越大（权重 n, n-1, ..., 1）"""
    out = [None] * len(x)
    wsum = n * (n + 1) / 2.0
    for i in range(len(x)):
        if i + 1 < n:
            continue
        seg = x[i - n + 1:i + 1]
        if not _ok(seg):
            continue
        out[i] = sum(seg[j] * (j + 1) for j in range(n)) / wsum
    return out


def corr(x, y, n):
    out = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < n:
            continue
        a = x[i - n + 1:i + 1]
        b = y[i - n + 1:i + 1]
        if not _ok(a) or not _ok(b):
            continue
        ma = sum(a) / n
        mb = sum(b) / n
        cov = sum((a[j] - ma) * (b[j] - mb) for j in range(n))
        va = math.sqrt(sum((v - ma) ** 2 for v in a))
        vb = math.sqrt(sum((v - mb) ** 2 for v in b))
        out[i] = cov / (va * vb) if va > 1e-12 and vb > 1e-12 else 0.0
    return out


def covariance(x, y, n):
    out = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < n:
            continue
        a = x[i - n + 1:i + 1]
        b = y[i - n + 1:i + 1]
        if not _ok(a) or not _ok(b):
            continue
        ma = sum(a) / n
        mb = sum(b) / n
        out[i] = sum((a[j] - ma) * (b[j] - mb) for j in range(n)) / n
    return out


def returns(close):
    out = [None] * len(close)
    for i in range(1, len(close)):
        if close[i - 1]:
            out[i] = close[i] / close[i - 1] - 1.0
    return out


def sign(x):
    out = [None] * len(x)
    for i, v in enumerate(x):
        if v is not None:
            out[i] = 1.0 if v > 0 else (-1.0 if v < 0 else 0.0)
    return out


def where(cond_vals, a_vals, b_vals):
    out = [None] * len(cond_vals)
    for i in range(len(cond_vals)):
        out[i] = a_vals[i] if cond_vals[i] else b_vals[i]
    return out


def neg(x):
    return [None if v is None else -v for v in x]


def mul(a, b):
    return [None if (a[i] is None or b[i] is None) else a[i] * b[i] for i in range(len(a))]


def sub(a, b):
    return [None if (a[i] is None or b[i] is None) else a[i] - b[i] for i in range(len(a))]


def add(*xs):
    n = len(xs[0])
    out = []
    for i in range(n):
        if any(x[i] is None for x in xs):
            out.append(None)
        else:
            out.append(sum(x[i] for x in xs))
    return out


def div(a, b):
    out = []
    for i in range(len(a)):
        if a[i] is None or b[i] is None:
            out.append(None)
        else:
            out.append(safe_div(a[i], b[i]))
    return out


def vwap_series(klines):
    """成交均价。A股 volume 单位是手(100股)，amount 单位是元"""
    out = []
    for k in klines:
        v = _f(k.get("volume"))
        a = _f(k.get("amount"))
        if v > 0 and a > 0:
            out.append(a / (v * 100.0))
        else:
            out.append((_f(k.get("high")) + _f(k.get("low")) + _f(k.get("close"))) / 3.0)
    return out


# ===========================================================================
# 因子注册表
# ===========================================================================
FACTORS: dict = {}
FAMILIES: list = []


def register(key, name, family, desc, direction=1, params=None):
    def deco(fn):
        FACTORS[key] = {
            "key": key, "name": name, "family": family, "desc": desc,
            "direction": direction, "params": params or [], "fn": fn,
        }
        if family not in FAMILIES:
            FAMILIES.append(family)
        return fn
    return deco


def compute(key, klines, params=None) -> list:
    meta = FACTORS.get(key)
    if meta is None:
        raise KeyError("没有这个因子: %s" % key)
    return meta["fn"](klines, **(params or {}))


def list_factors() -> list:
    order = {f: i for i, f in enumerate(FAMILIES)}
    out = []
    for m in FACTORS.values():
        out.append({"key": m["key"], "name": m["name"], "family": m["family"],
                    "desc": m["desc"], "direction": m["direction"], "params": m["params"]})
    out.sort(key=lambda x: (order.get(x["family"], 99), x["key"]))
    return out


# ===========================================================================
# 1. 动量 / 反转
# ===========================================================================
@register("mom_5", "5日动量", "动量/反转", "最近 5 个交易日的涨幅。短期动量在 A 股往往是反向的，用实验室实测才知道。")
def f_mom_5(kl):
    c = arr(kl, "close")
    return [None if x is None else safe_div(x, c[i - 5], 1) * 100 for i, x in enumerate(delta(c, 5))]


@register("mom_10", "10日动量", "动量/反转", "最近 10 个交易日涨幅。")
def f_mom_10(kl):
    c = arr(kl, "close")
    return [None if x is None else safe_div(x, c[i - 10], 1) * 100 for i, x in enumerate(delta(c, 10))]


@register("mom_20", "20日动量", "动量/反转", "最近 20 个交易日（约1个月）涨幅。中期动量在 A 股常被用作正向因子。")
def f_mom_20(kl):
    c = arr(kl, "close")
    return [None if x is None else safe_div(x, c[i - 20], 1) * 100 for i, x in enumerate(delta(c, 20))]


@register("mom_60", "60日动量", "动量/反转", "最近 60 个交易日（约3个月）涨幅。")
def f_mom_60(kl):
    c = arr(kl, "close")
    return [None if x is None else safe_div(x, c[i - 60], 1) * 100 for i, x in enumerate(delta(c, 60))]


@register("mom_120", "120日动量", "动量/反转", "最近 120 个交易日（约半年）涨幅。")
def f_mom_120(kl):
    c = arr(kl, "close")
    return [None if x is None else safe_div(x, c[i - 120], 1) * 100 for i, x in enumerate(delta(c, 120))]


@register("rev_1", "1日反转", "动量/反转", "前一天涨跌幅取负。短期反转是 A 股最稳的因子之一：昨天大跌今天容易反弹。", direction=1)
def f_rev_1(kl):
    c = arr(kl, "close")
    r = returns(c)
    return neg(r)


@register("rev_5", "5日反转", "动量/反转", "5 日涨幅取负，抄超跌。")
def f_rev_5(kl):
    return neg(f_mom_5(kl))


@register("rev_10", "10日反转", "动量/反转", "10 日涨幅取负。")
def f_rev_10(kl):
    return neg(f_mom_10(kl))


@register("mom_skip_20", "20日动量(剔除最近5日)", "动量/反转",
          "用 20 日前的价格到 5 日前的价格算动量，避开最近 5 日的短期反转干扰。学术上这是最干净的中期动量。")
def f_mom_skip(kl):
    c = arr(kl, "close")
    out = [None] * len(c)
    for i in range(25, len(c)):
        if c[i - 5] and c[i - 25]:
            out[i] = (c[i - 5] / c[i - 25] - 1.0) * 100
    return out


@register("mom_sharpe_20", "20日风险调整动量", "动量/反转", "20日涨幅除以20日波动率，衡量涨得稳不稳。")
def f_mom_sharpe(kl):
    c = arr(kl, "close")
    r = returns(c)
    sd = std([0.0 if v is None else v for v in r], 20)
    m = f_mom_20(kl)
    return [None if (m[i] is None or sd[i] is None) else safe_div(m[i], sd[i] * 100, 1) for i in range(len(c))]


@register("trend_r2_20", "趋势强度R²(20日)", "动量/反转", "收盘价对时间做回归的 R²，越高说明趋势越干净。")
def f_trend_r2(kl):
    c = arr(kl, "close")
    n, out = 20, [None] * len(c)
    xs = list(range(n))
    mx = sum(xs) / n
    vx = sum((x - mx) ** 2 for x in xs)
    for i in range(n - 1, len(c)):
        seg = c[i - n + 1:i + 1]
        my = sum(seg) / n
        cov = sum((xs[j] - mx) * (seg[j] - my) for j in range(n))
        vy = sum((v - my) ** 2 for v in seg)
        out[i] = (cov * cov) / (vx * vy) if vy > 1e-12 else 0.0
    return out


@register("price_ma_20", "价格/20日均线-1", "动量/反转", "收盘价相对 20 日均线的偏离，衡量短期强弱。")
def f_price_ma20(kl):
    c = arr(kl, "close")
    ma = sma(c, 20)
    return [None if ma[i] is None else (c[i] / ma[i] - 1.0) * 100 for i in range(len(c))]


@register("price_ma_60", "价格/60日均线-1", "动量/反转", "收盘价相对 60 日均线的偏离。")
def f_price_ma60(kl):
    c = arr(kl, "close")
    ma = sma(c, 60)
    return [None if ma[i] is None else (c[i] / ma[i] - 1.0) * 100 for i in range(len(c))]


@register("ma_cross_5_20", "均线差(5-20)", "动量/反转", "5日均线除以20日均线减1，大于0说明短期均线在上方。")
def f_ma_cross(kl):
    c = arr(kl, "close")
    a, b = sma(c, 5), sma(c, 20)
    return [None if (a[i] is None or b[i] is None) else (a[i] / b[i] - 1.0) * 100 for i in range(len(c))]


@register("ma_slope_20", "20日均线斜率", "动量/反转", "20日均线 5 日变化率，中期趋势方向。")
def f_ma_slope(kl):
    c = arr(kl, "close")
    ma = sma(c, 20)
    base = [0.0 if v is None else v for v in ma]
    out = [None] * len(c)
    for i, x in enumerate(delta(base, 5)):
        if x is not None and i >= 25 and ma[i - 5]:
            out[i] = safe_div(x, ma[i - 5], 0.0) * 100
    return out


# ===========================================================================
# 2. 波动率
# ===========================================================================
@register("vol_20_neg", "低波动(20日)", "波动率", "20日收益率标准差取负。低波动异象：波动小的股票长期收益更好。", direction=1)
def f_vol_neg(kl):
    r = returns(arr(kl, "close"))
    sd = std([0.0 if v is None else v for v in r], 20)
    return neg(sd)


@register("vol_60_neg", "低波动(60日)", "波动率", "60日波动率取负。")
def f_vol60_neg(kl):
    r = returns(arr(kl, "close"))
    sd = std([0.0 if v is None else v for v in r], 60)
    return neg(sd)


@register("atr_pct_neg", "低ATR波动(14日)", "波动率", "ATR/收盘价取负，波动越小分越高。")
def f_atr_neg(kl):
    h, l, c = arr(kl, "high"), arr(kl, "low"), arr(kl, "close")
    tr = [None] * len(c)
    for i in range(len(c)):
        if i == 0:
            tr[i] = h[i] - l[i]
        else:
            tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    a = sma([0.0 if v is None else v for v in tr], 14)
    return [None if (a[i] is None or not c[i]) else -(a[i] / c[i]) for i in range(len(c))]


@register("range_20_neg", "低振幅(20日)", "波动率", "20日平均振幅(最高-最低)/收盘 取负。")
def f_range_neg(kl):
    h, l, c = arr(kl, "high"), arr(kl, "low"), arr(kl, "close")
    rng = [safe_div(h[i] - l[i], c[i]) for i in range(len(c))]
    a = sma(rng, 20)
    return neg(a)


@register("downside_vol_20_neg", "低下行波动(20日)", "波动率", "只统计下跌日的波动率，取负。")
def f_downside_neg(kl):
    r = returns(arr(kl, "close"))
    out = [None] * len(r)
    for i in range(19, len(r)):
        seg = [r[j] for j in range(i - 19, i + 1) if r[j] is not None and r[j] < 0]
        out[i] = -(sum(v * v for v in seg) / len(seg)) ** 0.5 if seg else 0.0
    return out


@register("parkinson_20_neg", "Parkinson波动(20日)", "波动率", "用最高最低价算的波动率，取负。比收盘价波动率更灵敏。")
def f_parkinson_neg(kl):
    h, l = arr(kl, "high"), arr(kl, "low")
    hl = [safe_div(math.log(h[i] / l[i]) ** 2, 4 * math.log(2)) if h[i] > 0 and l[i] > 0 else 0.0
          for i in range(len(h))]
    a = sma(hl, 20)
    return [None if v is None else -math.sqrt(v) for v in a]


@register("vol_of_vol_20_neg", "波动率的波动(20日)", "波动率", "波动率本身的不稳定性，取负。")
def f_vov_neg(kl):
    r = returns(arr(kl, "close"))
    sd = std([0.0 if v is None else v for v in r], 10)
    sdsd = std([0.0 if v is None else v for v in sd], 20)
    return neg(sdsd)


@register("max_ret_20_neg", "MAX因子(20日最大单日涨幅)", "波动率",
          "20日内最大单日涨幅取负。A股的「彩票效应」：爱暴涨的股票后面往往跌得多。", direction=1)
def f_max_ret_neg(kl):
    r = returns(arr(kl, "close"))
    return neg(ts_max([0.0 if v is None else v for v in r], 20))


@register("skew_20_neg", "偏度(20日)", "波动率", "收益分布偏度取负。")
def f_skew_neg(kl):
    r = returns(arr(kl, "close"))
    r2 = [0.0 if v is None else v for v in r]
    out = [None] * len(r2)
    for i in range(19, len(r2)):
        seg = r2[i - 19:i + 1]
        m = sum(seg) / 20
        sd = (sum((v - m) ** 2 for v in seg) / 20) ** 0.5
        out[i] = -(sum(((v - m) / sd) ** 3 for v in seg) / 20) if sd > 1e-12 else 0.0
    return out


# ===========================================================================
# 3. 成交量 / 流动性
# ===========================================================================
@register("turnover_20_neg", "低换手(20日)", "成交量/流动性",
          "20日平均换手率取负。低换手在A股是非常经典的选股因子。", direction=1)
def f_turnover_neg(kl):
    t = arr(kl, "turnover")
    if not any(t):
        c, v = arr(kl, "close"), arr(kl, "volume")
        t = [safe_div(v[i], c[i]) for i in range(len(c))]
    return neg(sma(t, 20))


@register("turnover_5_neg", "低换手(5日)", "成交量/流动性", "5日平均换手率取负。")
def f_turnover5_neg(kl):
    t = arr(kl, "turnover")
    if not any(t):
        c, v = arr(kl, "close"), arr(kl, "volume")
        t = [safe_div(v[i], c[i]) for i in range(len(c))]
    return neg(sma(t, 5))


@register("volume_ratio_5_20", "量比(5/20)", "成交量/流动性",
          "5日均量 / 20日均量。放量往往意味着有资金在动，但也可能是出货。")
def f_volume_ratio(kl):
    v = arr(kl, "volume")
    a, b = sma(v, 5), sma(v, 20)
    return [None if (a[i] is None or b[i] is None) else safe_div(a[i], b[i], 1) for i in range(len(v))]


@register("amount_log_20", "成交额规模(20日,取log)", "成交量/流动性",
          "20日平均成交额取对数。适度放大的成交额代表流动性好、关注度高。")
def f_amount_log(kl):
    a = arr(kl, "amount")
    m = sma(a, 20)
    return [None if v is None or v <= 0 else math.log(v) for v in m]


@register("amihud_illiq_20", "非流动性(Amihud,20日)", "成交量/流动性",
          "平均(|日收益| / 成交额) × 1e9。非流动性溢价：越难交易的股票要求越高回报。")
def f_amihud(kl):
    c, a = arr(kl, "close"), arr(kl, "amount")
    r = returns(c)
    out = [None] * len(c)
    for i in range(19, len(c)):
        vals = [abs(r[j]) / a[j] * 1e9 for j in range(i - 19, i + 1)
                if r[j] is not None and a[j] > 0]
        out[i] = sum(vals) / len(vals) if vals else None
    return out


@register("obv_slope_20", "OBV能量潮斜率", "成交量/流动性", "OBV（涨日加量、跌日减量）的 20 日变化趋势。")
def f_obv(kl):
    c, v = arr(kl, "close"), arr(kl, "volume")
    obv = [0.0]
    for i in range(1, len(c)):
        obv.append(obv[-1] + (v[i] if c[i] > c[i - 1] else (-v[i] if c[i] < c[i - 1] else 0.0)))
    out = [None] * len(c)
    for i in range(20, len(c)):
        base = abs(obv[i - 20]) + 1e-9
        out[i] = (obv[i] - obv[i - 20]) / base
    return out


@register("mfi_14", "资金流量指标MFI(14)", "成交量/流动性", "带成交量的 RSI。低于20超卖，高于80超买。")
def f_mfi(kl):
    h, l, c, v = arr(kl, "high"), arr(kl, "low"), arr(kl, "close"), arr(kl, "volume")
    tp = [(h[i] + l[i] + c[i]) / 3 for i in range(len(c))]
    out = [None] * len(c)
    for i in range(14, len(c)):
        pos = neg_ = 0.0
        for j in range(i - 13, i + 1):
            flow = tp[j] * v[j]
            if tp[j] > tp[j - 1]:
                pos += flow
            elif tp[j] < tp[j - 1]:
                neg_ += flow
        out[i] = 100.0 if neg_ == 0 else 100 - 100 / (1 + pos / neg_)
    return out


@register("vwap_dev_20", "价格偏离VWAP(20日)", "成交量/流动性", "收盘价 / 20日成交均价 - 1。低于均价说明相对便宜。")
def f_vwap_dev(kl):
    c = arr(kl, "close")
    vw = vwap_series(kl)
    m = sma(vw, 20)
    return [None if m[i] is None else (c[i] / m[i] - 1.0) * 100 for i in range(len(c))]


@register("pv_corr_20", "量价相关(20日)", "成交量/流动性",
          "收盘价与成交量的 20 日相关系数取负。价涨量增往往是见顶信号。")
def f_pv_corr(kl):
    return neg(corr(arr(kl, "close"), arr(kl, "volume"), 20))


@register("volume_trend_20", "成交量趋势(20日)", "成交量/流动性", "20日均量相对 60日均量的变化，成交量在放大还是萎缩。")
def f_volume_trend(kl):
    v = arr(kl, "volume")
    a, b = sma(v, 20), sma(v, 60)
    return [None if (a[i] is None or b[i] is None) else safe_div(a[i] - b[i], b[i], 1) * 100 for i in range(len(v))]


@register("vstd_20_neg", "成交量稳定性(20日)", "成交量/流动性", "成交量标准差/均值取负，成交量越稳定越好。")
def f_vstd_neg(kl):
    v = arr(kl, "volume")
    s, m = std(v, 20), sma(v, 20)
    return [None if (s[i] is None or m[i] is None) else -safe_div(s[i], m[i], 1) for i in range(len(v))]


# ===========================================================================
# 4. 技术指标
# ===========================================================================
def _rsi(c, n):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g += max(d, 0.0)
        l += max(-d, 0.0)
    ag, al = g / n, l / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


@register("rsi_14_neg", "RSI超卖(14)", "技术指标", "14日 RSI 取负。RSI 低说明超卖。", direction=1)
def f_rsi_neg(kl):
    return neg(_rsi(arr(kl, "close"), 14))


@register("rsi_6_neg", "RSI超卖(6)", "技术指标", "6日 RSI 取负，更敏感。")
def f_rsi6_neg(kl):
    return neg(_rsi(arr(kl, "close"), 6))


@register("macd_hist", "MACD柱", "技术指标", "(DIF-DEA)×2。大于0代表多头动能占优。")
def f_macd_hist(kl):
    c = arr(kl, "close")
    def ema(x, n):
        out = [None] * len(x)
        if len(x) < n:
            return out
        k = 2.0 / (n + 1)
        prev = sum(x[:n]) / n
        out[n - 1] = prev
        for i in range(n, len(x)):
            prev = x[i] * k + prev * (1 - k)
            out[i] = prev
        return out
    ef, es = ema(c, 12), ema(c, 26)
    dif = [None if (ef[i] is None or es[i] is None) else ef[i] - es[i] for i in range(len(c))]
    st = next((i for i, v in enumerate(dif) if v is not None), None)
    dea = [None] * len(c)
    if st is not None:
        e = ema(dif[st:], 9)
        for j, v in enumerate(e):
            dea[st + j] = v
    return [None if (dif[i] is None or dea[i] is None) else 2 * (dif[i] - dea[i]) for i in range(len(c))]


@register("kdj_j_neg", "KDJ的J值(取负)", "技术指标", "J 值取负，越低越超卖。")
def f_kdj_neg(kl):
    h, l, c = arr(kl, "high"), arr(kl, "low"), arr(kl, "close")
    out = [None] * len(c)
    k = d = 50.0
    for i in range(8, len(c)):
        hh = max(h[i - 8:i + 1])
        ll = min(l[i - 8:i + 1])
        rsv = 50.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
        out[i] = -(3 * k - 2 * d)
    return out


@register("boll_pos_20", "布林位置(20日)", "技术指标", "(收盘-下轨)/(上轨-下轨)。0=贴下轨，1=贴上轨。")
def f_boll_pos(kl):
    c = arr(kl, "close")
    m, s = sma(c, 20), std(c, 20)
    out = [None] * len(c)
    for i in range(len(c)):
        if m[i] is None or s[i] is None or s[i] < 1e-9:
            continue
        out[i] = (c[i] - (m[i] - 2 * s[i])) / (4 * s[i])
    return out


@register("boll_width_20_neg", "布林带宽(20日,取负)", "技术指标", "带宽越小说明越盘整，后面容易突破。")
def f_boll_width_neg(kl):
    c = arr(kl, "close")
    m, s = sma(c, 20), std(c, 20)
    return [None if (m[i] is None or s[i] is None or not m[i]) else -(4 * s[i] / m[i]) for i in range(len(c))]


@register("cci_14_neg", "CCI顺势指标(14,取负)", "技术指标", "CCI 取负，越低越超卖。")
def f_cci_neg(kl):
    h, l, c = arr(kl, "high"), arr(kl, "low"), arr(kl, "close")
    tp = [(h[i] + l[i] + c[i]) / 3 for i in range(len(c))]
    ma = sma(tp, 14)
    out = [None] * len(c)
    for i in range(13, len(c)):
        seg = tp[i - 13:i + 1]
        md = sum(abs(v - ma[i]) for v in seg) / 14
        out[i] = -safe_div(tp[i] - ma[i], 0.015 * md, 1) if md > 1e-12 else 0.0
    return out


@register("wr_14", "威廉超卖(14)", "技术指标",
          "威廉指标的取反形式：0~100，数值越大说明收盘价越靠近区间低点（越超卖）。")
def f_wr_neg(kl):
    h, l, c = arr(kl, "high"), arr(kl, "low"), arr(kl, "close")
    out = [None] * len(c)
    for i in range(13, len(c)):
        hh, ll = max(h[i - 13:i + 1]), min(l[i - 13:i + 1])
        out[i] = 100.0 * safe_div(hh - c[i], hh - ll, 0.5) if hh != ll else 50.0
    return out


@register("bias_6_neg", "乖离率(6日,取负)", "技术指标", "(收盘-6日均线)/6日均线 取负。跌太多容易反弹。")
def f_bias_neg(kl):
    c = arr(kl, "close")
    m = sma(c, 6)
    return [None if m[i] is None else -((c[i] - m[i]) / m[i] * 100) for i in range(len(c))]


@register("psy_12_neg", "心理线(12日,取负)", "技术指标", "12日内上涨天数占比取负。人气太低时买入。")
def f_psy_neg(kl):
    c = arr(kl, "close")
    out = [None] * len(c)
    for i in range(12, len(c)):
        up = sum(1 for j in range(i - 11, i + 1) if c[j] > c[j - 1])
        out[i] = -(up / 12.0 * 100)
    return out


@register("adx_14", "ADX趋势强度(14)", "技术指标", "ADX 越高代表趋势越强（不分方向）。")
def f_adx(kl):
    h, l, c = arr(kl, "high"), arr(kl, "low"), arr(kl, "close")
    n = len(c)
    tr, pdm, ndm = [0.0] * n, [0.0] * n, [0.0] * n
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0
    out = [None] * n
    for i in range(14, n):
        atr = sum(tr[i - 13:i + 1]) / 14
        pdi = 100 * safe_div(sum(pdm[i - 13:i + 1]) / 14, atr, 1)
        ndi = 100 * safe_div(sum(ndm[i - 13:i + 1]) / 14, atr, 1)
        out[i] = 100 * safe_div(abs(pdi - ndi), pdi + ndi, 1)
    return out


@register("ar_26", "AR人气指标(26)", "技术指标", "(26日最高价之和)/(26日最低价之和)，衡量多空力量。")
def f_ar(kl):
    h, l = arr(kl, "high"), arr(kl, "low")
    out = [None] * len(h)
    for i in range(25, len(h)):
        sh = sum(h[i - 25:i + 1])
        sl = sum(l[i - 25:i + 1])
        out[i] = 100 * safe_div(sh, sl, 1)
    return out


@register("cr_26", "CR能量指标(26)", "技术指标", "26日最高价/最低价相对前日中价的强度。")
def f_cr(kl):
    h, l = arr(kl, "high"), arr(kl, "low")
    n = len(h)
    mid = [(h[i] + l[i]) / 2 for i in range(n)]      # 当日中价
    out = [None] * n
    for i in range(26, n):
        up = dn = 0.0
        for j in range(i - 25, i + 1):
            if j < 1:
                continue
            prev = mid[j - 1]                          # 前一天的中价
            up += max(0.0, h[j] - prev)
            dn += max(0.0, prev - l[j])
        out[i] = 100 * safe_div(up, dn, 1)
    return out


# ===========================================================================
# 5. K线形态
# ===========================================================================
@register("kmid", "实体涨跌(KMID)", "形态", "(收盘-开盘)/开盘。当天实体的涨跌幅。")
def f_kmid(kl):
    o, c = arr(kl, "open"), arr(kl, "close")
    return [-safe_div(o[i] - c[i], o[i], 1) for i in range(len(c))]


@register("klen_neg", "振幅(KLEN,取负)", "形态", "(最高-最低)/开盘 取负。日内振幅越大越不稳。")
def f_klen_neg(kl):
    o, h, l = arr(kl, "open"), arr(kl, "high"), arr(kl, "low")
    return [-safe_div(h[i] - l[i], o[i], 1) for i in range(len(o))]


@register("kmid2", "实体占振幅比(KMID2)", "形态", "(收盘-开盘)/(最高-最低)。实体越饱满越有力量。")
def f_kmid2(kl):
    o, c, h, l = arr(kl, "open"), arr(kl, "close"), arr(kl, "high"), arr(kl, "low")
    return [safe_div(o[i] - c[i], h[i] - l[i], 1) for i in range(len(o))]


@register("kup_neg", "上影线(KUP,取负)", "形态", "(最高-max(开,收))/开盘 取负。上影线长说明抛压重。")
def f_kup_neg(kl):
    o, c, h = arr(kl, "open"), arr(kl, "close"), arr(kl, "high")
    return [-safe_div(h[i] - max(o[i], c[i]), o[i], 1) for i in range(len(o))]


@register("klow", "下影线(KLOW)", "形态", "(min(开,收)-最低)/开盘。下影线长说明有承接。")
def f_klow(kl):
    o, c, l = arr(kl, "open"), arr(kl, "close"), arr(kl, "low")
    return [safe_div(min(o[i], c[i]) - l[i], o[i], 1) for i in range(len(o))]


@register("ksft", "K线重心(KSFT)", "形态", "(2×收盘-最高-最低)/开盘。收盘在区间上部为正。")
def f_ksft(kl):
    o, c, h, l = arr(kl, "open"), arr(kl, "close"), arr(kl, "high"), arr(kl, "low")
    return [safe_div(2 * c[i] - h[i] - l[i], o[i], 1) for i in range(len(o))]


@register("gap_open_neg", "跳空缺口(取负)", "形态", "今日开盘/昨日收盘-1 取负。大幅高开容易回落。")
def f_gap_neg(kl):
    o, c = arr(kl, "open"), arr(kl, "close")
    out = [None] * len(o)
    for i in range(1, len(o)):
        out[i] = -safe_div(o[i] - c[i - 1], c[i - 1], 1) * 100
    return out


@register("body_ratio", "阳线实体占比", "形态", "实体/振幅 × 方向。大阳线为正且值大。")
def f_body_ratio(kl):
    o, c, h, l = arr(kl, "open"), arr(kl, "close"), arr(kl, "high"), arr(kl, "low")
    out = []
    for i in range(len(o)):
        rng = h[i] - l[i]
        out.append(safe_div(c[i] - o[i], rng, 1))
    return out


# ===========================================================================
# 6. Alpha101 改编（WorldQuant 公开公式，横截面 rank 改为时序 ts_rank）
# ===========================================================================
A101_NOTE = "Alpha101 改编：原公式的横截面 rank 换成了时序 ts_rank（因为单只股票没有横截面）。"


@register("alpha001", "Alpha101-001", "Alpha101",
          "用 5 日内「收益为负时的波动率，或收盘价」的最大值位置排名。经典反转因子。" + A101_NOTE)
def f_a001(kl):
    c = arr(kl, "close")
    r = [0.0 if v is None else v for v in returns(c)]
    sd = std(r, 20)
    inner = [0.0] * len(c)
    for i in range(len(c)):
        if sd[i] is None:
            continue
        inner[i] = (c[i] ** 2) if r[i] >= 0 else (sd[i] ** 2)
    am = ts_argmax(inner, 5)
    return ts_rank([0.0 if v is None else v for v in am], 20)


@register("alpha003", "Alpha101-003", "Alpha101", "开盘价与成交量的时序排名相关性取负（价量背离）。" + A101_NOTE)
def f_a003(kl):
    return neg(corr(ts_rank(arr(kl, "open"), 10), ts_rank(arr(kl, "volume"), 10), 10))


@register("alpha004", "Alpha101-004", "Alpha101", "最低价的时序排名再排名取负。价格位置越低分越高。" + A101_NOTE)
def f_a004(kl):
    return neg(ts_rank(ts_rank(arr(kl, "low"), 10), 10))


@register("alpha006", "Alpha101-006", "Alpha101", "开盘价与成交量的相关性取负。量价同向时看空。" + A101_NOTE)
def f_a006(kl):
    return neg(corr(arr(kl, "open"), arr(kl, "volume"), 10))


@register("alpha012", "Alpha101-012", "Alpha101", "成交量变化方向 × 收盘价变化的反方向。放量下跌看多。" + A101_NOTE)
def f_a012(kl):
    return mul(sign(delta(arr(kl, "volume"), 1)), neg(delta(arr(kl, "close"), 1)))


@register("alpha013", "Alpha101-013", "Alpha101", "收盘价排名与成交量排名的协方差取负。" + A101_NOTE)
def f_a013(kl):
    return neg(ts_rank(covariance(ts_rank(arr(kl, "close"), 10), ts_rank(arr(kl, "volume"), 10), 5), 10))


@register("alpha016", "Alpha101-016", "Alpha101", "最高价排名与成交量排名的协方差取负。" + A101_NOTE)
def f_a016(kl):
    return neg(ts_rank(covariance(ts_rank(arr(kl, "high"), 10), ts_rank(arr(kl, "volume"), 10), 5), 10))


@register("alpha018", "Alpha101-018", "Alpha101",
          "「5日开收盘价差绝对值」的标准差 + 当日价差 + 开收盘相关性，再取排名负值。" + A101_NOTE)
def f_a018(kl):
    o, c = arr(kl, "open"), arr(kl, "close")
    absdiff = [abs(c[i] - o[i]) for i in range(len(c))]
    part = add(std(absdiff, 5), sub(c, o), corr(c, o, 10))
    return neg(ts_rank([0.0 if v is None else v for v in part], 10))


@register("alpha023", "Alpha101-023", "Alpha101", "若最高价高于20日均最高价，则看跌 2 日最高价变化。" + A101_NOTE)
def f_a023(kl):
    h = arr(kl, "high")
    m = sma(h, 20)
    d = delta(h, 2)
    return [None if (m[i] is None or d[i] is None) else (-d[i] if h[i] > m[i] else 0.0) for i in range(len(h))]


@register("alpha026", "Alpha101-026", "Alpha101", "成交量排名与最高价排名相关性的 3 日最大值取负。" + A101_NOTE)
def f_a026(kl):
    cc = corr(ts_rank(arr(kl, "volume"), 5), ts_rank(arr(kl, "high"), 5), 5)
    return neg(ts_max([0.0 if v is None else v for v in cc], 3))


@register("alpha033", "Alpha101-033", "Alpha101", "开盘价相对收盘价的折价，取负后再排名。" + A101_NOTE)
def f_a033(kl):
    o, c = arr(kl, "open"), arr(kl, "close")
    return ts_rank([1 - safe_div(o[i], c[i], 1) for i in range(len(o))], 10)


@register("alpha034", "Alpha101-034", "Alpha101", "2日波动率/5日波动率的排名取负。短期波动放大是坏事。" + A101_NOTE)
def f_a034(kl):
    c = arr(kl, "close")
    r = [0.0 if v is None else v for v in returns(c)]
    ratio = div(std(r, 2), std(r, 5))
    return neg(ts_rank([0.0 if v is None else v for v in ratio], 5))


@register("alpha041", "Alpha101-041", "Alpha101", "最高最低价的几何均值减去成交均价 VWAP。" + A101_NOTE)
def f_a041(kl):
    h, l = arr(kl, "high"), arr(kl, "low")
    vw = vwap_series(kl)
    return [math.sqrt(max(h[i] * l[i], 0.0)) - vw[i] for i in range(len(h))]


@register("alpha043", "Alpha101-043", "Alpha101", "成交量相对20日均量的排名 × 7日跌幅排名。" + A101_NOTE)
def f_a043(kl):
    v = arr(kl, "volume")
    rel = div(v, sma(v, 20))
    r1 = ts_rank([0.0 if x is None else x for x in rel], 20)
    r2 = ts_rank([0.0 if x is None else x for x in neg(delta(arr(kl, "close"), 7))], 8)
    return mul(r1, r2)


@register("alpha044", "Alpha101-044", "Alpha101", "最高价与成交量排名的相关性取负。" + A101_NOTE)
def f_a044(kl):
    return neg(corr(arr(kl, "high"), ts_rank(arr(kl, "volume"), 5), 5))


@register("alpha053", "Alpha101-053", "Alpha101", "收盘价在当日区间位置的 9 日变化取负。" + A101_NOTE)
def f_a053(kl):
    c, h, l = arr(kl, "close"), arr(kl, "high"), arr(kl, "low")
    pos = [safe_div((c[i] - l[i]) - (h[i] - c[i]), c[i] - l[i], 1) for i in range(len(c))]
    return neg(delta(pos, 9))


@register("alpha054", "Alpha101-054", "Alpha101", "做空收盘价与开盘价的相对位置（含5次方放大）。" + A101_NOTE)
def f_a054(kl):
    o, c, h, l = arr(kl, "open"), arr(kl, "close"), arr(kl, "high"), arr(kl, "low")
    out = []
    for i in range(len(c)):
        num = -(l[i] - c[i]) * (o[i] ** 5)
        den = (l[i] - h[i]) * (c[i] ** 5)
        out.append(safe_div(num, den, 1))
    return out


@register("alpha060", "Alpha101-060", "Alpha101", "收盘价在区间中的位置 × 成交量，取负。高位放量看空。" + A101_NOTE)
def f_a060(kl):
    c, h, l, v = arr(kl, "close"), arr(kl, "high"), arr(kl, "low"), arr(kl, "volume")
    return [-safe_div(2 * c[i] - l[i] - h[i], h[i] - l[i], 1) * v[i] for i in range(len(c))]


@register("alpha101", "Alpha101-101", "Alpha101", "(收盘-开盘)/(最高-最低)。最简单的日内强弱因子。" + A101_NOTE)
def f_a101(kl):
    o, c, h, l = arr(kl, "open"), arr(kl, "close"), arr(kl, "high"), arr(kl, "low")
    return [safe_div(c[i] - o[i], h[i] - l[i] + 0.001, 1) for i in range(len(c))]


# ===========================================================================
# 7. 组合预设
# ===========================================================================
# 这些是"上面那些因子按权重拼起来"的配方。权重是按 A 股常见经验给的先验，
# 不代表一定赚钱，请到因子实验室里自己验证。
PRESETS = [
    {
        "key": "preset_reversal",
        "name": "短期反转组合",
        "desc": "A股最经典的短线思路：跌得多的容易反弹。由 1日/5日反转 + 低换手 + 低波动 组成。",
        "factors": {"rev_1": 0.30, "rev_5": 0.25, "turnover_20_neg": 0.25, "vol_20_neg": 0.20},
    },
    {
        "key": "preset_momentum",
        "name": "中期动量组合",
        "desc": "跟趋势：中期涨得好、趋势干净、成交活跃的继续拿。",
        "factors": {"mom_skip_20": 0.35, "trend_r2_20": 0.25, "mom_sharpe_20": 0.20, "amount_log_20": 0.20},
    },
    {
        "key": "preset_lowvol",
        "name": "低波动稳健组合",
        "desc": "低波动异象：波动小、换手低、下行风险小的股票，长期夏普更高。",
        "factors": {"vol_20_neg": 0.30, "vol_60_neg": 0.25, "turnover_20_neg": 0.25, "max_ret_20_neg": 0.20},
    },
    {
        "key": "preset_reversal_tech",
        "name": "超卖反弹组合",
        "desc": "技术指标超卖 + 短期反转，做短线反弹。",
        "factors": {"rsi_14_neg": 0.25, "bias_6_neg": 0.25, "kdj_j_neg": 0.20, "rev_1": 0.30},
    },
    {
        "key": "preset_alpha101",
        "name": "Alpha101 精选组合",
        "desc": "从 Alpha101 里挑的 6 个在 A 股相对常见的反转/量价因子。",
        "factors": {"alpha004": 0.20, "alpha006": 0.15, "alpha012": 0.15,
                    "alpha013": 0.15, "alpha044": 0.15, "alpha101": 0.20},
    },
    {
        "key": "preset_volume_price",
        "name": "量价背离组合",
        "desc": "价涨量缩、量价背离的股票后面往往有修复机会。",
        "factors": {"pv_corr_20": 0.30, "alpha003": 0.25, "alpha016": 0.25, "vstd_20_neg": 0.20},
    },
]


def list_presets() -> list:
    return [{"key": p["key"], "name": p["name"], "desc": p["desc"], "factors": p["factors"]}
            for p in PRESETS]


def get_preset(key: str):
    for p in PRESETS:
        if p["key"] == key:
            return p
    return None


# ===========================================================================
# 8. Alpha101 扩充
# ===========================================================================
# 说明（很重要，别跳过）：
#  1. 原公式里的 rank() 是【横截面】排名（同一天比所有股票）。单只股票做因子/回测时
#     没有横截面，所以统一换成【时序】ts_rank(x, N)（和它自己过去 N 天比）。
#  2. 原公式里需要行业/市值中性化的（IndNeutralize、sector、industry、cap）这里【没有实现】，
#     因为那不是单只股票能算的。行业中性化请在【因子实验室】里勾选，那是横截面层面的事，
#     效果等价甚至更好。
#  3. 原公式里那些 2.14593 之类的魔数窗口，统一取整数，避免过拟合噪音。
#  4. 少数公式里 sum(returns, 250) 这类超长窗口，在数据不够时缩短为 120。
#  5. 布尔条件（rank(a) < rank(b)）改成了连续差值，这样 IC 才能算得出来。

def _d(x):
    """把 None 填成 0，方便喂给滚动算子"""
    return [0.0 if v is None else v for v in x]


def _k(x, v):
    return [v] * len(x)


def eabs(x):
    return [None if v is None else abs(v) for v in x]


def emax(a, b):
    return [None if (a[i] is None or b[i] is None) else max(a[i], b[i]) for i in range(len(a))]


def emin(a, b):
    return [None if (a[i] is None or b[i] is None) else min(a[i], b[i]) for i in range(len(a))]


def elog(x):
    return [None if v is None or v <= 0 else math.log(v) for v in x]


def _O(kl):
    return arr(kl, "open")


def _H(kl):
    return arr(kl, "high")


def _L(kl):
    return arr(kl, "low")


def _C(kl):
    return arr(kl, "close")


def _V(kl):
    return arr(kl, "volume")


def _VW(kl):
    return vwap_series(kl)


def _RET(kl):
    return _d(returns(_C(kl)))


def _ADV(kl, n=20):
    return _d(sma(_V(kl), n))


def _tr(x, n):
    """对可能含 None 的序列做 ts_rank（先填 0）"""
    return ts_rank(_d(x), n)


def _add_alpha(num, desc, fn, direction=1):
    key = "alpha%03d" % num
    FACTORS[key] = {"key": key, "name": "Alpha101-%03d" % num, "family": "Alpha101",
                    "desc": desc + " " + A101_NOTE, "direction": direction,
                    "params": [], "fn": fn}


_add_alpha(2, "成交量对数的 2 日变化 与 实体涨跌幅 的时序排名相关性取负。",
           lambda kl: neg(corr(_tr(delta(elog(_V(kl)), 2), 20),
                               _tr(div(sub(_C(kl), _O(kl)), _O(kl)), 20), 6)))

_add_alpha(5, "开盘价相对 10 日成交均价的偏离排名 × 收盘价对 VWAP 偏离的反向程度。",
           lambda kl: mul(_tr(sub(_O(kl), sma(_VW(kl), 10)), 20),
                          neg(eabs(sub(_tr(sub(_C(kl), _VW(kl)), 20), _k(kl, 0.5))))))

_add_alpha(7, "放量时：7 日价格变化的反向动能；缩量时：固定看空。",
           lambda kl: where([None if (a is None or b is None) else (a < b) for a, b in
                             zip(_ADV(kl, 20), _V(kl))],
                            mul(mul(_tr(eabs(delta(_C(kl), 7)), 60), sign(delta(_C(kl), 7))), _k(kl, -1.0)),
                            _k(kl, -1.0)))

_add_alpha(8, "开盘价之和 × 收益之和 的 10 日变化排名取负。",
           lambda kl: neg(_tr(sub(mul(tsum(_O(kl), 5), tsum(_RET(kl), 5)),
                                 delay(mul(tsum(_O(kl), 5), tsum(_RET(kl), 5)), 10)), 20)))

_add_alpha(9, "1 日价格变化：如果连续 5 天都在涨就顺势，连续 5 天都在跌也顺势，其余情况反向。",
           lambda kl: _a9(kl, 5))

_add_alpha(10, "同 Alpha009，窗口改为 4 天。",
           lambda kl: _a9(kl, 4))


def _a9(kl, n):
    d1 = delta(_C(kl), 1)
    dd = _d(d1)
    mn = ts_min(dd, n)
    mx = ts_max(dd, n)
    out = [None] * len(d1)
    for i in range(len(d1)):
        if d1[i] is None or mn[i] is None or mx[i] is None:
            continue
        out[i] = d1[i] if (mn[i] > 0 or mx[i] < 0) else -d1[i]
    return out


_add_alpha(11, "VWAP 与收盘价差的最大/最小位置的排名之和，乘以成交量变化的排名。",
           lambda kl: mul(add(_tr(ts_max(sub(_VW(kl), _C(kl)), 3), 20),
                              _tr(ts_min(sub(_VW(kl), _C(kl)), 3), 20)),
                          _tr(delta(_V(kl), 3), 20)))

_add_alpha(14, "3 日收益变化的排名取负 × 开盘价与成交量的相关性。",
           lambda kl: mul(neg(_tr(delta(_RET(kl), 3), 20)), corr(_O(kl), _V(kl), 10)))

_add_alpha(15, "最高价与成交量的时序排名相关性，再做 3 日求和取负。",
           lambda kl: neg(tsum(_d(corr(_tr(_H(kl), 10), _tr(_V(kl), 10), 3)), 3)))

_add_alpha(17, "收盘价位置排名 × 价格加速度排名 × 量比排名，三者组合取负。",
           lambda kl: neg(mul(mul(_tr(_tr(_C(kl), 10), 10), _tr(delta(delta(_C(kl), 1), 1), 20)),
                              _tr(div(_V(kl), _ADV(kl, 20)), 5))))

_add_alpha(19, "7 日价格动量的方向取负 × 长期收益排名。",
           lambda kl: neg(mul(sign(add(sub(_C(kl), delay(_C(kl), 7)), delta(_C(kl), 7))),
                              add(_k(kl, 1.0), _tr(tsum(_RET(kl), 120), 120)))))

_add_alpha(20, "开盘价相对昨高/昨收/昨低的跳空程度，三者排名相乘取负。",
           lambda kl: neg(mul(mul(_tr(sub(_O(kl), delay(_H(kl), 1)), 20),
                                  _tr(sub(_O(kl), delay(_C(kl), 1)), 20)),
                              _tr(sub(_O(kl), delay(_L(kl), 1)), 20))))

_add_alpha(22, "最高价与成交量相关性的 5 日变化取负 × 收盘价波动率排名。",
           lambda kl: neg(mul(delta(_d(corr(_H(kl), _V(kl), 5)), 5), _tr(std(_C(kl), 20), 20))))


def _a24(kl):
    c = _C(kl)
    m = sma(c, 100)
    dl = delay(m, 100)
    mn = ts_min(c, 100)
    d3 = delta(c, 3)
    out = [None] * len(c)
    for i in range(len(c)):
        if dl[i] is None or not dl[i] or mn[i] is None or d3[i] is None:
            continue
        growth = ((m[i] - dl[i]) / dl[i]) if m[i] is not None else 0.0
        out[i] = -(c[i] - mn[i]) if growth <= 0.05 else -d3[i]
    return out


_add_alpha(24, "横盘 100 天后如果还在低位就买入（做多低位），否则跟随 3 日反向动量。",
           _a24)

_add_alpha(25, "反向收益 × 20日均量 × 成交均价 × 上影线，整体排名。",
           lambda kl: _tr(mul(mul(mul(neg(_RET(kl)), _ADV(kl, 20)), _VW(kl)),
                               sub(_H(kl), _C(kl))), 20))

_add_alpha(28, "20日均量与最低价的相关性，加上当日中价，减去收盘价。",
           lambda kl: sub(add(corr(_ADV(kl, 20), _L(kl), 5),
                              div(add(_H(kl), _L(kl)), _k(kl, 2.0))), _C(kl)))


def _a30(kl):
    c = _C(kl)
    s1 = sign(sub(c, delay(c, 1)))
    s2 = sign(sub(delay(c, 1), delay(c, 2)))
    s3 = sign(sub(delay(c, 2), delay(c, 3)))
    tot = add(s1, s2, s3)
    v5 = tsum(_V(kl), 5)
    v20 = tsum(_V(kl), 20)
    return mul(sub(_k(kl, 1.0), _tr(tot, 20)), div(v5, v20))


_add_alpha(30, "连续上涨/下跌天数的排名（反向）× 短期量能占比。", _a30)

_add_alpha(31, "收盘价位置的三重排名 + 3 日反向变化排名 + 均量与最低价相关性的符号。",
           lambda kl: add(_tr(_tr(decay_linear(neg(_tr(delta(_C(kl), 10), 20)), 10), 10), 10),
                          _tr(neg(delta(_C(kl), 3)), 20),
                          sign(corr(_ADV(kl, 20), _L(kl), 12))))

_add_alpha(32, "价格偏离 7 日均线 + 20 倍 VWAP 与 5 日前收盘的相关性。",
           lambda kl: add(sub(sma(_C(kl), 7), _C(kl)),
                          mul(_k(kl, 20.0), _tr(corr(_VW(kl), delay(_C(kl), 5), 60), 20))))

_add_alpha(35, "成交量排名 × (1 - 价格区间位置排名) × (1 - 收益排名)。",
           lambda kl: mul(mul(_tr(_V(kl), 32),
                              sub(_k(kl, 1.0), _tr(sub(add(_C(kl), _H(kl)), _L(kl)), 16))),
                          sub(_k(kl, 1.0), _tr(_RET(kl), 32))))

_add_alpha(37, "昨日的实体涨跌与今日收盘的相关性排名 + 今日实体涨跌排名。",
           lambda kl: add(_tr(corr(delay(sub(_O(kl), _C(kl)), 1), _C(kl), 60), 20),
                          _tr(sub(_O(kl), _C(kl)), 20)))

_add_alpha(38, "收盘价位置排名取负 × 当日实体涨跌幅排名。",
           lambda kl: mul(neg(_tr(_tr(_C(kl), 10), 20)), _tr(div(_C(kl), _O(kl)), 20)))

_add_alpha(39, "7 日价格变化 × (1 - 量能趋势排名)，整体取负后乘以长期收益。",
           lambda kl: neg(mul(_tr(mul(delta(_C(kl), 7),
                                      sub(_k(kl, 1.0), _tr(decay_linear(div(_V(kl), _ADV(kl, 20)), 9), 20))), 20),
                              add(_k(kl, 1.0), _tr(tsum(_RET(kl), 120), 120)))))

_add_alpha(40, "最高价波动率排名取负 × 最高价与成交量相关性。",
           lambda kl: neg(mul(_tr(std(_H(kl), 10), 20), corr(_H(kl), _V(kl), 10))))

_add_alpha(42, "VWAP 与收盘价差值的排名，除以 VWAP 与收盘价之和的排名。",
           lambda kl: div(_tr(sub(_VW(kl), _C(kl)), 20),
                          add(_tr(add(_VW(kl), _C(kl)), 20), _k(kl, 0.01))))

_add_alpha(45, "5 日前收盘的均值排名 × 收盘量相关 × 长短均线相关性排名，整体取负。",
           lambda kl: neg(mul(mul(_tr(sma(delay(_C(kl), 5), 20), 20),
                                  corr(_C(kl), _V(kl), 2)),
                              _tr(corr(tsum(_C(kl), 5), tsum(_C(kl), 20), 2), 20))))

_add_alpha(50, "成交量排名与 VWAP 排名相关性的 5 日最大值取负。",
           lambda kl: neg(ts_max(_d(_tr(corr(_tr(_V(kl), 10), _tr(_VW(kl), 10), 5), 20)), 5)))

_add_alpha(52, "近 5 日最低价的抬升幅度 × 中长期收益斜率排名 × 成交量排名。",
           lambda kl: mul(mul(add(neg(ts_min(_L(kl), 5)), delay(ts_min(_L(kl), 5), 5)),
                              _tr(div(sub(tsum(_RET(kl), 120), tsum(_RET(kl), 20)), _k(kl, 100.0)), 20)),
                          _tr(_V(kl), 5)))

_add_alpha(55, "收盘价在 12 日区间中的位置排名，与成交量排名的相关性取负。",
           lambda kl: neg(corr(_tr(div(sub(_C(kl), ts_min(_L(kl), 12)),
                                      add(sub(ts_max(_H(kl), 12), ts_min(_L(kl), 12)), _k(kl, 0.001))), 20),
                              _tr(_V(kl), 20), 6)))

_add_alpha(61, "VWAP 离 16 日最低点的距离排名，减去 VWAP 与长期均量相关性排名。",
           lambda kl: sub(_tr(sub(_VW(kl), ts_min(_VW(kl), 16)), 20),
                          _tr(corr(_VW(kl), sma(_V(kl), 60), 18), 20)))

_add_alpha(62, "VWAP 量能相关性排名 与 开盘价强度排名 的差值取负。",
           lambda kl: neg(sub(_tr(corr(_VW(kl), sma(_V(kl), 22), 10), 20),
                             _tr(sub(mul(_k(kl, 2.0), _tr(_O(kl), 20)),
                                     add(_tr(div(add(_H(kl), _L(kl)), _k(kl, 2.0)), 20),
                                         _tr(_H(kl), 20))), 20))))

_add_alpha(65, "开盘价与 VWAP 加权价 与 长期均量 的相关性排名，减去开盘价离 14 日低点距离排名，取负。",
           lambda kl: neg(sub(_tr(corr(add(mul(_O(kl), _k(kl, 0.008)),
                                        mul(_VW(kl), _k(kl, 0.992))), sma(_V(kl), 60), 6), 20),
                             _tr(sub(_O(kl), ts_min(_O(kl), 14)), 20))))

_add_alpha(66, "VWAP 变化 + 收盘价相对 VWAP 与中价差的位置，整体取负。",
           lambda kl: neg(add(_tr(decay_linear(delta(_VW(kl), 4), 7), 20),
                              ts_rank(_d(decay_linear(
                                  div(sub(_L(kl), _VW(kl)),
                                      add(sub(_O(kl), div(add(_H(kl), _L(kl)), _k(kl, 2.0))), _k(kl, 0.001))), 11)), 7))))

_add_alpha(68, "最高价与均量的相关性排名，减去 收盘价与最低价加权价 的排名，取负。",
           lambda kl: neg(sub(ts_rank(_d(corr(_tr(_H(kl), 10), _tr(sma(_V(kl), 15), 10), 9)), 14),
                             _tr(add(mul(_C(kl), _k(kl, 0.518)), mul(_L(kl), _k(kl, 0.482))), 20))))

_add_alpha(72, "中价与均量相关性排名，除以 VWAP 与成交量相关性排名。",
           lambda kl: div(ts_rank(_d(decay_linear(corr(div(add(_H(kl), _L(kl)), _k(kl, 2.0)),
                                                      sma(_V(kl), 40), 9), 10)), 20),
                          add(ts_rank(_d(decay_linear(corr(_tr(_VW(kl), 4), _tr(_V(kl), 19), 7), 3)), 20),
                              _k(kl, 0.01))))

_add_alpha(73, "VWAP 变化排名 与 开盘VWAP加权价反向变化排名 的较大者取负。",
           lambda kl: neg(emax(_tr(decay_linear(delta(_VW(kl), 5), 3), 20),
                               ts_rank(_d(decay_linear(
                                   neg(div(delta(add(mul(_O(kl), _k(kl, 0.147)),
                                                    mul(_VW(kl), _k(kl, 0.853))), 2),
                                            add(mul(_O(kl), _k(kl, 0.147)),
                                                mul(_VW(kl), _k(kl, 0.853))))), 3)), 17))))

_add_alpha(74, "收盘价与均量相关性排名，减去 最高VWAP加权价与成交量相关性排名，取负。",
           lambda kl: neg(sub(_tr(corr(_C(kl), sma(_V(kl), 30), 15), 20),
                             _tr(corr(_tr(add(mul(_H(kl), _k(kl, 0.026)),
                                            mul(_VW(kl), _k(kl, 0.974))), 20),
                                      _tr(_V(kl), 20), 11), 20))))

_add_alpha(75, "VWAP 与成交量相关性排名，减去 最低价与均量相关性排名。",
           lambda kl: sub(_tr(corr(_VW(kl), _V(kl), 4), 20),
                          _tr(corr(_tr(_L(kl), 20), _tr(sma(_V(kl), 50), 20), 12), 20)))

_add_alpha(77, "中价上冲程度排名 与 中价均量相关性排名 的较小者取负。",
           lambda kl: neg(emin(_tr(decay_linear(sub(add(div(add(_H(kl), _L(kl)), _k(kl, 2.0)), _H(kl)),
                                                 add(_VW(kl), _H(kl))), 20), 20),
                               _tr(decay_linear(corr(div(add(_H(kl), _L(kl)), _k(kl, 2.0)),
                                                     sma(_V(kl), 40), 3), 6), 20))))

_add_alpha(83, "振幅相对 5 日均价的延迟排名 × 成交量排名，除以振幅与 VWAP 偏离的比值。",
           lambda kl: div(mul(_tr(delay(div(sub(_H(kl), _L(kl)), sma(_C(kl), 5)), 2), 20),
                              _tr(_tr(_V(kl), 20), 20)),
                          add(div(div(sub(_H(kl), _L(kl)), sma(_C(kl), 5)),
                                  sub(_VW(kl), _C(kl))), _k(kl, 0.001))))

_add_alpha(85, "最高收盘加权价与均量相关性排名，减去 中价与成交量相关性排名。",
           lambda kl: sub(_tr(corr(add(mul(_H(kl), _k(kl, 0.877)),
                                     mul(_C(kl), _k(kl, 0.123))), sma(_V(kl), 30), 10), 20),
                          _tr(corr(_tr(div(add(_H(kl), _L(kl)), _k(kl, 2.0)), 4),
                                  _tr(_V(kl), 10), 7), 20)))

_add_alpha(86, "收盘价与均量相关性排名，减去 价格重心排名，取负。",
           lambda kl: neg(sub(_tr(corr(_C(kl), sma(_V(kl), 20), 6), 20),
                             _tr(sub(add(_O(kl), _C(kl)), add(_VW(kl), _O(kl))), 20))))

_add_alpha(88, "开低与高低排名差的衰减排名 与 收盘均量相关性衰减排名 的较小者取负。",
           lambda kl: neg(emin(_tr(decay_linear(sub(add(_tr(_O(kl), 20), _tr(_L(kl), 20)),
                                                   add(_tr(_H(kl), 20), _tr(_C(kl), 20))), 8), 20),
                               ts_rank(_d(decay_linear(
                                   corr(_tr(_C(kl), 8), _tr(sma(_V(kl), 60), 20), 8), 7)), 3))))

_add_alpha(94, "VWAP 离 12 日低点距离排名，减去 VWAP 与均量相关性排名，取负。",
           lambda kl: neg(sub(_tr(sub(_VW(kl), ts_min(_VW(kl), 12)), 20),
                             ts_rank(_d(corr(_tr(_VW(kl), 20), _tr(sma(_V(kl), 60), 20), 18)), 3))))

_add_alpha(95, "开盘价离 12 日低点距离排名，减去 中价与均量相关性排名。",
           lambda kl: sub(_tr(sub(_O(kl), ts_min(_O(kl), 12)), 20),
                          ts_rank(_d(corr(_tr(div(add(_H(kl), _L(kl)), _k(kl, 2.0)), 19),
                                         _tr(sma(_V(kl), 40), 19), 13)), 12)))

_add_alpha(99, "中价与均量相关性排名，减去 最低价与均量相关性排名，取负。",
           lambda kl: neg(sub(_tr(corr(_tr(div(add(_H(kl), _L(kl)), _k(kl, 2.0)), 19),
                                   _tr(sma(_V(kl), 60), 19), 9), 20),
                             _tr(corr(_tr(_L(kl), 20), _tr(sma(_V(kl), 30), 20), 6), 20))))


# ===========================================================================
# 9. 回调买入法（强势股回踩支撑 + 重启上涨时买入，买点贴近止损）
# ===========================================================================
PULLBACK_NOTE = ("回调买入法的核心不是「买到最低点」，而是「买在离止损最近的地方」："
                 "先找已经在上升趋势里的强势股，等它回调到支撑位附近、并且重新掉头向上时买入，"
                 "止损就放在支撑下面一点点。这样单笔亏损小、盈亏比高，错几次也不会伤筋动骨。")


def pullback_state(klines, p=None):
    """逐步计算回调买入法的状态。

    返回一个和 K 线等长的列表，每项是 dict 或 None（数据不够）。
    dict 字段：
        strong       是否处于上升趋势
        pullback_pct 从近期高点回调了多少 %
        off_high_pct 当前价距近期高点的距离 %
        support      支撑价（趋势均线 / 长期均线 / 近期低点中最接近且在其之上的那个）
        stop         建议止损价
        risk_pct     买入到止损的距离 %（越小越好）
        near_support 是否已经贴近支撑
        shrink       回调过程中是否缩量
        trigger      是否出现回升确认
        score        综合评分 0~1
    """
    p = dict(p or {})
    trend_n = int(p.get("trend_ma", 20))
    long_n = int(p.get("long_ma", 60))
    min_mom = float(p.get("min_mom20", 3.0))
    min_pb = float(p.get("min_pullback", 2.0))
    max_pb = float(p.get("max_pullback", 15.0))
    tol = float(p.get("support_tol", 4.0))
    shrink_ratio = float(p.get("shrink_ratio", 0.85))
    stop_buf = float(p.get("stop_buffer", 2.0))
    max_risk = float(p.get("max_risk", 8.0))
    need_shrink = bool(p.get("use_volume_shrink", True))
    confirm = int(p.get("confirm_days", 1))

    n = len(klines)
    c = arr(klines, "close")
    o = arr(klines, "open")
    h = arr(klines, "high")
    l = arr(klines, "low")
    v = arr(klines, "volume")
    if n < max(long_n, trend_n) + 25:
        return [None] * n
    ma_t = sma(c, trend_n)
    ma_l = sma(c, long_n)
    hh = ts_max(h, 20)
    v5 = sma(v, 5)
    v20 = sma(v, 20)
    r = _d(returns(c))
    rsi = _rsi(c, 14)

    out = [None] * n
    for i in range(max(long_n, trend_n) + 20, n):
        if ma_t[i] is None or ma_l[i] is None or hh[i] is None:
            continue
        px = c[i]
        mom20 = (px / c[i - 20] - 1.0) * 100 if c[i - 20] else 0.0
        strong = (px > ma_t[i] > ma_l[i]) and mom20 >= min_mom
        off_high = (hh[i] - px) / hh[i] * 100 if hh[i] else 0.0
        pullback_ok = min_pb <= off_high <= max_pb
        # 支撑：在趋势均线/长期均线/近 10 日低点里，挑不高于现价且最接近的那个
        cands = [x for x in (ma_t[i], ma_l[i], ts_min(l, 10)[i]) if x is not None and 0 < x <= px * 1.005]
        support = max(cands) if cands else (ma_l[i] if ma_l[i] else px)
        dist = (px - support) / px * 100 if px else 99.0
        near_support = 0 <= dist <= tol
        shrink = (v5[i] is not None and v20[i] is not None and v5[i] <= v20[i] * shrink_ratio) \
            if (v5[i] is not None and v20[i] is not None) else False
        # 回升确认：突破前 confirm 天的最高价，且是阳线
        ok_trigger = False
        if i >= confirm:
            prior_high = max(h[i - confirm:i])
            ok_trigger = px > prior_high and px > o[i]
        stop = support * (1.0 - stop_buf / 100.0)
        stop = min(stop, min(l[max(0, i - 5):i + 1]))
        risk = (px - stop) / px * 100 if px else 99.0

        score = 0.0
        if strong:
            score += 0.30
        if pullback_ok:
            score += 0.22
        if near_support:
            score += 0.20
        if shrink:
            score += 0.10
        if ok_trigger:
            score += 0.12
        if strong and 0 < risk <= max_risk:
            score += 0.18 * (1.0 - risk / max_risk)
        if not strong:
            score *= 0.15          # 回调买入法只做强势股，不在趋势里的直接压到很低
        if risk <= 0 or risk > max_risk:
            score *= 0.5
        out[i] = {
            "strong": bool(strong), "pullback_pct": round(off_high, 2), "off_high_pct": round(off_high, 2),
            "support": round(support, 3), "stop": round(stop, 3), "risk_pct": round(risk, 2),
            "near_support": bool(near_support), "shrink": bool(shrink), "trigger": bool(ok_trigger),
            "mom20": round(mom20, 2), "rsi": round(rsi[i], 1) if rsi[i] is not None else None,
            "score": round(min(score, 1.0), 4),
        }
    return out


@register("pullback_score", "回调买入评分", "回调买入法",
          PULLBACK_NOTE + " 这里把它的五个条件合成为一个 0~1 的连续分数，方便和其它因子一起比较。",
          direction=1)
def f_pullback_score(kl):
    return [None if x is None else x["score"] for x in pullback_state(kl)]


@register("pullback_risk_neg", "买点到止损的距离(取负)", "回调买入法",
          "现价到建议止损位的百分比距离，取负。数值越大（离 0 越近）说明买点越贴近止损、风险越小。",
          direction=1)
def f_pullback_risk(kl):
    return [None if x is None else -x["risk_pct"] for x in pullback_state(kl)]


def _pullback_support_dist(kl):
    """现价高于支撑位的百分比取负：越接近 0 越好"""
    out = []
    c = arr(kl, "close")
    for i, x in enumerate(pullback_state(kl)):
        if x is None:
            out.append(None)
        else:
            px = c[i]
            dist = (px - x["support"]) / px * 100 if px else 0.0
            out.append(-abs(dist))
    return out


FACTORS["pullback_support_dist_neg"] = {
    "key": "pullback_support_dist_neg", "name": "离支撑位的距离(取负)", "family": "回调买入法",
    "desc": "现价高于支撑位的百分比取负：越接近 0 说明越贴近支撑，下跌空间越小。" + PULLBACK_NOTE,
    "direction": 1, "params": [], "fn": _pullback_support_dist,
}
if "回调买入法" not in FAMILIES:
    FAMILIES.append("回调买入法")


# 回调买入法专用预设
PRESETS.append({
    "key": "preset_pullback",
    "name": "回调买入法组合",
    "desc": "强势趋势 + 回踩支撑 + 缩量 + 买点贴近止损，用回调买入评分和风险距离合成。",
    "factors": {"pullback_score": 0.45, "pullback_risk_neg": 0.30, "pullback_support_dist_neg": 0.25},
})


# ===========================================================================
# 10. Alpha101 中文简名
# ===========================================================================
# 光看编号根本不知道是干啥的，所以给每个因子补一个"一眼看懂"的短名字。
# 格式：A编号 + 一句话说明。
A101_SHORT = {
    1: "波动率跳升反转",
    2: "量价变化背离",
    3: "开盘量能背离",
    4: "低价位置反转",
    5: "开盘偏离均价",
    6: "开盘量能负相关",
    7: "放量反转动量",
    8: "开盘收益累计反转",
    9: "五日动量转向",
    10: "四日动量转向",
    11: "量价极值共振",
    12: "放量下跌看多",
    13: "收盘量能协变",
    14: "收益加速反转",
    15: "最高量能背离",
    16: "高价量能协变",
    17: "量价动能共振",
    18: "开收盘结构反转",
    19: "长短期动量反转",
    20: "跳空反转",
    22: "高价量能衰减",
    23: "高位滞涨看空",
    24: "长期横盘低位",
    25: "上影放量反转",
    26: "量价顶部背离",
    28: "均量低价支撑",
    30: "连涨连跌反转",
    31: "价格位置共振",
    32: "短均线偏离",
    33: "开盘折价",
    34: "波动放大看空",
    35: "缩量低位",
    37: "实体相关动能",
    38: "收盘位置与实体背离",
    39: "量能趋势反转",
    40: "高价波动反转",
    41: "价格高于成交均价",
    42: "均价偏离比",
    43: "缩量超跌",
    44: "高价量能负相关",
    45: "滞后价格量能",
    50: "量价同步见顶",
    52: "低点抬升",
    53: "区间位置变化",
    54: "收盘强势度",
    55: "区间位置量能",
    57: "均价偏离衰减",
    60: "高位放量看空",
    61: "均价离底强度",
    62: "均价量能强度",
    65: "开盘均价强度",
    66: "均价变化反转",
    68: "高价量能位置",
    72: "中价量能比",
    73: "均价变化极值",
    74: "收盘量能背离",
    75: "均价量能背离",
    77: "中价上冲反转",
    83: "振幅量能比",
    85: "高价收盘量能",
    86: "收盘量能位置",
    88: "开低量能衰减",
    94: "均价低位强度",
    95: "开盘低位强度",
    99: "中价量能强度",
    101: "日内实体强度",
}

for _n, _short in A101_SHORT.items():
    _key = "alpha%03d" % _n
    if _key in FACTORS:
        FACTORS[_key]["name"] = "A%03d %s" % (_n, _short)


# ===========================================================================
# 深度学习因子（AI 自动挖掘，见 app/dlfactor.py）
# ===========================================================================
# 这两个因子不是人工写公式，而是 dlfactor.py 用小型神经网络从 K 线里学出来的。
# 使用前需先训练：POST /api/dlfactor/train（task=cross_section / time_series）。
# 未训练时 compute 返回全 None，不影响其它因子。
@register("dl_ai_cross", "AI选股因子", "AI深度学习",
          "深度学习（小型神经网络）自动挖掘的横截面选股因子，输出0~1选股分。"
          "需先训练模型（/api/dlfactor/train，task=cross_section）后才有值。",
          direction=1)
def f_dl_ai_cross(kl):
    from . import dlfactor as dlf
    return dlf.ai_factor_score(kl, "cross_section")


@register("dl_ai_timing", "AI择时信号", "AI深度学习",
          "深度学习自动挖掘的单股时序涨跌方向信号，输出0~1多头概率。"
          "需先训练模型（/api/dlfactor/train，task=time_series）后才有值。",
          direction=1)
def f_dl_ai_timing(kl):
    from . import dlfactor as dlf
    return dlf.ai_factor_score(kl, "time_series")



# ---------------------------------------------------------------------------
# 无监督学习因子（不需要训练标签，直接从数据结构里找信息，见 app/dlfactor.py）
# ---------------------------------------------------------------------------
# 和监督学习不同：这几类不需要「未来收益」当答案，装好就能算。
# 它们描述的是「当前状态有多极端 / 多罕见 / 多不合群」，方向要由 IC 实测决定。
UL_NOTE = ("无监督学习因子：不预测涨跌，只描述当前状态在历史分布里的位置。"
           "没有先验方向，务必看因子实验室的 IC 再决定正用还是反用。")
@register("ul_pca_score", "无监督PCA主成分", "AI无监督",
          "把15维特征做PCA，取第一主成分上的投影值。绝对值大 = 当前状态处在主要波动方向的极端位置。" + UL_NOTE)
def f_ul_pca_score(kl):
    from . import dlfactor as dlf
    return dlf.ul_factor(kl, "ul_pca_score")


@register("ul_pca_resid", "无监督PCA残差", "AI无监督",
          "剔除第一主成分后的残差范数。值大 = 这只股票的走势偏离大盘式共同波动，走出独立行情。" + UL_NOTE)
def f_ul_pca_resid(kl):
    from . import dlfactor as dlf
    return dlf.ul_factor(kl, "ul_pca_resid")


@register("ul_ae_err", "无监督自编码器误差", "AI无监督",
          "用自编码器学习历史正常状态，重构误差大 = 当前这种状态历史上很少见（异常）。" + UL_NOTE)
def f_ul_ae_err(kl):
    from . import dlfactor as dlf
    return dlf.ul_factor(kl, "ul_ae_err")


@register("ul_km_dist", "无监督聚类距离", "AI无监督",
          "把历史状态聚成几类，当前到最近类中心的距离。值大 = 当前状态不合群、可能正在切换模式。" + UL_NOTE)
def f_ul_km_dist(kl):
    from . import dlfactor as dlf
    return dlf.ul_factor(kl, "ul_km_dist")
