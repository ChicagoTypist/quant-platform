# -*- coding: utf-8 -*-
"""组合优化器

解决的问题：算出"每只股票买多少钱"。

同一批候选股票，怎么分配权重，长期结果差别很大：
  * 等权        最简单，但把风险平均分给了高波动股
  * 因子加权    按因子得分分配，敢重仓最看好的
  * 最小方差    在给定约束下把组合波动压到最低
  * 风险平价    让每只股票贡献的风险一样大（比等权稳很多）
  * 最大夏普    在收益和风险之间找最优平衡

全部是纯 Python 实现，不依赖 numpy。股票数一般不超过 50 只，速度没问题。
所有方法都支持两个硬约束：单只上限、总仓位上限、只做多（不允许做空）。
"""
from __future__ import annotations

import math

METHODS = [
    {"key": "equal", "name": "等权分配", "desc": "每只股票分一样的钱。最简单，也是最难被打败的基准。"},
    {"key": "factor", "name": "因子分加权", "desc": "因子得分越高买越多。进攻性强，但会集中押注。"},
    {"key": "min_var", "name": "最小方差", "desc": "在约束内让组合波动最小。熊市抗跌，牛市跑得慢。"},
    {"key": "risk_parity", "name": "风险平价", "desc": "让每只股票贡献的风险一样大。低波动股拿更多权重，长期夏普通常更好。"},
    {"key": "max_sharpe", "name": "最大夏普", "desc": "用因子得分当预期收益，找收益/风险比最高的组合。理论最优，但对预期收益估计误差很敏感。"},
]


# ---------------------------------------------------------------------------
# 线性代数小工具
# ---------------------------------------------------------------------------
def cov_matrix(series: list) -> list:
    """series: list[list[float]]，每行是一只股票的历史收益率（等长）"""
    n = len(series)
    if n == 0:
        return []
    t = min(len(s) for s in series)
    if t < 2:
        return [[0.0] * n for _ in range(n)]
    series = [s[-t:] for s in series]          # 对齐到最近的 t 天
    means = [sum(s) / t for s in series]
    cov = [[0.0] * n for _ in range(n)]
    for i in range(n):
        si = series[i]
        for j in range(i, n):
            sj = series[j]
            s = 0.0
            for k in range(t):
                s += (si[k] - means[i]) * (sj[k] - means[j])
            v = s / (t - 1)
            cov[i][j] = v
            cov[j][i] = v
    return cov


def matvec(m, v):
    return [sum(m[i][j] * v[j] for j in range(len(v))) for i in range(len(m))]


def normalize_cov(cov):
    """把协方差矩阵缩放到对角线约为 1，方便用固定的步长做梯度下降"""
    n = len(cov)
    mx = max((cov[i][i] for i in range(n)), default=0.0)
    if mx <= 0:
        return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    return [[cov[i][j] / mx for j in range(n)] for i in range(n)]


def project(v_target, total, cap):
    """把权重缩放到 {0 <= w <= cap, sum(w) = total}

    关键点：一定要【按比例】放大缩小，而不是给每个权重加同一个常数
    —— 加常数会把算法辛苦算出来的相对差异全部抹平（等权化了）。
    超过单只上限的部分，按比例摊给还没满的股票。
    """
    n = len(v_target)
    if n == 0:
        return []
    cap = max(cap, 1e-9)
    total = min(total, cap * n)
    x = [max(t, 0.0) for t in v_target]
    s = sum(x)
    if s <= 1e-12:
        x = [1.0] * n
        s = float(n)
    w = [t / s * total for t in x]
    for _ in range(200):
        over = 0.0
        free = []
        for i in range(n):
            if w[i] > cap:
                over += w[i] - cap
                w[i] = cap
            else:
                free.append(i)
        if over <= 1e-12 or not free:
            break
        fs = sum(w[i] for i in free)
        if fs <= 1e-12:
            for i in free:
                w[i] += over / len(free)
        else:
            for i in free:
                w[i] += over * (w[i] / fs)
    return w


# ---------------------------------------------------------------------------
# 各种权重算法
# ---------------------------------------------------------------------------
def w_equal(n, total, cap):
    return project([1.0] * n, total, cap)


def w_factor(scores, total, cap):
    """得分越高权重越大。得分先转成正数（减最小值再加个底），避免全是负数"""
    n = len(scores)
    if n == 0:
        return []
    lo = min(scores)
    raw = [(s - lo) + 0.1 for s in scores]
    s = sum(raw)
    if s <= 0:
        return w_equal(n, total, cap)
    return project([r / s for r in raw], total, cap)


def w_min_var(cov, total, cap, iters=600, lr=0.15):
    n = len(cov)
    if n == 0:
        return []
    m = normalize_cov(cov)
    w = [total / n] * n
    for it in range(iters):
        g = matvec(m, w)
        step = lr / (1.0 + it * 0.01)
        w = project([w[i] - step * 2.0 * g[i] for i in range(n)], total, cap)
    return w


