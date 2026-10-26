# -*- coding: utf-8 -*-
"""全市场自动选股

和主流量化选股的做法对齐，分三步：

    ① 粗筛   把全市场 5900 多只先用硬条件过一遍（剔除 ST、次新、停牌、涨跌停、
             成交额太小的、市值不合适的、板块不对的）
    ② 打分   对剩下的股票做横截面因子打分（把每个因子的值换成百分位排名，
             再按权重加权）。这是所有券商金工多因子模型的标准做法。
    ③ 精筛   只对排名靠前的短名单去拉 K 线，算真实波动率、均线多头、突破、回调位置

为什么能这么快？因为东财的 clist 接口一次就能拿到全市场的
基本面（PE/PB/ROE/营收增速/净利润增速/毛利率）+ 量价（涨幅/换手/量比/振幅）
+ 资金流（主力净流入/净占比）+ 行业，一次批量调用约 12 秒，
不需要对 5900 只股票逐个拉 K 线。
"""
from __future__ import annotations

import datetime as dt
import math
import threading

from . import datasource as ds

# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
SNAP_FIELDS = ("f12,f13,f14,f2,f3,f5,f6,f7,f8,f9,f10,f11,f20,f21,f22,f23,f24,f25,"
               "f26,f37,f38,f39,f41,f45,f46,f49,f58,f62,f72,f78,f84,f100,f102,f103,"
               "f114,f115,f184,f221")

_SNAP = {"ts": 0.0, "rows": [], "meta": {}}
_SNAP_LOCK = threading.Lock()
_SNAP_FILE = "screener_snapshot.json"


def _snap_path():
    try:
        from . import config as _cfg
        return _cfg.BASE_DIR / "data" / _SNAP_FILE
    except Exception:
        import pathlib
        return pathlib.Path("data") / _SNAP_FILE


def _save_snap(rows: list, meta: dict) -> None:
    """把全市场快照存到磁盘。东财限流的时候，这就是救命稻草。"""
    try:
        import json
        p = _snap_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"rows": rows, "meta": meta}, ensure_ascii=False),
                     encoding="utf-8")
    except Exception:
        pass


def _load_snap(max_age_hours: float = 24.0):
    """读上次存的全市场快照。太旧的不要（行情差太多，选出来没意义）。"""
    try:
        import json
        import time as _t
        p = _snap_path()
        if not p.exists():
            return None
        age_h = (_t.time() - p.stat().st_mtime) / 3600.0
        if age_h > max_age_hours:
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        rows = d.get("rows") or []
        meta = dict(d.get("meta") or {})
        if len(rows) < 1000:
            return None
        meta["stale"] = True
        meta["age_hours"] = round(age_h, 2)
        return {"rows": rows, "meta": meta}
    except Exception:
        return None


