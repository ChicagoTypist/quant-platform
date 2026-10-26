# -*- coding: utf-8 -*-
"""因子实验室

回答三个问题：
  1. 这个因子到底有没有用？  -> IC（信息系数）、ICIR、t值、多空收益
  2. 因子值高的股票后面涨得多吗？ -> 分层（分位数）收益
  3. 现在哪个因子最强？ -> 全因子扫描排行榜

IC 是什么：把当天所有股票按因子值排序，再按未来 N 日收益排序，
两条排名序列的相关系数就是当天的 IC。IC 长期为正说明"因子值高 -> 后面涨得多"。
业界经验：|IC| > 0.03 就有点用，> 0.05 算不错，> 0.1 很强（但很少见，且会失效）。

注意：这里算出来的都是历史统计，不代表未来。因子会失效、会拥挤。
"""
from __future__ import annotations

import datetime as dt
import math
import threading
import time

from . import datasource as ds
from . import factors as F
from . import store

# 默认扫描股票池：40 只有代表性的、成交活跃的股票（覆盖金融/消费/科技/医药/能源等）
DEFAULT_POOL = [
    "600519.SH", "601318.SH", "600036.SH", "000001.SZ", "000002.SZ",
    "600030.SH", "601166.SH", "600887.SH", "000858.SZ", "002415.SZ",
    "300750.SZ", "601012.SH", "600276.SH", "000333.SZ", "000651.SZ",
    "601888.SH", "600900.SH", "601899.SH", "600028.SH", "601857.SH",
    "601398.SH", "601288.SH", "600016.SH", "601668.SH", "600048.SH",
    "000725.SZ", "002594.SZ", "300059.SZ", "002230.SZ", "600309.SH",
    "000568.SZ", "603288.SH", "601633.SH", "600690.SH", "000063.SZ",
    "600585.SH", "601601.SH", "600050.SH", "601988.SH", "600585.SH",
]

MIN_CROSS_SECTION = 8      # 少于这么多只股票就不做横截面 IC，改用时序 IC