def w_risk_parity(cov, total, cap, iters=400):
    """风险平价：迭代让每只股票的边际风险贡献相等"""
    n = len(cov)
    if n == 0:
        return []
    m = normalize_cov(cov)
    w = [1.0 / n] * n
    for _ in range(iters):
        sw = matvec(m, w)
        port_var = sum(w[i] * sw[i] for i in range(n))
        if port_var <= 1e-14:
            break
        target = port_var / n
        nw = []
        for i in range(n):
            rc = w[i] * sw[i]
            if rc <= 1e-14:
                nw.append(w[i] * 1.05 + 1e-6)
            else:
                nw.append(w[i] * (target / rc) ** 0.25)   # 0.25 是阻尼指数，收敛更稳
        s = sum(nw)
        if s <= 0:
            break
        w = [x / s for x in nw]
    return project(w, total, cap)


def w_max_sharpe(cov, mu, total, cap, iters=700, lr=0.08):
    n = len(cov)
    if n == 0:
        return []
    m = normalize_cov(cov)
    ms = max((abs(x) for x in mu), default=0.0)
    if ms <= 1e-12:
        return w_min_var(cov, total, cap)
    mu_n = [x / ms for x in mu]
    w = w_min_var(cov, total, cap)
    best, best_v = list(w), -1e18
    for it in range(iters):
        sw = matvec(m, w)
        var = sum(w[i] * sw[i] for i in range(n))
        if var <= 1e-14:
            break
        vol = math.sqrt(var)
        ret = sum(w[i] * mu_n[i] for i in range(n))
        sharpe = ret / vol if vol > 0 else 0.0
        if sharpe > best_v:
            best_v, best = sharpe, list(w)
        # dSharpe/dw = mu/vol - ret/(vol^3) * Σw
        g = [mu_n[i] / vol - (ret / (vol ** 3)) * sw[i] for i in range(n)]
        step = lr / (1.0 + it * 0.004)
        w = project([w[i] + step * g[i] for i in range(n)], total, cap)
    return best


# ---------------------------------------------------------------------------
# 额外硬约束（行业中性 / 换手 / 流动性）
# ---------------------------------------------------------------------------
def apply_constraints(w: list, symbols: list, industry_map: dict | None = None,
                      max_industry_pct: float | None = None,
                      adv_map: dict | None = None, max_adv_pct: float | None = None,
                      capital: float | None = None,
                      current_weights: dict | None = None,
                      max_turnover_pct: float | None = None) -> list:
    """对一组百分比权重做「机构级」硬约束投影，返回调整后的权重（百分比）。

    三个约束都是实盘真实会碰到的：
      1. 流动性约束  单只仓位不能超过「当日成交额(ADV)的 X%」，否则一建仓就把自己
                     买成对手盘、想卖时也卖不掉。
      2. 行业上限    单个行业的总仓位不能超过 X%，避免整个组合押注在一个行业上。
      3. 换手上限    新旧权重的总变动（买入+卖出的总幅度）不能超过 X%，控制交易成本。

    约束之间可能有冲突，这里按「流动性 -> 行业 -> 换手」的顺序逐个投影，是业界常用的
    顺序逼近。施加约束后总仓位可能低于目标（留现金），这是正常的——想同时满足所有
    约束时，有时就是没法满仓。
    """
    n = len(symbols)
    w = [max(0.0, x) for x in w]
    if n == 0:
        return w

    # 1) 流动性上限：position <= (max_adv_pct%) × ADV
    #    weight% = position/capital × 100 = max_adv_pct × ADV / capital
    if adv_map and max_adv_pct is not None and capital:
        for i, s in enumerate(symbols):
            adv = float(adv_map.get(s, 0) or 0)
            if adv > 0:
                cap_adv = max_adv_pct * (adv / float(capital))
                w[i] = min(w[i], cap_adv)

    # 2) 行业上限
    if industry_map and max_industry_pct is not None:
        groups: dict = {}
        for i, s in enumerate(symbols):
            groups.setdefault(industry_map.get(s, "其他"), []).append(i)
        for idxs in groups.values():
            total = sum(w[i] for i in idxs)
            if total > max_industry_pct and total > 0:
                scale = max_industry_pct / total
                for i in idxs:
                    w[i] *= scale

    # 3) 换手上限：朝旧权重收缩，直到总变动 = 预算
    if current_weights and max_turnover_pct is not None:
        cw = [max(0.0, float(current_weights.get(s, 0.0))) for s in symbols]
        diff = sum(abs(w[i] - cw[i]) for i in range(n))
        if diff > max_turnover_pct:
            lo, hi = 0.0, 1.0
            for _ in range(60):
                mid = (lo + hi) / 2.0
                wm = [w[i] + mid * (cw[i] - w[i]) for i in range(n)]
                if sum(abs(wm[i] - cw[i]) for i in range(n)) > max_turnover_pct:
                    lo = mid
                else:
                    hi = mid
            w = [w[i] + hi * (cw[i] - w[i]) for i in range(n)]

    return w


