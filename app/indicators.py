# -*- coding: utf-8 -*-
"""技术指标库（纯 Python 实现，不依赖 numpy/pandas）

所有函数输入一个 list，输出等长的 list，数据不足的位置是 None。
"""
from __future__ import annotations

import math


def _clean(vals):
    return [float(v) if v is not None else 0.0 for v in vals]


def sma(vals, n: int):
    n = int(n)
    out = [None] * len(vals)
    if n <= 0:
        return out
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(vals, n: int):
    n = int(n)
    out = [None] * len(vals)
    if n <= 0 or len(vals) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(vals[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def stddev(vals, n: int):
    n = int(n)
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        seg = vals[i - n + 1:i + 1]
        m = sum(seg) / n
        var = sum((x - m) ** 2 for x in seg) / n
        out[i] = math.sqrt(var)
    return out


def highest(vals, n: int):
    n = int(n)
    out = [None] * len(vals)
    for i in range(len(vals)):
        if i >= n - 1:
            out[i] = max(vals[i - n + 1:i + 1])
    return out


def lowest(vals, n: int):
    n = int(n)
    out = [None] * len(vals)
    for i in range(len(vals)):
        if i >= n - 1:
            out[i] = min(vals[i - n + 1:i + 1])
    return out


def rsi(vals, n: int = 14):
    """Wilder 平滑 RSI"""
    n = int(n)
    out = [None] * len(vals)
    if len(vals) <= n:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag = gains / n
    al = losses / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def macd(vals, fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (DIF, DEA, MACD柱)"""
    ef = ema(vals, fast)
    es = ema(vals, slow)
    dif = [None] * len(vals)
    for i in range(len(vals)):
        if ef[i] is not None and es[i] is not None:
            dif[i] = ef[i] - es[i]
    start = next((i for i, v in enumerate(dif) if v is not None), None)
    dea = [None] * len(vals)
    if start is not None:
        seg = dif[start:]
        e = ema(seg, signal)
        for j, v in enumerate(e):
            dea[start + j] = v
    hist = [None] * len(vals)
    for i in range(len(vals)):
        if dif[i] is not None and dea[i] is not None:
            hist[i] = 2.0 * (dif[i] - dea[i])
    return dif, dea, hist


def boll(vals, n: int = 20, k: float = 2.0):
    """布林带，返回 (中轨, 上轨, 下轨)"""
    mid = sma(vals, n)
    sd = stddev(vals, n)
    up = [None] * len(vals)
    low = [None] * len(vals)
    for i in range(len(vals)):
        if mid[i] is not None and sd[i] is not None:
            up[i] = mid[i] + k * sd[i]
            low[i] = mid[i] - k * sd[i]
    return mid, up, low


def atr(highs, lows, closes, n: int = 14):
    tr = [None] * len(closes)
    for i in range(len(closes)):
        if i == 0:
            tr[i] = highs[i] - lows[i]
        else:
            tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    return ema(tr, n)


def true_range(highs, lows, closes):
    """真实波幅 TR_t = max(H-L, |H-C_prev|, |L-C_prev|)。"""
    tr = [0.0] * len(closes)
    for i in range(len(closes)):
        if i == 0:
            tr[i] = float(highs[i]) - float(lows[i])
        else:
            tr[i] = max(float(highs[i]) - float(lows[i]),
                        abs(float(highs[i]) - float(closes[i - 1])),
                        abs(float(lows[i]) - float(closes[i - 1])))
    return tr


def atr_sma(highs, lows, closes, n: int = 20):
    """简单平均版 ATR（原文口径：N 个交易日 TR 取平均）。

    注意和上面的 atr() 区别：atr() 用的是 EMA（更灵敏、但会加权近期），
    这里按研报原文用简单平均，保证可复现。
    """
    return sma(true_range(highs, lows, closes), n)


def natr(highs, lows, closes, n: int = 20):
    """NATR：收盘价相对「价格中枢」的偏离，用 ATR 归一化。

        NATR_t = (C_t - MA(C, N)) / ATR_t

    含义：现在价格离中枢有几个 ATR。+2 表示高于中枢 2 个波动单位。
    分母 ATR 为 0（长期停牌/一字板）时返回 None，避免除零。
    """
    n = max(1, int(n))
    mid = sma(closes, n)
    atr_ = atr_sma(highs, lows, closes, n)
    out = [None] * len(closes)
    for i in range(len(closes)):
        a = atr_[i]
        m = mid[i]
        if a is None or m is None or a <= 1e-12:
            continue
        out[i] = (float(closes[i]) - float(m)) / float(a)
    return out



def kdj(highs, lows, closes, n: int = 9, m1: int = 3, m2: int = 3):
    length = len(closes)
    rsv = [None] * length
    for i in range(length):
        if i >= n - 1:
            hh = max(highs[i - n + 1:i + 1])
            ll = min(lows[i - n + 1:i + 1])
            rsv[i] = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100.0
    kk = [None] * length
    dd = [None] * length
    jj = [None] * length
    pk, pd = 50.0, 50.0
    for i in range(length):
        if rsv[i] is None:
            continue
        pk = (m1 - 1) / m1 * pk + 1.0 / m1 * rsv[i]
        pd = (m2 - 1) / m2 * pd + 1.0 / m2 * pk
        kk[i], dd[i], jj[i] = pk, pd, 3 * pk - 2 * pd
    return kk, dd, jj


def crossover(a, b, i: int) -> bool:
    """a 在第 i 根上穿 b"""
    if i < 1:
        return False
    if a[i] is None or b[i] is None or a[i - 1] is None or b[i - 1] is None:
        return False
    return a[i - 1] <= b[i - 1] and a[i] > b[i]


def crossunder(a, b, i: int) -> bool:
    if i < 1:
        return False
    if a[i] is None or b[i] is None or a[i - 1] is None or b[i - 1] is None:
        return False
    return a[i - 1] >= b[i - 1] and a[i] < b[i]


def volatility(closes, n: int = 20):
    """年化波动率（%）"""
    if len(closes) < 3:
        return None
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1]:
            rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 2:
        return None
    seg = rets[-n:]
    m = sum(seg) / len(seg)
    var = sum((x - m) ** 2 for x in seg) / max(len(seg) - 1, 1)
    return math.sqrt(var) * math.sqrt(244) * 100.0


def max_drawdown(equity):
    peak = -1e18
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd * 100.0


def annualized_return(equity, days: float):
    if not equity or len(equity) < 2 or days <= 0:
        return 0.0
    total = equity[-1] / equity[0] - 1.0
    years = days / 244.0
    if years <= 0:
        return 0.0
    try:
        return ((1.0 + total) ** (1.0 / years) - 1.0) * 100.0
    except Exception:
        return 0.0


def sharpe(daily_returns, rf_annual: float = 0.0):
    if not daily_returns:
        return 0.0
    n = len(daily_returns)
    m = sum(daily_returns) / n
    if n < 2:
        return 0.0
    var = sum((x - m) ** 2 for x in daily_returns) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return (m - rf_annual / 244.0) / sd * math.sqrt(244.0)


def sortino(daily_returns):
    if not daily_returns:
        return 0.0
    n = len(daily_returns)
    m = sum(daily_returns) / n
    downs = [x for x in daily_returns if x < 0]
    if not downs:
        return 0.0
    dd = math.sqrt(sum(x * x for x in downs) / len(downs))
    if dd == 0:
        return 0.0
    return m / dd * math.sqrt(244.0)


def compute_set(klines: list, which: list | None = None) -> dict:
    """给 K 线列表算一批常用指标，返回 {指标名: [值...]}，用于前端画图"""
    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    out: dict = {}
    want = set(which or ["ma5", "ma10", "ma20", "ma60", "boll", "macd", "rsi", "kdj"])
    if "ma5" in want:
        out["ma5"] = sma(closes, 5)
    if "ma10" in want:
        out["ma10"] = sma(closes, 10)
    if "ma20" in want:
        out["ma20"] = sma(closes, 20)
    if "ma30" in want:
        out["ma30"] = sma(closes, 30)
    if "ma60" in want:
        out["ma60"] = sma(closes, 60)
    if "boll" in want:
        mid, up, low = boll(closes, 20, 2.0)
        out["boll_mid"], out["boll_up"], out["boll_low"] = mid, up, low
    if "macd" in want:
        dif, dea, hist = macd(closes)
        out["macd_dif"], out["macd_dea"], out["macd_hist"] = dif, dea, hist
    if "rsi" in want:
        out["rsi6"] = rsi(closes, 6)
        out["rsi14"] = rsi(closes, 14)
    if "kdj" in want:
        kk, dd, jj = kdj(highs, lows, closes)
        out["kdj_k"], out["kdj_d"], out["kdj_j"] = kk, dd, jj
    if "atr" in want:
        out["atr14"] = atr(highs, lows, closes, 14)
    return out