def _num(v, default=None):
    """东财的缺值会给 '-' 或者空串"""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v) if not (isinstance(v, float) and math.isnan(v)) else default
    s = str(v).strip()
    if s in ("", "-", "--", "None", "nan"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def board_of(code: str) -> str:
    """按代码前缀判断板块"""
    c = str(code or "").strip()
    if c.startswith(("688", "689")):
        return "科创板"
    if c.startswith(("300", "301", "302")):
        return "创业板"
    if c.startswith(("8", "43", "92")):
        return "北交所"
    if c.startswith(("600", "601", "603", "605")):
        return "沪主板"
    if c.startswith(("000", "001", "002", "003")):
        return "深主板"
    return "其他"


def _is_st(name: str) -> bool:
    n = (name or "").upper().replace(" ", "")
    return ("ST" in n) or ("退" in n) or n.startswith("*")


def snapshot(force: bool = False, ttl: float = 300.0) -> dict:
    """全市场快照（带缓存）。返回 {"rows": [...], "meta": {...}}"""
    now = __import__("time").time()
    with _SNAP_LOCK:
        if not force and _SNAP["rows"] and (now - _SNAP["ts"] < ttl):
            return {"rows": _SNAP["rows"], "meta": _SNAP["meta"]}

    rows, fetch_err = [], ""
    try:
        rows = _fetch_snapshot()
    except Exception as e:  # noqa: BLE001
        fetch_err = str(e)[:160]

    # 东财会限流，经常只抓到一部分页面。
    # 这里用【新抓到的 + 缓存里的】做并集，缺的用缓存补上，
    # 而不是「抓到一半就当整体」（那会悄悄缩小选股范围，你根本看不出来）。
    cached = _load_snap()
    # 全市场应有只数：用东财返回的真实总数（约 5925），拿不到才用 5900 兜底。
    # 以前写死 4500——既不是真实只数，又让「是否完整」的判断永远失真。
    FULL = float(_LAST_TOTAL["n"]) if _LAST_TOTAL["n"] > 1000 else 5900.0

    # 【一条都没抓到】= 东财列表接口被限流/封禁（表现为 RemoteDisconnected）。
    # 这种情况必须单独处理，且要在「和缓存合并」之前判断：
    # 否则 0 只 + 5925 只缓存 = 5925 只，长度够了就走进正常分支，
    # 对外报告 count=5925 / complete=True / fetched_at=现在，
    # 用户看到的就是「刚抓完、数据完整」——实际上全是几小时前的旧数据，纯属谎报。
    if not rows and cached:
        with _SNAP_LOCK:
            _SNAP["rows"] = cached["rows"]
            _SNAP["ts"] = now
            _SNAP["meta"] = cached["meta"]
        try:
            from . import store
            store.log("WARN", "screener",
                      "一只都没抓到最新的（%s），全部使用 %.1f 小时前的缓存（%d 只）"
                      % (fetch_err or "返回数据太少",
                         cached["meta"].get("age_hours") or 0, len(cached["rows"])))
        except Exception:
            pass
        return {"rows": cached["rows"], "meta": cached["meta"]}

    partial = 0
    fresh = len(rows)
    if cached and len(rows) < FULL:
        have = {r.get("code") for r in rows}
        add = [r for r in cached["rows"] if r.get("code") not in have]
        partial = len(add)
        rows = list(rows) + add

    if len(rows) < 1000:
        if cached:
            with _SNAP_LOCK:
                _SNAP["rows"] = cached["rows"]
                _SNAP["ts"] = now
                _SNAP["meta"] = cached["meta"]
            try:
                from . import store
                store.log("WARN", "screener",
                          "拿不到最新全市场行情（%s），先用 %.1f 小时前的缓存（%d 只）。"
                          % (fetch_err or "返回数据太少", cached["meta"].get("age_hours") or 0,
                             len(cached["rows"])))
            except Exception:
                pass
            return {"rows": cached["rows"], "meta": cached["meta"]}
        return {"rows": [], "meta": {"count": 0, "error": fetch_err,
                                     "fetched_at": "", "boards": {}, "stale": False}}

    complete = len(rows) >= FULL
    if partial:
        try:
            from . import store
            store.log("WARN", "screener",
                      "这次只抓到 %d 只最新行情，已用缓存补上 %d 只（合计 %d/%d 只，%s）"
                      % (fresh, partial, len(rows), int(FULL),
                         "已完整" if complete else "多跑几次会慢慢补全"))
        except Exception:
            pass

    meta = {
        "count": len(rows),
        "fresh": fresh,
        "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "boards": {},
        # 只要是靠缓存补齐的，就标记成「不是全部最新」
        "stale": bool(partial) or not complete,
        "partial": partial, "complete": complete,
    }
    for r in rows:
        meta["boards"][r["board"]] = meta["boards"].get(r["board"], 0) + 1
    with _SNAP_LOCK:
        _SNAP["rows"] = rows
        _SNAP["ts"] = now
        _SNAP["meta"] = meta
    # 存的是【新抓到的 + 缓存】的并集，所以越跑越全，不会覆盖成更差的
    _save_snap(rows, meta)
    return {"rows": rows, "meta": meta}


_LAST_TOTAL = {"n": 0}      # 东财 clist 返回的全市场总数，用来自动算页数


def _fetch_page(pn: int, pz: int = 200) -> list:
    import urllib.parse
    url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=%d&po=1&np=1"
           "&fltt=2&invt=2&fid=f12&fs=%s&fields=%s"
           % (pn, pz, urllib.parse.quote(ds.ALL_A_FS, safe=":,"), SNAP_FIELDS))
    js = ds.http_get_json(url)
    data = (js or {}).get("data") or {}
    try:
        tot = int(data.get("total") or 0)
        if tot > 1000:
            _LAST_TOTAL["n"] = tot
    except Exception:
        pass
    return data.get("diff") or []