def industry_breakdown(weights: dict, industry_map: dict | None) -> dict:
    """按行业汇总权重（百分比），用于展示行业是否过于集中"""
    out: dict = {}
    if not industry_map:
        return out
    for s, w in weights.items():
        ind = industry_map.get(s, "其他")
        out[ind] = round(out.get(ind, 0.0) + w, 3)
    return dict(sorted(out.items(), key=lambda x: -x[1]))


# ---------------------------------------------------------------------------
# 对外主函数
# ---------------------------------------------------------------------------
def optimize(method: str, symbols: list, returns_series: dict, scores: dict | None = None,
             total_pct: float = 90.0, max_single_pct: float = 20.0,
             industry_map: dict | None = None, max_industry_pct: float | None = None,
             adv_map: dict | None = None, max_adv_pct: float | None = None,
             capital: float | None = None, current_weights: dict | None = None,
             max_turnover_pct: float | None = None) -> dict:
    """算权重

    method       : equal / factor / min_var / risk_parity / max_sharpe
    symbols      : 候选股票（已按看好程度排好序）
    returns_series: {symbol: [日收益率...]}
    scores       : {symbol: 因子得分}，factor / max_sharpe 用得上
    total_pct    : 总仓位上限（%）
    max_single_pct: 单只上限（%）
    —— 以下为可选硬约束（机构级风控，缺省关闭）——
    industry_map / max_industry_pct : 行业总仓位上限（行业中性）
    adv_map / max_adv_pct / capital : 流动性约束（单只 <= 当日成交额 X%）
    current_weights / max_turnover_pct : 换手率上限（%）
    返回 {ok, method, weights: {symbol: 百分比}, ...}
    """
    symbols = [s for s in symbols if s in returns_series and len(returns_series[s]) >= 20]
    n = len(symbols)
    if n == 0:
        return {"ok": False, "message": "没有可用的股票（需要至少 20 天历史收益）"}
    total = max(0.0, min(total_pct, 100.0))
    cap = max(0.1, min(max_single_pct, 100.0))
    if cap * n < total:
        # 股票太少、单只上限太低，装不下目标仓位，按比例放宽
        cap = min(100.0, total / n)

    if method == "equal":
        w = w_equal(n, total, cap)
    elif method == "factor":
        if not scores:
            return {"ok": False, "message": "因子加权需要提供因子得分"}
        w = w_factor([float(scores.get(s, 0.0)) for s in symbols], total, cap)
    elif method == "min_var":
        w = w_min_var(cov_matrix([returns_series[s] for s in symbols]), total, cap)
    elif method == "risk_parity":
        w = w_risk_parity(cov_matrix([returns_series[s] for s in symbols]), total, cap)
    elif method == "max_sharpe":
        sc = [float((scores or {}).get(s, 0.0)) for s in symbols]
        if max((abs(x) for x in sc), default=0.0) <= 1e-12:
            sc = [1.0] * n
        w = w_max_sharpe(cov_matrix([returns_series[s] for s in symbols]), sc, total, cap)
    else:
        return {"ok": False, "message": "没有这个优化方法: %s" % method}

    # 应用行业中性 / 流动性 / 换手 约束
    applied = False
    if (max_industry_pct is not None) or (adv_map and max_adv_pct is not None) \
            or (current_weights and max_turnover_pct is not None):
        w = apply_constraints(w, symbols, industry_map, max_industry_pct,
                              adv_map, max_adv_pct, capital,
                              current_weights, max_turnover_pct)
        applied = True

    weights = {symbols[i]: round(w[i], 3) for i in range(n)}
    # 组合层面的指标
    cov = cov_matrix([returns_series[s] for s in symbols])
    wf = [w[i] / 100.0 for i in range(n)]
    var = sum(wf[i] * sum(cov[i][j] * wf[j] for j in range(n)) for i in range(n))
    vol_daily = math.sqrt(max(var, 0.0))
    contrib = []
    for i in range(n):
        rc = wf[i] * sum(cov[i][j] * wf[j] for j in range(n))
        contrib.append(rc / var if var > 0 else 0.0)
    note = ""
    if cap * n <= total + 1e-9:
        note = ("注意：单只上限 %.1f%% × %d 只 = %.1f%%，已经装不下 %.1f%% 的目标仓位，"
                "所以各方法算出来的权重会被上限压得差不多（接近等权）。"
                "想让优化器真正发挥作用，请多选几只股票，或者把单只上限调低。"
                % (max_single_pct, n, max_single_pct * n, total))
    if applied and sum(w) < total - 0.5:
        note = (note + " 由于行业/流动性/换手约束，最终总仓位 %.1f%% 低于目标 %.1f%%，"
                "剩余留作现金。" % (sum(w), total)).strip()

    return {
        "ok": True, "method": method, "note": note,
        "weights": weights,
        "total_pct": round(sum(w), 3),
        "max_single_pct": round(max(w), 3) if w else 0,
        "n": n,
        "annual_vol_pct": round(vol_daily * math.sqrt(244) * 100, 2),
        "risk_contribution": {symbols[i]: round(contrib[i] * 100, 2) for i in range(n)},
        "industry_breakdown": industry_breakdown(weights, industry_map),
        "symbols": symbols,
    }