# ---------------------------------------------------------------------------
# 统计工具
# ---------------------------------------------------------------------------
def _rank(vals):
    """平均秩（处理并列）"""
    n = len(vals)
    order = sorted(range(n), key=lambda i: vals[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(a, b):
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((v - ma) ** 2 for v in a)
    vb = sum((v - mb) ** 2 for v in b)
    if va <= 1e-15 or vb <= 1e-15:
        return None
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / math.sqrt(va * vb)


def spearman(a, b):
    if len(a) < 3:
        return None
    return _pearson(_rank(a), _rank(b))


def _stats(ics):
    n = len(ics)
    if n == 0:
        return {}
    m = sum(ics) / n
    if n > 1:
        sd = (sum((v - m) ** 2 for v in ics) / (n - 1)) ** 0.5
    else:
        sd = 0.0
    return {
        "ic_mean": m,
        "ic_std": sd,
        "icir": (m / sd) if sd > 1e-12 else 0.0,
        "ic_positive_ratio": sum(1 for v in ics if v > 0) / n,
        "t_stat": (m / sd * math.sqrt(n)) if sd > 1e-12 else 0.0,
        "ic_count": n,
    }


# ---------------------------------------------------------------------------
# 数据面板
# ---------------------------------------------------------------------------
# 硬性下限：少于这么多根K线就真的没法算了（次新股/退市/代码写错）
HARD_MIN_BARS = 30
# 期望下限：够算大部分中短周期因子。只作为提示，不直接丢弃数据。
GOOD_MIN_BARS = 80

# 最近一次 load_panel 的详细情况，供上层做错误诊断（不改变函数返回值）
LAST_PANEL_INFO: dict = {}


def _fetch_one(sym: str, days: int):
    """单只股票取数。自己吞掉所有异常，返回 (sym, klines, err)。

    注意：不能让它抛异常 —— 一旦抛到线程池外面，整个并发拉取就崩了，
    表现就是「一只股票把整个股票池拖垮」。
    """
    try:
        ks = ds.kline(sym, "daily", 1, int(days))
        ks = [k for k in ks if k.get("close")]
        if len(ks) < HARD_MIN_BARS:
            return sym, None, ("只有 %d 根K线（至少需要 %d 根，可能是次新股、停牌或代码有误）"
                               % (len(ks), HARD_MIN_BARS))
        return sym, ks, None
    except Exception as e:  # noqa: BLE001
        return sym, None, ds.err_brief(e)


def load_panel(symbols, days=400, progress=None):
    """拉取股票池历史数据，返回 {symbol: klines}

    为了「换什么股票池都能稳定拿到数据」做了三件事：
      1. 并发拉取（4 线程）：串行时一只卡 10 秒，几十只就全超时；
      2. 放宽数据量门槛：>= 30 根就收（以前要 80 根，次新股会被整批丢掉）；
      3. 每只失败原因都记下来，汇总到 LAST_PANEL_INFO，上层能给出明确的诊断，
         而不是一句干巴巴的「没取到行情数据」。
    """
    global LAST_PANEL_INFO
    raw = [s for s in (symbols or []) if s]
    norm, bad = [], []
    for s in raw:
        try:
            norm.append(ds.normalize(s))
        except Exception as e:  # noqa: BLE001
            bad.append({"symbol": str(s), "error": "代码格式不对: %s" % str(e)[:60]})
    # 去重保序
    seen = set()
    norm = [s for s in norm if not (s in seen or seen.add(s))]

    panel: dict = {}
    failures: list = list(bad)
    total = max(len(norm), 1)
    done = 0
    t0 = time.time()
    if norm:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_fetch_one, s, days): s for s in norm}
            for f in as_completed(futs):
                try:
                    sym, ks, err = f.result()
                except Exception as e:  # noqa: BLE001  理论上不会到这里
                    sym, ks, err = futs[f], None, str(e)[:120]
                done += 1
                if ks:
                    panel[sym] = ks
                else:
                    failures.append({"symbol": sym, "error": err or "未知原因"})
                    store.log("WARN", "factor", "取 %s 数据失败: %s" % (sym, (err or "")[:100]))
                if progress:
                    progress(int(done / total * 30),
                             "拉取行情 %d/%d（成功 %d）" % (done, total, len(panel)))

    LAST_PANEL_INFO = {
        "requested": len(raw), "total": len(norm), "ok": len(panel),
        "failed": len(failures), "failures": failures[:30],
        "elapsed": round(time.time() - t0, 1), "hard_min": HARD_MIN_BARS,
        "sources": ds.kline_source_status(),
    }
    return panel


def _panel_index(panel):
    """构建「与因子无关」的面板索引（日期序列 / 每只票的日期→下标 / 名称 / 收盘价）。

    这些只依赖面板本身，跟具体是哪个因子没关系。扫描时 130 个因子如果每个都
    重新构建一遍，等于把同一份索引重复算 130 次（40 只 × 400 根要排十几万次序）。
    抽出来构建一次、复用到底，扫描能明显提速。
    """
    dates = sorted(set(k["date"][:10] for ks in panel.values() for k in ks))
    idx = {sym: {k["date"][:10]: i for i, k in enumerate(ks)} for sym, ks in panel.items()}
    names = {sym: ks[-1].get("name") or sym for sym, ks in panel.items()}
    closes = {sym: [k["close"] for k in ks] for sym, ks in panel.items()}
    return {"dates": dates, "idx": idx, "names": names, "closes": closes}


def _build(panel, factor_key, params=None, horizon=5, index=None):
    """把面板整理成按日期对齐的横截面数据"""
    if index is None:
        index = _panel_index(panel)
    values = {}
    for sym, ks in panel.items():
        # 单只股票算因子失败（数据异常、除零、极端值……）不能拖垮整个因子，
        # 这只就当「没数据」，其它股票照算。否则一个坏数据能让 126 个因子全挂。
        try:
            values[sym] = F.compute(factor_key, ks, params)
        except Exception as e:  # noqa: BLE001
            store.log("WARN", "factor", "因子 %s 在 %s 上算失败: %s"
                      % (factor_key, sym, str(e)[:80]))
            values[sym] = [None] * len(ks)
    out = dict(index)
    out["values"] = values
    return out