def _fetch_snapshot() -> list:
    """并发把全市场抓下来，转成好用的字典

    ⚠️ 关键：东财 clist 的 pz 参数有**硬上限 100**——传 200 或 500，实际也只返回
    100 条（实测验证）。所以页数必须按真实每页 100 条来算。
    以前按请求的 pz=200 算页数（5925/200=30 页），30 页 × 100 条 = 3000 只，
    剩下近 3000 只永远抓不到——这就是「只抓到 4001/4500」的真正原因。
    """
    pz = 100
    first = []
    try:
        first = _fetch_page(1, pz)
    except Exception:
        first = []
    if not first:
        raise RuntimeError("东财 clist 第一页就拿不到（很可能被限流）")
    # 优先用东财返回的真实全市场只数算页数；拿不到才用 5925 兜底
    total = _LAST_TOTAL["n"] or 5925
    # 第一页若因限流给得特别少，按实际条数估算，别漏页
    per_page = len(first) if 0 < len(first) < pz else pz
    pages = (total + per_page - 1) // per_page
    pages = max(1, min(pages, 120))
    out = list(first)
    rest = list(range(2, pages + 1))
    lock = threading.Lock()
    errs = []

    def worker(pn):
        try:
            got = _fetch_page(pn, pz)
            with lock:
                out.extend(got)
        except Exception as e:  # noqa: BLE001
            with lock:
                errs.append("%s:%s" % (pn, str(e)[:60]))

    threads = []
    for pn in rest:
        th = threading.Thread(target=worker, args=(pn,), daemon=True)
        threads.append(th)
        if len(threads) >= 10:
            for x in threads:
                x.start()
            for x in threads:
                x.join()
            threads = []
    for x in threads:
        x.start()
    for x in threads:
        x.join()

    # 有页面没抓到就再补一遍（东财偶尔抽风）。以前只补前 20 页，
    # 失败页一多就会静默漏股票；这里放宽到全部失败页（上限 60，防止无限重试）。
    if errs:
        failed = []
        for e in errs:
            try:
                failed.append(int(str(e).split(":")[0]))
            except Exception:
                continue
        for i, pn in enumerate(failed[:60]):
            try:
                got = _fetch_page(pn, pz)
                with lock:
                    out.extend(got)
            except Exception:
                pass
            if i and i % 5 == 0:
                __import__("time").sleep(0.3)

    rows = []
    seen = set()
    for it in out:
        code = str(it.get("f12") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        mk = _num(it.get("f13"), 0)
        try:
            sym = ds.normalize(code)
        except Exception:
            sym = code
        name = str(it.get("f14") or "").strip()
        price = _num(it.get("f2"))
        rows.append({
            "symbol": sym, "code": code, "name": name, "board": board_of(code),
            "price": price,
            "chg_pct": _num(it.get("f3")),
            "volume": _num(it.get("f5")),
            "amount": _num(it.get("f6")),
            "amplitude": _num(it.get("f7")),
            "turnover": _num(it.get("f8")),
            "pe_dyn": _num(it.get("f9")),
            "vol_ratio": _num(it.get("f10")),
            "chg_5min": _num(it.get("f11")),
            "mktcap": _num(it.get("f20")),
            "float_cap": _num(it.get("f21")),
            "speed": _num(it.get("f22")),
            "pb": _num(it.get("f23")),
            "chg_60d": _num(it.get("f24")),
            "chg_ytd": _num(it.get("f25")),
            "listed": _num(it.get("f26")),
            "roe": _num(it.get("f37")),
            "shares": _num(it.get("f38")),
            "float_shares": _num(it.get("f39")),
            "rev_yoy": _num(it.get("f41")),
            "net_profit": _num(it.get("f45")),
            "profit_yoy": _num(it.get("f46")),
            "gross_margin": _num(it.get("f49")),
            "net_assets": _num(it.get("f58")),
            "main_in": _num(it.get("f62")),
            "big_in": _num(it.get("f72")),
            "mid_in": _num(it.get("f78")),
            "small_in": _num(it.get("f84")),
            "industry": str(it.get("f100") or "").strip() or "未知",
            "area": str(it.get("f102") or "").strip(),
            "concepts": str(it.get("f103") or "").strip(),
            "pe_static": _num(it.get("f114")),
            "pe_ttm": _num(it.get("f115")),
            "main_pct": _num(it.get("f184")),
            "report_date": it.get("f221"),
            "is_st": _is_st(name),
        })
    return rows


# ---------------------------------------------------------------------------
# 硬过滤（粗筛）
# ---------------------------------------------------------------------------
DEFAULT_FILTERS = {
    "exclude_st": True,          # 剔除 ST / 退市
    "min_listed_days": 120,      # 剔除上市不足 N 天的次新股
    "min_amount": 50000000.0,    # 最小成交额（元），默认 5000 万
    "max_amount": 0.0,           # 0 = 不限
    "min_mktcap": 2000000000.0,  # 最小总市值（元），默认 20 亿
    "max_mktcap": 0.0,           # 0 = 不限
    "min_price": 3.0,            # 剔除太低价的（仙股风险）
    "max_price": 0.0,
    "exclude_limit_up": True,    # 剔除涨停（买不进）
    "exclude_limit_down": True,  # 剔除跌停（可能要出事）
    "exclude_loss": False,       # 剔除亏损股（PE<=0）
    "boards": ["沪主板", "深主板", "创业板", "科创板"],   # 北交所默认不要
    "industries_exclude": [],
    "industries_include": [],
}


def apply_filters(rows: list, flt: dict | None = None) -> tuple[list, dict]:
    """按硬条件过一遍，返回 (通过的股票, 每一步剔除了多少)"""
    f = dict(DEFAULT_FILTERS)
    f.update(flt or {})
    stats = {"总数": len(rows)}
    out = []

    today = dt.date.today()
    for r in rows:
        reason = None

        if f["exclude_st"] and r["is_st"]:
            reason = "ST/退市"
        elif r["price"] is None or not r["amount"]:
            reason = "停牌/无成交"
        elif f["boards"] and r["board"] not in f["boards"]:
            reason = "板块不符"
        elif f["min_listed_days"] and r["listed"]:
            try:
                listed = dt.datetime.strptime(str(int(r["listed"])), "%Y%m%d").date()
                if (today - listed).days < int(f["min_listed_days"]):
                    reason = "次新股"
            except Exception:
                pass
        if not reason and f["industries_exclude"] and r["industry"] in f["industries_exclude"]:
            reason = "行业排除"
        if not reason and f["industries_include"] and r["industry"] not in f["industries_include"]:
            reason = "行业不在白名单"
        if not reason and f["min_amount"] and (r["amount"] or 0) < f["min_amount"]:
            reason = "成交额太小"
        if not reason and f["max_amount"] and (r["amount"] or 0) > f["max_amount"]:
            reason = "成交额太大"
        if not reason and f["min_mktcap"] and (r["mktcap"] or 0) < f["min_mktcap"]:
            reason = "市值太小"
        if not reason and f["max_mktcap"] and (r["mktcap"] or 0) > f["max_mktcap"]:
            reason = "市值太大"
        if not reason and f["min_price"] and (r["price"] or 0) < f["min_price"]:
            reason = "股价太低"
        if not reason and f["max_price"] and (r["price"] or 0) > f["max_price"]:
            reason = "股价太高"
        if not reason and f["exclude_loss"]:
            pe = r["pe_ttm"] if r["pe_ttm"] is not None else r["pe_dyn"]
            if pe is not None and pe <= 0:
                reason = "亏损股"
        if not reason and f["exclude_limit_up"]:
            lim = 20.0 if r["board"] in ("创业板", "科创板") else 10.0
            if (r["chg_pct"] or 0) >= lim - 0.15:
                reason = "涨停买不进"
        if not reason and f["exclude_limit_down"]:
            lim = 20.0 if r["board"] in ("创业板", "科创板") else 10.0
            if (r["chg_pct"] or 0) <= -(lim - 0.15):
                reason = "跌停"

        if reason:
            stats[reason] = stats.get(reason, 0) + 1
        else:
            out.append(r)

    stats["通过"] = len(out)
    return out, stats


# ---------------------------------------------------------------------------
# 因子（横截面）
# ---------------------------------------------------------------------------
def _pe(r):
    v = r["pe_ttm"] if r["pe_ttm"] is not None else r["pe_dyn"]
    if v is None or v <= 0:
        return None
    return v


def _value_score(v: dict):
    """把估值数据换算成 0~1 的价值分（越高说明"越便宜/越有吸引力"）

    - PE 近 5 年分位越低越好（当前估值处在历史低位）
    - PB 近 5 年分位越低越好
    - 股息率越高越好（0~6% 映射到 0~1）
    数据不足返回 None（调用方会跳过，不影响排序）。
    """
    if not isinstance(v, dict):
        return None
    parts = []
    try:
        pe = v.get("pe_pct_5y")
        if pe is not None:
            parts.append(1.0 - max(0.0, min(float(pe), 100.0)) / 100.0)
        pb = v.get("pb_pct_5y")
        if pb is not None:
            parts.append(1.0 - max(0.0, min(float(pb), 100.0)) / 100.0)
        dy = v.get("dividend_yield")
        if dy is not None:
            parts.append(max(0.0, min(float(dy), 6.0)) / 6.0)
    except (TypeError, ValueError):
        return None
    if not parts:
        return None
    return sum(parts) / len(parts)


def enrich_fundamental(rows: list, limit: int = 80, budget_sec: float = 35.0) -> tuple:
    """对候选股票补上估值分位（PE/PB 近 5 年分位、股息率），并打价值分。

    全部是「尽力而为」：没令牌、没网络、超时、取不到，一律直接跳过，
    绝不抛异常、绝不影响选股主流程。返回 (成功只数, 说明文字)。
    """
    try:
        import time as _t
        from . import config as _cfg, fundamental as _fnd
        if not _cfg.get("fundamental.enabled", True):
            return 0, "基本面增强已关闭"
        if not _fnd.token_available():
            return 0, "未配置数据令牌，跳过估值增强（选股结果不受影响）"
    except Exception:  # noqa: BLE001
        return 0, "基本面模块不可用，已跳过"

    cand = [r for r in (rows or []) if isinstance(r, dict) and r.get("symbol")]
    cand = cand[:max(1, int(limit))]
    if not cand:
        return 0, "没有候选股票"

    t0 = _t.time()
    ok = [0]
    lock = threading.Lock()

    def work(r):
        try:
            # 超预算就不再发新请求，避免用户干等
            if _t.time() - t0 > budget_sec:
                return
            v = _fnd.get_valuation(r["symbol"])
            if not v:
                return
            r["_fnd"] = v
            sc = _value_score(v)
            if sc is not None:
                r["_value_score"] = sc
                with lock:
                    ok[0] += 1
        except Exception:  # noqa: BLE001
            return

    threads = []
    for r in cand:
        th = threading.Thread(target=work, args=(r,), daemon=True)
        threads.append(th)
        if len(threads) >= 4:
            for x in threads:
                x.start()
            for x in threads:
                x.join()
            threads = []
    for x in threads:
        x.start()
    for x in threads:
        x.join()

    n = ok[0]
    if n == 0:
        err = ""
        try:
            err = _fnd.last_error()
        except Exception:  # noqa: BLE001
            pass
        return 0, "估值没取到%s，按原结果排序" % ("（%s）" % err if err else "")
    return n, "已为 %d 只补上估值分位（PE/PB 近5年分位、股息率）" % n


def _blend_value(pool: list, wv: float) -> None:
    """把价值分混进最终分并原地重排。

    wv 是价值分权重（0~0.6）。没有价值分的股票保持原分不动。
    抽成独立函数，方便单测，也避免 run() 里逻辑越堆越长。
    """
    try:
        wv = max(0.0, min(float(wv), 0.6))
    except (TypeError, ValueError):
        wv = 0.0
    for r in pool:
        if not isinstance(r, dict):
            continue
        base = r.get("_final")
        if base is None:
            base = r.get("_score", 0.0)
        try:
            base_f = float(base)
        except (TypeError, ValueError):
            base_f = 0.0
        vs = r.get("_value_score")
        if vs is None:
            # 没价值分：分数不动，但也补上 _final，保证前端字段一致
            r["_final"] = round(base_f, 2)
            continue
        try:
            r["_final"] = round(base_f * (1.0 - wv) + float(vs) * 100.0 * wv, 2)
        except (TypeError, ValueError):
            r["_final"] = round(base_f, 2)
    pool.sort(key=lambda x: -float(
        x.get("_final") if isinstance(x, dict) and x.get("_final") is not None
        else (x.get("_score", 0.0) if isinstance(x, dict) else 0.0)))


def _mf_strength(r):
    """主力资金流入强度 = 主力净流入 / 成交额"""
    if not r["amount"] or r["main_in"] is None:
        return None
    return r["main_in"] / r["amount"] * 100.0


# name -> (中文名, 取值函数, 方向)
# 方向 +1 表示越大越好，-1 表示越小越好
SCREEN_FACTORS = {
    "value_pe":        ("低市盈率",     _pe,                                     -1),
    "value_pb":        ("低市净率",     lambda r: (r["pb"] if (r["pb"] or 0) > 0 else None), -1),
    "value_ps":        ("低市销率",     lambda r: (r["mktcap"] / (r["net_assets"] or 0)
                                                  if (r["net_assets"] or 0) > 0 else None), -1),
    "quality_roe":     ("高ROE",       lambda r: r["roe"],                        +1),
    "quality_margin":  ("高毛利率",     lambda r: r["gross_margin"],               +1),
    "growth_rev":      ("营收高增长",   lambda r: r["rev_yoy"],                   +1),
    "growth_profit":   ("净利润高增长", lambda r: r["profit_yoy"],                +1),
    "mom_60d":         ("60日动量",     lambda r: r["chg_60d"],                   +1),
    "mom_ytd":         ("年初至今动量", lambda r: r["chg_ytd"],                   +1),
    "rev_today":       ("今日反转",     lambda r: (-r["chg_pct"] if r["chg_pct"] is not None else None), +1),
    "move_today":      ("今日强势",     lambda r: r["chg_pct"],                   +1),
    "turnover":        ("换手活跃",     lambda r: r["turnover"],                  +1),
    "turnover_low":    ("换手温和",     lambda r: r["turnover"],                  -1),
    "vol_ratio":       ("量比放大",     lambda r: r["vol_ratio"],                 +1),
    "liquidity":       ("流动性好",     lambda r: r["amount"],                    +1),
    "money_flow":      ("主力净占比",   lambda r: r["main_pct"],                  +1),
    "money_flow_str":  ("主力流入强度", _mf_strength,                             +1),
    "size_small":      ("小市值",       lambda r: r["mktcap"],                    -1),
    "size_big":        ("大市值",       lambda r: r["mktcap"],                    +1),
    "low_amplitude":   ("低振幅(低波代理)", lambda r: r["amplitude"],              -1),
    "speed":           ("盘中涨速",     lambda r: r["speed"],                     +1),
}


def percentile_ranks(values: list) -> list:
    """把一列数换成 0~100 的百分位排名。缺失值给 50（中性），不参与排序也不影响别人。"""
    idx = [(i, v) for i, v in enumerate(values) if v is not None]
    out = [50.0] * len(values)
    if not idx:
        return out
    idx.sort(key=lambda x: x[1])
    n = len(idx)
    for pos, (i, _) in enumerate(idx):
        out[i] = (pos + 0.5) / n * 100.0
    return out


def _score(rows: list, weights: dict, neutralize_industry: bool = False) -> list:
    """横截面打分：每个因子转百分位，按方向和权重加权"""
    total_w = 0.0
    for k, w in weights.items():
        if k in SCREEN_FACTORS and w:
            total_w += abs(float(w))
    if total_w <= 0:
        total_w = 1.0

    scores = [0.0] * len(rows)
    detail = [{} for _ in rows]

    if neutralize_industry:
        groups = {}
        for i, r in enumerate(rows):
            groups.setdefault(r["industry"], []).append(i)
    else:
        groups = {"ALL": list(range(len(rows)))}

    for key, w in weights.items():
        if key not in SCREEN_FACTORS or not w:
            continue
        label, fn, direction = SCREEN_FACTORS[key]
        w = float(w)
        for _, members in groups.items():
            if len(members) < 3:
                for i in members:
                    detail[i][label] = 50.0
                continue
            vals = []
            for i in members:
                try:
                    vals.append(fn(rows[i]))
                except Exception:
                    vals.append(None)
            ranks = percentile_ranks(vals)
            for pos, i in enumerate(members):
                v = ranks[pos]
                if direction < 0:
                    v = 100.0 - v
                detail[i][label] = v
                scores[i] += v * w

    for i in range(len(scores)):
        scores[i] = scores[i] / total_w
    return scores, detail


# ---------------------------------------------------------------------------
# 精筛（只对短名单拉 K 线）
# ---------------------------------------------------------------------------
def _tech_metrics(symbol: str, bars: int = 90) -> dict:
    """给短名单算技术面指标（波动率/均线多头/突破/回调位置）"""
    try:
        ks = ds.kline(symbol, limit=bars, period="daily", adjust=1)
    except Exception:
        return {}
    if not ks or len(ks) < 30:
        return {}
    close = [k.get("close") for k in ks if k.get("close")]
    high = [k.get("high") for k in ks if k.get("high")]
    vol = [k.get("volume") or 0 for k in ks]
    n = len(close)
    if n < 30:
        return {}

    rets = []
    for i in range(1, n):
        if close[i - 1]:
            rets.append((close[i] - close[i - 1]) / close[i - 1])
    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((x - mean) ** 2 for x in rets) / len(rets) if rets else 0.0
    vol_ann = math.sqrt(var) * math.sqrt(250) * 100.0

    ma5 = sum(close[-5:]) / 5
    ma10 = sum(close[-10:]) / 10
    ma20 = sum(close[-20:]) / 20
    ma60 = sum(close[-60:]) / 60 if n >= 60 else ma20
    bull = 0.0
    if ma5 > ma10:
        bull += 33.0
    if ma10 > ma20:
        bull += 33.0
    if ma20 > ma60:
        bull += 34.0

    hi20 = max(high[-20:]) if high else max(close[-20:])
    last = close[-1]
    breakout = (last / hi20 * 100.0) if hi20 else 100.0

    v5 = sum(vol[-5:]) / 5 if n >= 5 else 0
    v20 = sum(vol[-20:]) / 20 if n >= 20 else 0
    vol_expand = (v5 / v20) if v20 else 1.0

    peak = max(close[-60:]) if n >= 60 else max(close)
    drawdown = (last / peak - 1.0) * 100.0 if peak else 0.0

    return {
        "vol_ann": round(vol_ann, 2),
        "ma_bull": round(bull, 1),
        "breakout": round(breakout, 2),
        "vol_expand": round(vol_expand, 3),
        "drawdown_60d": round(drawdown, 2),
        "ma20": round(ma20, 3),
        "last": round(last, 3),
        "above_ma20": last > ma20,
    }


def _pullback(symbol: str) -> dict:
    """复用因子里的回调买入法（强势股回到支撑附近）

    注意 pullback_state 返回的是【逐根K线】的状态列表，要取最后一个才是当前状态。
    """
    try:
        from . import factors as fac
        ks = ds.kline(symbol, limit=180, period="daily", adjust=1)
        if not ks or len(ks) < 60:
            return {}
        states = fac.pullback_state(ks)
        if not states:
            return {}
        cur = states[-1]
        return cur if isinstance(cur, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 主流策略模板
# ---------------------------------------------------------------------------
PRESETS = {
    "multi_factor": {
        "name": "多因子综合（最主流）",
        "desc": "价值+质量+成长+动量+资金流+流动性，按百分位加权打分。"
                "公募私募和券商金工最常用的框架，追求稳稳跑赢指数。",
        "weights": {"value_pe": 0.8, "value_pb": 0.6, "quality_roe": 1.2, "quality_margin": 0.6,
                    "growth_rev": 0.8, "growth_profit": 0.8, "mom_60d": 0.8,
                    "money_flow": 0.7, "liquidity": 0.5, "low_amplitude": 0.4},
    },
    "value_quality": {
        "name": "价值质量（低估值+高ROE）",
        "desc": "经典的价值投资量化版：便宜 + 赚钱能力强。适合震荡市和防守。",
        "weights": {"value_pe": 1.5, "value_pb": 1.2, "quality_roe": 1.6,
                    "quality_margin": 1.0, "size_big": 0.5, "low_amplitude": 0.6},
    },
    "garp": {
        "name": "GARP 成长价值",
        "desc": "低估值 + 高成长（PEG 思路）。既有安全边际又有成长性，"
                "是很多主动权益基金的核心选股逻辑。",
        "weights": {"value_pe": 1.0, "growth_rev": 1.4, "growth_profit": 1.4,
                    "quality_roe": 1.0, "mom_60d": 0.5},
    },
    "momentum": {
        "name": "动量趋势（强者恒强）",
        "desc": "选已经走强且还在走强的。学术界最经得起检验的因子之一，"
                "但在A股容易在急跌时吃回撤，建议配合止损。",
        "weights": {"mom_60d": 1.6, "mom_ytd": 1.0, "move_today": 0.4,
                    "vol_ratio": 0.6, "liquidity": 0.6},
    },
    "money_flow": {
        "name": "主力资金流",
        "desc": "跟着大单走：主力净流入占比高、流入强度大。"
                "短线有效，但要防主力对倒，别单独用。",
        "weights": {"money_flow": 1.8, "money_flow_str": 1.4, "vol_ratio": 0.8,
                    "turnover": 0.6, "liquidity": 0.5},
    },
    "volume_price": {
        "name": "量价异动（放量上攻）",
        "desc": "量比放大 + 换手活跃 + 盘中走强。捕捉刚启动的票。",
        "weights": {"vol_ratio": 1.6, "turnover": 1.2, "move_today": 0.8,
                    "speed": 0.6, "money_flow": 0.6},
    },
    "small_cap": {
        "name": "小市值（高风险）",
        "desc": "A股历史上最强的因子之一，但 2017 和 2024 年初都出现过小市值股集体暴跌。"
                "仓位一定要小，务必设止损。",
        "weights": {"size_small": 1.8, "liquidity": 0.8, "quality_roe": 0.8, "value_pb": 0.6},
    },
    "low_vol": {
        "name": "低波动稳健",
        "desc": "低振幅 + 大市值 + 高ROE。长期风险调整后收益通常不错，回撤小。",
        "weights": {"low_amplitude": 1.6, "size_big": 1.0, "quality_roe": 1.0, "value_pe": 0.6},
    },
    "reversal": {
        "name": "超跌反转",
        "desc": "短期跌过头、但基本面还在的股票。左侧交易，需要耐心和纪律。",
        "weights": {"rev_today": 1.6, "value_pb": 0.8, "quality_roe": 0.8, "liquidity": 0.6},
    },
    "pullback_buy": {
        "name": "回调买入（强势股回踩支撑）",
        "desc": "先找强势股，再等它回到支撑附近。买点尽量贴近止损位，"
                "风险收益比好。注意：这个是择时工具，不是选股因子。",
        "weights": {"mom_60d": 1.2, "quality_roe": 0.6, "liquidity": 0.6, "low_amplitude": 0.4},
        "tech": "pullback",
    },
    "quarter_leader": {
        "name": "行业轮动·龙头",
        "desc": "按行业分组，每个行业里选综合得分最高的（行业龙头思路），"
                "再取全局前 N。这样选出来不会全挤在一个行业。",
        "weights": {"quality_roe": 1.2, "mom_60d": 1.0, "money_flow": 0.8,
                    "size_big": 0.6, "liquidity": 0.6},
        "per_industry": 1,
    },
}


def presets() -> list:
    return [{"key": k, "name": v["name"], "desc": v["desc"],
             "weights": {kk: vv for kk, vv in v["weights"].items()},
             "factor_names": {kk: SCREEN_FACTORS[kk][0] for kk in v["weights"] if kk in SCREEN_FACTORS}}
            for k, v in PRESETS.items()]


def factors_list() -> list:
    return [{"key": k, "name": v[0], "direction": "越大越好" if v[2] > 0 else "越小越好"}
            for k, v in SCREEN_FACTORS.items()]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(preset: str = "multi_factor", filters: dict | None = None, top_n: int = 20,
        weights: dict | None = None, neutralize_industry: bool = False,
        with_tech: bool = True, tech_pool: int = 3, force_snapshot: bool = False,
        with_fundamental: bool = True) -> dict:
    """跑一次全市场选股

    top_n       最终选几只
    tech_pool   精筛候选池 = top_n * tech_pool
    with_tech   是否对短名单拉 K 线做技术面精筛
    with_fundamental 是否对短名单补估值分位（PE/PB 近5年分位、股息率）。
                     没配令牌或取不到会自动跳过，不影响选股结果。
    """
    import time
    t0 = time.time()
    pre = PRESETS.get(preset)
    if pre is None:
        return {"ok": False, "message": "没有这个策略模板: %s" % preset}

    w = dict(weights) if weights else dict(pre["weights"])
    if not w:
        return {"ok": False, "message": "没有指定任何因子权重"}

    snap = snapshot(force=force_snapshot)
    rows = snap["rows"]
    if not rows:
        return {"ok": False, "message": "拿不到全市场行情，检查一下网络"}

    passed, fstats = apply_filters(rows, filters)
    if len(passed) < 10:
        return {"ok": False, "message": "硬条件太严了，只剩 %d 只，选不出东西。放宽一点试试。"
                                        % len(passed), "filter_stats": fstats}

    scores, detail = _score(passed, w, neutralize_industry=neutralize_industry)
    for i, r in enumerate(passed):
        r["_score"] = round(scores[i], 2)
        r["_detail"] = detail[i]
    passed.sort(key=lambda x: -x["_score"])

    pool_n = max(int(top_n), int(top_n) * max(1, int(tech_pool)))
    pool = passed[:max(pool_n, int(top_n))]
    if pre.get("per_industry"):
        # 行业轮动：每个行业先选出一只最好的，再全局排序
        best = {}
        for r in passed:
            ind = r["industry"]
            if ind not in best:
                best[ind] = r
        pool = list(best.values())
        pool.sort(key=lambda x: -x["_score"])
        pool = pool[:max(pool_n, int(top_n))]

    tech_note = ""
    if with_tech:
        cand = pool[:max(int(top_n) * max(1, int(tech_pool)), int(top_n))]
        tech_rows = []
        lock = threading.Lock()

        def work(r):
            m = _tech_metrics(r["symbol"], bars=90)
            pb = _pullback(r["symbol"]) if pre.get("tech") == "pullback" else {}
            with lock:
                tech_rows.append((r, m, pb))

        threads = []
        for r in cand:
            th = threading.Thread(target=work, args=(r,), daemon=True)
            threads.append(th)
            if len(threads) >= 8:
                for x in threads:
                    x.start()
                for x in threads:
                    x.join()
                threads = []
        for x in threads:
            x.start()
        for x in threads:
            x.join()

        by_sym = {r["symbol"]: (r, m, pb) for r, m, pb in tech_rows}
        refined = []
        for r in cand:
            item = by_sym.get(r["symbol"])
            if not item:
                continue
            rr, m, pb = item
            rr["tech"] = m
            rr["pullback"] = pb
            bonus = 0.0
            if m:
                # 技术面加分项（0~12 分），不推翻基本面排序
                bonus += (m.get("ma_bull", 0) / 100.0) * 4.0
                bonus += min(max((m.get("vol_expand", 1.0) - 1.0), -0.5), 1.5) * 2.0
                bonus -= min(max((m.get("vol_ann", 30.0) - 30.0) / 100.0, -0.3), 1.5) * 2.0
                if m.get("above_ma20"):
                    bonus += 2.0
            if pb:
                bonus += float(pb.get("score") or 0) * 0.04
                if pb.get("near_support"):
                    bonus += 2.0
            rr["_tech_bonus"] = round(bonus, 2)
            rr["_final"] = round(rr["_score"] + bonus, 2)
            refined.append(rr)
        refined.sort(key=lambda x: -x["_final"])
        pool = refined
        tech_note = "已对前 %d 只做过技术面精筛" % len(refined)

    picked = pool[:int(top_n)]

    # ---- 可选：基本面增强（估值分位）----
    # 只对短名单抓，不碰全市场（3000 只逐只查不现实）。取不到就原样排序。
    fnd_note, fnd_n = "", 0
    if with_fundamental and pool:
        fnd_n, fnd_note = enrich_fundamental(
            pool, limit=max(60, int(top_n) * 4), budget_sec=35.0)
        if fnd_n > 0:
            wv = 0.15
            try:
                from . import config as _cfg
                wv = float(_cfg.get("fundamental.screener_weight", 0.15) or 0.0)
            except Exception:  # noqa: BLE001
                wv = 0.15
            _blend_value(pool, wv)
            picked = pool[:int(top_n)]

    # 行业分布
    ind_count = {}
    for r in picked:
        ind_count[r["industry"]] = ind_count.get(r["industry"], 0) + 1

    warn = []
    if ind_count and max(ind_count.values()) >= max(3, len(picked) * 0.4):
        warn.append("选出来的股票有 %d 只集中在「%s」，行业过于集中，"
                    "建议减少该行业权重或用【行业轮动·龙头】模板。"
                    % (max(ind_count.values()), max(ind_count, key=ind_count.get)))
    if len(picked) < int(top_n):
        warn.append("只选出 %d 只（你要 %d 只），说明硬条件偏严。" % (len(picked), int(top_n)))
    # 幸存者偏差：选股池是「今天还在市的股票」，历史被淘汰的（退市/ST 摘牌）根本不在里面。
    # 这不是本平台的 bug，而是"用当前成分股回看历史"的通病，但用户必须知道。
    warn.append("提示：本结果基于「当前」全市场股票池筛选，不含已退市股票，存在幸存者偏差。"
                "用这个池子做历史回测，收益会偏乐观。")

    out = []
    for i, r in enumerate(picked, 1):
        top_f = sorted(r.get("_detail", {}).items(), key=lambda x: -x[1])[:3]
        out.append({
            "rank": i,
            "symbol": r["symbol"], "code": r["code"], "name": r["name"],
            "board": r["board"], "industry": r["industry"],
            "price": r["price"], "chg_pct": r["chg_pct"],
            "amount": r["amount"], "turnover": r["turnover"], "vol_ratio": r["vol_ratio"],
            "mktcap": r["mktcap"], "pe": _pe(r), "pb": r["pb"], "roe": r["roe"],
            "gross_margin": r["gross_margin"], "rev_yoy": r["rev_yoy"],
            "profit_yoy": r["profit_yoy"], "main_pct": r["main_pct"],
            "chg_60d": r["chg_60d"], "chg_ytd": r["chg_ytd"],
            "score": r["_score"], "tech_bonus": r.get("_tech_bonus"),
            "final": r.get("_final", r["_score"]),
            "why": [{"factor": k, "pct": round(v, 1)} for k, v in top_f],
            "tech": r.get("tech") or {},
            "pullback": r.get("pullback") or {},
            "fnd": r.get("_fnd") or {},
            "value_score": r.get("_value_score"),
        })

    return {
        "ok": True,
        "preset": preset, "preset_name": pre["name"],
        "picked": out,
        "weights": w,
        "weight_names": {k: SCREEN_FACTORS[k][0] for k in w if k in SCREEN_FACTORS},
        "filter_stats": fstats,
        "filter_used": {k: v for k, v in (filters or {}).items()},
        "universe": len(rows), "after_filter": len(passed),
        "industry_count": ind_count,
        "warnings": warn,
        "tech_note": tech_note,
        "fundamental_note": fnd_note,
        "fundamental_used": fnd_n,
        "elapsed": round(time.time() - t0, 2),
        "snapshot_at": snap["meta"].get("fetched_at"),
        "neutralize_industry": neutralize_industry,
    }