# ---------------------------------------------------------------------------
# 单因子分析
# ---------------------------------------------------------------------------
def _neutralize_pairs(pairs, ind_map):
    """行业中性化：把同一行业里的因子值减去行业均值，只留下"行业内相对强弱"

    这样就不会出现"整个行业都在涨，所以这个行业的股票因子分都高"的假信号。
    行业里股票太少（不足 3 只）就不动它，避免把噪音当成信号。
    """
    if not ind_map:
        return pairs
    groups: dict = {}
    for v, f, s in pairs:
        groups.setdefault(ind_map.get(s, "其他"), []).append((v, f, s))
    out = []
    for g, items in groups.items():
        if len(items) >= 3:
            mv = sum(x[0] for x in items) / len(items)
            out.extend([(x[0] - mv, x[1], x[2]) for x in items])
        else:
            out.extend(items)
    return out


def analyze(factor_key: str, symbols: list, days: int = 400, horizon: int = 5,
            params: dict | None = None, panel=None, neutralize: str = "none",
            index=None) -> dict:
    meta = F.FACTORS.get(factor_key)
    if meta is None:
        return {"ok": False, "message": "没有这个因子: %s" % factor_key}
    symbols = [ds.normalize(s) for s in (symbols or []) if s]
    if not symbols:
        symbols = DEFAULT_POOL[:12]
    if panel is None:
        panel = load_panel(symbols, days)
    if not panel:
        return {"ok": False, "message": "没取到任何行情数据，请检查代码或网络"}
    horizon = max(1, int(horizon))

    d = _build(panel, factor_key, params, horizon, index=index)
    values, closes, names, dates, idx = d["values"], d["closes"], d["names"], d["dates"], d["idx"]
    ind_map = ds.industry_map() if neutralize == "industry" else {}

    # ---- 横截面 IC ----
    ic_series = []
    quant_buckets: list = []      # 每次调仓的每组收益
    n_groups = min(5, max(2, len(panel) // 4))
    for dt_ in dates:
        pairs = []
        for sym in panel:
            i = idx[sym].get(dt_)
            if i is None or i + horizon >= len(closes[sym]):
                continue
            v = values[sym][i]
            c0, c1 = closes[sym][i], closes[sym][i + horizon]
            if v is None or not c0 or not c1:
                continue
            pairs.append((v, c1 / c0 - 1.0, sym))
        if neutralize == "industry" and len(pairs) >= MIN_CROSS_SECTION:
            pairs = _neutralize_pairs(pairs, ind_map)
        if len(pairs) >= MIN_CROSS_SECTION:
            ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
            if ic is not None:
                ic_series.append({"date": dt_, "ic": round(ic, 5), "n": len(pairs)})
        if len(pairs) >= n_groups * 2:
            pairs.sort(key=lambda x: x[0], reverse=bool(meta["direction"] >= 0))
            size = len(pairs) // n_groups
            row = []
            for g in range(n_groups):
                seg = pairs[g * size:(g + 1) * size] if g < n_groups - 1 else pairs[g * size:]
                row.append(sum(p[1] for p in seg) / len(seg) if seg else 0.0)
            quant_buckets.append(row)

    ics = [x["ic"] for x in ic_series]
    stats = _stats(ics)
    stats["mode"] = "cross_section" if ic_series else "none"

    # ---- 横截面不够时，退回时序 IC（单只股票自己和自己比）----
    ts_ics = []
    if not ic_series:
        stats["mode"] = "time_series"
        for sym in panel:
            v = values[sym]
            c = closes[sym]
            pairs = []
            for i in range(len(c) - horizon):
                if v[i] is None or not c[i] or not c[i + horizon]:
                    continue
                pairs.append((v[i], c[i + horizon] / c[i] - 1.0))
            if len(pairs) >= 60:
                ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
                if ic is not None:
                    ts_ics.append(ic)
                    ic_series.append({"date": "", "ic": round(ic, 5), "n": len(pairs), "symbol": sym})
        stats = _stats(ts_ics)
        stats["mode"] = "time_series"

    # ---- 分层收益 ----
    quantiles = []
    if quant_buckets:
        for g in range(n_groups):
            vals = [row[g] for row in quant_buckets if g < len(row)]
            if not vals:
                continue
            mean_r = sum(vals) / len(vals)
            cum = 1.0
            for v in vals:
                cum *= (1 + v)
            quantiles.append({
                "group": g + 1, "label": "第%d组" % (g + 1),
                "mean_forward_return_pct": round(mean_r * 100, 3),
                "cum_return_pct": round((cum - 1) * 100, 2),
                "n": len(vals),
            })
    long_short = None
    if len(quantiles) >= 2:
        long_short = round(quantiles[0]["mean_forward_return_pct"]
                           - quantiles[-1]["mean_forward_return_pct"], 3)

    # ---- 当前打分（今天这个因子给股票池打的分数）----
    latest_date = dates[-1] if dates else ""
    latest = []
    for sym in panel:
        i = idx[sym].get(latest_date)
        if i is None or values[sym][i] is None:
            continue
        latest.append({"symbol": sym, "name": names.get(sym, sym),
                       "value": round(values[sym][i], 6),
                       "close": closes[sym][i],
                       "industry": ind_map.get(sym) or ds.industry_of(sym)})
    latest.sort(key=lambda x: x["value"], reverse=True)

    # ---- 结论 ----
    icm = stats.get("ic_mean", 0.0) or 0.0
    t = abs(stats.get("t_stat", 0.0) or 0.0)
    if t >= 2.0 and abs(icm) >= 0.05:
        verdict, level = "统计上比较显著，历史上有效", "strong"
    elif t >= 1.5 and abs(icm) >= 0.03:
        verdict, level = "有一定效果，可以小仓位试试", "medium"
    elif abs(icm) >= 0.02:
        verdict, level = "效果偏弱，单独用不太行", "weak"
    else:
        verdict, level = "基本无效，不建议单独使用", "none"

    return {
        "ok": True, "factor": {"key": factor_key, "name": meta["name"], "family": meta["family"],
                               "desc": meta["desc"], "direction": meta["direction"]},
        "horizon": horizon, "days": days, "symbol_count": len(panel),
        "neutralize": neutralize,
        "pool": sorted(panel.keys()), "names": names,
        "start": dates[0] if dates else "", "end": latest_date,
        "ic_series": ic_series, **stats,
        "quantiles": quantiles, "long_short_pct": long_short,
        "latest": latest[:40],
        "suggest_direction": 1 if icm >= 0 else -1,
        "verdict": verdict, "level": level,
        "note": ("横截面 IC：每天把股票池按因子值排序，与未来 %d 日收益排序求相关。" % horizon)
                if stats.get("mode") == "cross_section" else
                ("时序 IC：股票太少（少于 %d 只），改成每只股票自己历史上因子值与未来收益的相关性，取平均。"
                 % MIN_CROSS_SECTION),
    }


# ---------------------------------------------------------------------------
# 全因子扫描
# ---------------------------------------------------------------------------
_scan_lock = threading.RLock()
SCAN = {
    "running": False, "progress": 0, "stage": "", "error": "",
    "result": None, "started": "", "finished": "", "params": {},
}
_scan_cache: dict = {}


def _cache_key(symbols, days, horizon, neutralize="none"):
    return "%s|%s|%s|%s" % (",".join(sorted(symbols)), days, horizon, neutralize)


def _load_persisted_scan():
    """程序刚启动时，把上次扫描结果从数据库里读回来"""
    with _scan_lock:
        if SCAN["result"] is not None:
            return
        try:
            blob = store.kv_get("last_scan")
            if blob and blob.get("result"):
                SCAN["result"] = blob["result"]
                SCAN["finished"] = blob.get("at", "")
                SCAN["stage"] = "（上次扫描的结果，重启后自动恢复）"
                SCAN["progress"] = 100
                SCAN["params"] = blob["result"].get("params", {})
        except Exception:
            pass


def scan_status() -> dict:
    if SCAN["result"] is None:
        _load_persisted_scan()
    with _scan_lock:
        return {
            "running": SCAN["running"], "progress": SCAN["progress"], "stage": SCAN["stage"],
            "error": SCAN["error"], "started": SCAN["started"], "finished": SCAN["finished"],
            "params": SCAN["params"], "result": SCAN["result"],
        }


def start_scan(symbols: list | None = None, days: int = 300, horizon: int = 5,
               force: bool = False, neutralize: str = "none") -> dict:
    # 用户手填的股票池里难免有错字/中文/漏了后缀，不能因为一个坏代码就让整个扫描炸掉。
    # 能认的留下，认不出的收集起来，一个都认不出时给一句人话提示。
    raw = [s for s in (symbols or DEFAULT_POOL) if s]
    norm, bad = [], []
    for s in raw:
        try:
            norm.append(ds.normalize(s))
        except Exception:  # noqa: BLE001
            bad.append(str(s))
    symbols = norm
    if not symbols:
        return {"ok": False,
                "message": "股票代码都不认识：%s。请填 6 位数字代码，例如 600519.SH、000001.SZ"
                           % ("、".join(bad[:5]) or "（空）")}
    # 去重保序
    seen = set()
    symbols = [s for s in symbols if not (s in seen or seen.add(s))]
    neutralize = "industry" if neutralize == "industry" else "none"
    key = _cache_key(symbols, days, horizon, neutralize)
    with _scan_lock:
        if SCAN["running"]:
            return {"ok": False, "message": "已经有一个扫描在跑了，请稍等"}
        if not force and key in _scan_cache:
            cached = _scan_cache[key]
            if time.time() - cached["ts"] < 1800:
                SCAN.update({"result": cached["data"], "finished": cached["at"],
                             "error": "", "progress": 100, "stage": "（直接用了 30 分钟内的缓存结果）",
                             "params": {"symbols": symbols, "days": days, "horizon": horizon,
                                        "neutralize": neutralize}})
                return {"ok": True, "cached": True, "result": cached["data"]}
        SCAN.update({"running": True, "progress": 0, "stage": "准备中",
                     "error": "", "result": None,
                     "started": store.now_str(), "finished": "",
                     "params": {"symbols": symbols, "days": days, "horizon": horizon,
                                "neutralize": neutralize}})
    threading.Thread(target=_scan_worker, args=(symbols, days, horizon, key, neutralize),
                     name="factor-scan", daemon=True).start()
    return {"ok": True, "cached": False, "message": "扫描已开始，大约需要 10~60 秒"}


def _prog(p, stage):
    with _scan_lock:
        SCAN["progress"] = max(0, min(100, int(p)))
        SCAN["stage"] = stage


def _scan_worker(symbols, days, horizon, key, neutralize="none"):
    try:
        _prog(2, "拉取股票池历史数据…")
        panel = load_panel(symbols, days, progress=lambda p, s: _prog(p, s))
        if not panel:
            # 不再只说一句「没取到行情数据」——把真实原因摊开，
            # 让用户一眼能看出是「代码写错」还是「网络被限流」。
            info = LAST_PANEL_INFO or {}
            fails = info.get("failures") or []
            sample = "；".join("%s（%s）" % (f.get("symbol"), f.get("error"))
                               for f in fails[:3])
            srcs = info.get("sources") or {}
            cooling = [k for k, v in srcs.items() if not v.get("ok")]
            msg = "没取到任何行情数据：%d 只全部失败，耗时 %s 秒" % (
                info.get("failed", len(symbols)), info.get("elapsed", "?"))
            if sample:
                msg += "。原因示例：%s" % sample
            if cooling:
                msg += "。当前被限流冷却的数据源：%s（等 1~3 分钟会自动恢复）" % "、".join(cooling)
            if not fails:
                msg += "。请检查股票代码是否填写正确"
            raise RuntimeError(msg)
        info_good = LAST_PANEL_INFO or {}
        if info_good.get("ok"):
            _prog(30, "行情就绪：成功 %d 只 / 共 %d 只" % (
                info_good.get("ok", len(panel)), info_good.get("total", len(symbols))))
        _prog(32, "开始逐个计算 %d 个因子…" % len(F.FACTORS))
        # 面板索引只构建一次，130 个因子共享（避免每个因子都重复排一遍日期序列）
        panel_index = _panel_index(panel)
        rows = []
        keys = list(F.FACTORS.keys())
        for n, k in enumerate(keys):
            try:
                r = analyze(k, symbols, days, horizon, panel=panel, neutralize=neutralize,
                            index=panel_index)
                if r.get("ok"):
                    rows.append({
                        "key": k, "name": r["factor"]["name"], "family": r["factor"]["family"],
                        "desc": r["factor"]["desc"], "prior_direction": r["factor"]["direction"],
                        "ic_mean": round(r.get("ic_mean", 0) or 0, 5),
                        "icir": round(r.get("icir", 0) or 0, 4),
                        "t_stat": round(r.get("t_stat", 0) or 0, 3),
                        "ic_positive_ratio": round(r.get("ic_positive_ratio", 0) or 0, 3),
                        "ic_count": r.get("ic_count", 0),
                        "long_short_pct": r.get("long_short_pct"),
                        "suggest_direction": r.get("suggest_direction", 1),
                        "verdict": r.get("verdict", ""), "level": r.get("level", ""),
                        "mode": r.get("mode", ""),
                    })
            except Exception as e:  # noqa: BLE001
                store.log("WARN", "factor", "因子 %s 分析失败: %s" % (k, str(e)[:120]))
            _prog(32 + int((n + 1) / len(keys) * 63), "分析因子 %d/%d：%s" % (n + 1, len(keys), k))
        rows.sort(key=lambda x: abs(x["ic_mean"]), reverse=True)
        for i, r in enumerate(rows):
            r["rank"] = i + 1
        result = {
            "ok": True, "generated_at": store.now_str(),
            "params": {"symbols": symbols, "days": days, "horizon": horizon,
                       "pool_size": len(panel), "factor_count": len(rows),
                       "neutralize": neutralize},
            "top": rows[:25],
            "all": rows,
        }
        with _scan_lock:
            SCAN.update({"running": False, "progress": 100, "stage": "完成",
                         "result": result, "finished": store.now_str()})
        _scan_cache[key] = {"ts": time.time(), "data": result, "at": store.now_str()}
        try:
            # 持久化，重启程序后排行榜和 AI 助手的因子模型还在
            store.kv_set("last_scan", {"result": result, "at": store.now_str(),
                                       "neutralize": neutralize})
        except Exception:
            pass
        store.log("INFO", "factor", "因子扫描完成：%d 个因子 × %d 只股票" % (len(rows), len(panel)))
        # 扫描完就顺手把选股结果预热一下（此时刚好有了"当前最有效的因子"）
        try:
            from . import assistant as ast
            ast.warm_picks()
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001
        with _scan_lock:
            SCAN.update({"running": False, "error": str(e)[:300], "stage": "失败"})
        store.log("ERROR", "factor", "因子扫描失败: %s" % str(e)[:200])


def get_scan() -> dict:
    """取上次扫描结果（内存里没有就尝试返回空）"""
    with _scan_lock:
        return SCAN["result"]


# ---------------------------------------------------------------------------
# 组合打分：给一组因子按权重算综合分
# ---------------------------------------------------------------------------
def composite(klines: list, weights: dict, normalize: str = "rank", ortho: bool = False) -> list:
    """把多个因子按权重合成一个分数

    normalize:
      rank  - 各因子先转成时序百分位(0~1)再加权（推荐，不同量纲不会互相压制）
      z     - 转成 z-score 再加权
    ortho:
      True  - 先把因子做时序正交化（Gram-Schmidt），再去加权。
              同一个家族里的因子（比如 mom_5 / mom_10 / mom_20）彼此高度相关，
              不处理的话动量被重复计了好几次、暴露被悄悄集中。正交化后每个因子
              只贡献它「独有的」信息，避免重复计数。注意：因子顺序即权重字典的顺序，
              排在前面的因子是锚点（保留原样），后面的依次剔除与前面重复的部分。
    """
    n = len(klines)
    if not weights:
        return [None] * n
    used = []          # [(权重, 归一化后并按先验方向翻转过的序列)]
    for key, w in weights.items():
        meta = F.FACTORS.get(key)
        if meta is None or not w:
            continue
        raw = F.compute(key, klines)
        d = 1.0 if meta["direction"] >= 0 else -1.0
        norm = F.ts_rank(raw, 120) if normalize == "rank" else _zscore(raw, 120)
        used.append((float(w), [None if v is None else d * v for v in norm]))
    # 某个因子如果整段都没有数据（比如备用数据源没给成交额），
    # 就把它剔掉，不要让一个坏因子把整个组合分都变成 None
    # —— 那会导致策略一个信号都出不来，而且很难查。
    dropped = [k for (k, _w), (_wo, arr) in zip(list(weights.items()), used)
               if not any(v is not None for v in arr)]
    if dropped:
        used = [(w, arr) for (w, arr) in used if any(v is not None for v in arr)]
    if not used:
        return [None] * n
    # 可选：时序正交化，消除同族因子的重复计数
    if ortho and len(used) > 1:
        arrs = [arr for _w, arr in used]
        orth = orthogonalize_series(arrs)
        used = [(used[i][0], orth[i]) for i in range(len(used))]
    total_w = sum(abs(w) for w, _ in used) or 1.0
    out = []
    for i in range(n):
        s = 0.0
        ok = True
        for w, arr in used:
            if arr[i] is None:
                ok = False
                break
            s += arr[i] * w
        out.append(s / total_w if ok else None)
    return out


def _zscore(x, n):
    out = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < n:
            continue
        seg = x[i - n + 1:i + 1]
        if not F._ok(seg):
            continue
        m = sum(seg) / n
        sd = (sum((v - m) ** 2 for v in seg) / n) ** 0.5
        out[i] = (seg[-1] - m) / sd if sd > 1e-12 else 0.0
    return out


# ---------------------------------------------------------------------------
# 因子相关性 & 正交化
# ---------------------------------------------------------------------------
def _project_out(x, prev):
    """把序列 x 里「和 prev 线性相关」的成分剔除，返回残差。

    即：x_res = x - beta·prev，其中 beta = cov(prev,x)/var(prev)。
    两个序列都是 None 的位置不参与回归，残差里同样保留 None 位置。
    """
    n = len(x)
    pairs = [(x[i], prev[i]) for i in range(n)
             if x[i] is not None and prev[i] is not None]
    if len(pairs) < 3:
        return [None] * n
    mp = sum(p[1] for p in pairs) / len(pairs)
    mx = sum(p[0] for p in pairs) / len(pairs)
    vp = sum((p[1] - mp) ** 2 for p in pairs)
    if vp <= 1e-15:
        return [None] * n
    beta = sum((p[1] - mp) * (p[0] - mx) for p in pairs) / vp
    out = []
    for i in range(n):
        if x[i] is None or prev[i] is None:
            # 无法投影（当天两个因子至少缺一个），保留 x 原值（None 仍为 None）
            out.append(x[i])
        else:
            out.append(x[i] - beta * prev[i])
    return out


def orthogonalize_series(series_list):
    """对一组等长序列做 Gram-Schmidt 正交化。

    顺序敏感：第 0 个序列原样保留，第 k 个序列依次剔除它在前 k 个序列方向上的投影。
    返回和输入等长的「去相关后」序列列表。
    """
    if not series_list:
        return []
    out = [list(series_list[0])]
    for k in range(1, len(series_list)):
        x = list(series_list[k])
        for prev in out:
            x = _project_out(x, prev)
        out.append(x)
    return out


def _avg_xcorr(da, db, panel, index):
    """两个因子在股票池上的「平均横截面相关」：每个交易日算一次 Spearman 相关再取平均。

    这是判断「两个因子是不是在说同一件事」最直接的办法——如果它们每天给股票的排序
    都高度一致，那它们就是冗余的。
    """
    dates = index["dates"]
    idx = index["idx"]
    corrs = []
    for d in dates:
        xs, ys = [], []
        for sym in panel:
            i = idx[sym].get(d)
            if i is None:
                continue
            va = da["values"][sym][i]
            vb = db["values"][sym][i]
            if va is None or vb is None:
                continue
            xs.append(va)
            ys.append(vb)
        if len(xs) >= MIN_CROSS_SECTION:
            c = spearman(xs, ys)
            if c is not None:
                corrs.append(c)
    if not corrs:
        return None
    return sum(corrs) / len(corrs)


def factor_correlation(factor_keys: list, symbols: list, days: int = 400,
                       threshold: float = 0.7) -> dict:
    """计算一组因子两两之间的相关性，找出冗余组合。

    用途：多因子组合最怕「表面好几个因子、实际在测同一个东西」。比如把 mom_5、
    mom_10、mom_20、mom_60 都塞进组合，它们彼此相关 0.8+，动量等于被计了 4 遍，
    一旦动量失效整个组合一起崩。先跑这个函数，把高度相关的因子去掉/合并，组合才真的分散。

    返回：相关矩阵、冗余对（|corr| >= threshold）、以及一个「去重后建议保留的因子」列表。
    """
    keys = []
    for k in factor_keys or []:
        if k in F.FACTORS:
            keys.append(k)
    if len(keys) < 2:
        return {"ok": False,
                "message": "至少需要 2 个有效因子才能算相关性（收到 %d 个）" % len(keys)}

    panel = load_panel(symbols, days)
    if not panel:
        return {"ok": False, "message": "没取到任何行情数据，请检查代码或网络"}

    index = _panel_index(panel)
    fact_vals = {}
    for k in keys:
        fact_vals[k] = _build(panel, k, index=index)

    # 两两相关
    pairs = []
    mat = {}
    nk = len(keys)
    for i in range(nk):
        row = []
        for j in range(nk):
            if i == j:
                row.append(1.0)
            elif j < i:
                v = mat[keys[j]][i]
                row.append(round(v, 4) if v is not None else None)
            else:
                c = _avg_xcorr(fact_vals[keys[i]], fact_vals[keys[j]], panel, index)
                c = round(c, 4) if c is not None else None
                row.append(c)
                if c is not None and abs(c) >= threshold:
                    pairs.append({"a": keys[i], "b": keys[j],
                                  "name_a": F.FACTORS[keys[i]]["name"],
                                  "name_b": F.FACTORS[keys[j]]["name"],
                                  "family_a": F.FACTORS[keys[i]]["family"],
                                  "family_b": F.FACTORS[keys[j]]["family"],
                                  "corr": c})
        mat[keys[i]] = row

    # 贪婪去重：按 |相关| 从高到低处理，后出现的因子若和已保留的因子高度相关就建议剔除
    pairs_sorted = sorted(pairs, key=lambda p: -abs(p["corr"]))
    dropped = set()
    keep = []
    for k in keys:
        redundant = False
        for kept_k in keep:
            # 直接查矩阵里两者的相关性
            ci = keys.index(k)
            kj = keys.index(kept_k)
            cv = mat[kept_k][ci]
            if cv is not None and abs(cv) >= threshold:
                redundant = True
                break
        if redundant:
            dropped.add(k)
        else:
            keep.append(k)

    advice = []
    if pairs:
        for p in pairs_sorted[:10]:
            advice.append("「%s」与「%s」相关系数 %.2f，几乎在测同一个东西，建议只保留其一。"
                          % (p["name_a"], p["name_b"], p["corr"]))

    return {
        "ok": True,
        "symbols": sorted(panel.keys()), "days": days, "threshold": threshold,
        "factors": [{"key": k, "name": F.FACTORS[k]["name"], "family": F.FACTORS[k]["family"]}
                    for k in keys],
        "matrix": {"factors": keys, "data": mat},
        "redundant_pairs": pairs_sorted,
        "keep": keep,
        "drop": sorted(dropped),
        "advice": advice,
        "note": ("相关系数 = 每个交易日这两个因子给股票池排序的 Spearman 相关的平均值。"
                 "|相关| 越接近 1 越冗余，超过 %.2f 就建议合并或删掉一个。" % threshold),
    }
