# -*- coding: utf-8 -*-
"""基本面增强模块（可选，绝不进入回测主链路）

给平台补上「K 线以外」的数据：估值分位、申万三级行业、分红送配。

设计铁律（破坏任何一条都会让平台从"双击即用"变成"要配环境才能跑"）：
  1. 纯标准库，零第三方依赖（urllib + json + re）。
  2. 任何失败——没令牌、没网络、接口改格式、缓存文件损坏、磁盘只读——
     公开函数一律返回 None / 空值，**绝不向上抛异常**。
  3. 不参与因子计算与回测。只有被显式调用（选股增强、行业查询）时才联网。
  4. 结果落盘缓存到 data/fundamental/，带 TTL，断网时用旧缓存。
  5. 原子写盘（先写 .tmp 再 os.replace），断电也不会留下半截 JSON。

数据来源：腾讯 NeoData 自然语言金融搜索接口。
令牌来源优先级：config.fundamental.token > 环境变量 NEODATA_TOKEN > 本机技能缓存文件。
三者都没有时，模块静默降级为「只用磁盘缓存」。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from . import config

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
ENDPOINT = "https://copilot.tencent.com/agenttool/v1/neodata"
HTTP_TIMEOUT = 20.0          # 单次请求超时（秒）
MIN_REQ_INTERVAL = 0.25      # 两次请求最小间隔，避免猛打腾讯接口
RETRY_SLEEP = 0.6            # 网络类错误重试前的等待（秒）
CACHE_DIR = config.DATA_DIR / "fundamental"

# 各类数据的缓存有效期（秒）
TTL_VALUATION = 24 * 3600     # 估值：1 天
TTL_INDUSTRY = 30 * 24 * 3600  # 行业分类：30 天（几乎不变）
TTL_DIVIDEND = 7 * 24 * 3600   # 分红：7 天

# 缓存格式/解析逻辑版本。改了任何解析规则就 +1，旧缓存自动失效，
# 避免"代码修好了、缓存里还是旧脏数据"。
CACHE_VER = 2

_SYM_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.I)
_SEP_CELL = re.compile(r"^:?-{2,}:?$")

# ---------------------------------------------------------------------------
# 内部状态
# ---------------------------------------------------------------------------
_last_req_ts = [0.0]
_req_lock = threading.Lock()
_sym_locks: dict = {}
_sym_locks_guard = threading.Lock()
_last_error = [""]


class _AuthError(Exception):
    """令牌失效（401/403/40101）。单独一类，因为重试没有意义。"""


def _set_error(msg: str) -> None:
    _last_error[0] = str(msg or "")[:300]


def last_error() -> str:
    """最近一次失败原因，给状态页展示用（成功时清空）"""
    return _last_error[0]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def normalize(symbol) -> str | None:
    """校验并标准化股票代码；非法返回 None（不抛异常）"""
    try:
        s = str(symbol or "").strip().upper()
        return s if _SYM_RE.match(s) else None
    except Exception:  # noqa: BLE001
        return None


def _num(v):
    """把各种脏值转成 float，转不了返回 None

    需要处理：'--'、'未开盘'、''、'1,234.5'、'3.91%'、'1.664959e12'、None、NaN
    """
    if v is None:
        return None
    try:
        s = str(v).strip().replace(",", "").replace("%", "").replace("，", "")
    except Exception:  # noqa: BLE001
        return None
    if not s or s in ("--", "-", "—", "未开盘", "nan", "NaN", "None", "null"):
        return None
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    # 过滤 inf / nan
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _split_row(line: str) -> list:
    """拆一行 markdown 表格，去首尾竖线"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_sep_row(cells: list) -> bool:
    """判断是否为表头下的分隔行 :---"""
    if not cells:
        return False
    hit = 0
    for c in cells:
        if _SEP_CELL.match(c.strip()):
            hit += 1
    return hit >= max(1, len(cells) // 2)


def _tables(text: str) -> list:
    """抽出文本里所有 markdown 表格，返回 [(headers, rows), ...]

    rows 里已剔除分隔行。格式不合法时返回空列表，不抛异常。
    """
    out = []
    try:
        lines = str(text or "").splitlines()
    except Exception:  # noqa: BLE001
        return out
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i].strip()
        if not ln.startswith("|"):
            i += 1
            continue
        block = []
        while i < n and lines[i].strip().startswith("|"):
            block.append(lines[i].strip())
            i += 1
        if len(block) < 2:
            continue
        cells_block = [_split_row(b) for b in block]
        headers = cells_block[0]
        rows = [c for c in cells_block[1:] if not _is_sep_row(c)]
        if headers and rows:
            out.append((headers, rows))
    return out


def _row_dict(headers: list, row: list) -> dict:
    """表格行按表头转字典；长度不齐时按最短对齐，缺的补 None"""
    d = {}
    for idx, h in enumerate(headers):
        key = (h or "").strip()
        if not key:
            continue
        d[key] = row[idx] if idx < len(row) else None
    return d


def _pick(d: dict, *names):
    """按候选表头名取值（不同接口的表头别名），返回 (是否命中, 值)"""
    for nm in names:
        if nm in d:
            return True, d[nm]
    # 退一步：包含匹配
    for nm in names:
        for k in d:
            if nm in k:
                return True, d[k]
    return False, None


# ---------------------------------------------------------------------------
# 令牌
# ---------------------------------------------------------------------------
def _token() -> str | None:
    """按优先级取令牌；都没有返回 None"""
    try:
        t = config.get("fundamental.token") or ""
        t = str(t).strip()
        if t:
            return t
    except Exception:  # noqa: BLE001
        pass
    try:
        t = (os.environ.get("NEODATA_TOKEN") or "").strip()
        if t:
            return t
    except Exception:  # noqa: BLE001
        pass
    # 同机 WorkBuddy 技能缓存：~/.workbuddy/skills/.neodata_token
    try:
        p = os.path.join(
            os.path.expanduser("~"), ".workbuddy", "skills", ".neodata_token"
        )
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if not raw:
                return None
            try:
                obj = json.loads(raw)
                tok = str(obj.get("token") or "").strip()
                saved = float(obj.get("saved_at") or 0)
                # 超过 12 小时视为过期（服务端有效期）
                if tok and (time.time() - saved) < 12 * 3600:
                    return tok
                _set_error("令牌已过期（超过 12 小时），请重新获取")
                return None
            except (json.JSONDecodeError, TypeError, ValueError):
                _set_error("令牌缓存文件格式异常")
                return None
    except Exception:  # noqa: BLE001
        pass
    return None


def token_available() -> bool:
    return bool(_token())


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _throttle() -> None:
    """全局节流：两次请求至少间隔 MIN_REQ_INTERVAL 秒"""
    with _req_lock:
        now = time.time()
        wait = MIN_REQ_INTERVAL - (now - _last_req_ts[0])
        if wait > 0:
            time.sleep(wait)
        _last_req_ts[0] = time.time()


def _post(query: str, data_type: str = "api") -> dict | None:
    """调 NeoData，成功返回响应 dict，失败返回 None（永不抛异常）

    鉴权失败抛 _AuthError（内部消化用），其它错误一律返回 None。
    """
    tok = _token()
    if not tok:
        _set_error("没有可用令牌")
        return None

    payload = {
        "query": str(query or "").strip(),
        "channel": "neodata",
        "sub_channel": "workbuddy",
    }
    if data_type and data_type != "all":
        payload["data_type"] = data_type

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer %s" % tok,
        },
        method="POST",
    )

    last_exc = None
    for attempt in range(2):  # 网络类错误重试 1 次
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                _set_error("接口返回的不是合法 JSON")
                return None
            code = str(data.get("code") or "")
            if code in ("40101", "401", "403"):
                raise _AuthError(code)
            if not data.get("suc") and code not in ("200", ""):
                _set_error("接口返回失败 code=%s msg=%s" % (code, data.get("msg")))
                return None
            _set_error("")
            return data
        except _AuthError as e:
            _set_error("令牌无效或已过期（code=%s）" % e)
            return None
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                _set_error("令牌无效或已过期（HTTP %d）" % e.code)
                return None
            last_exc = e
            if 400 <= e.code < 500 and e.code != 429:
                # 参数类错误，重试没用
                _set_error("请求被拒绝（HTTP %d）" % e.code)
                return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
        except Exception as e:  # noqa: BLE001
            last_exc = e
            break
        if attempt == 0:
            time.sleep(RETRY_SLEEP)

    _set_error("网络请求失败：%s" % (str(last_exc)[:200] if last_exc else "未知"))
    return None


def _api_blocks(data: dict) -> list:
    """从响应里安全取出 apiRecall 块列表"""
    try:
        recall = ((data.get("data") or {}).get("apiData") or {}).get("apiRecall") or []
        return [b for b in recall if isinstance(b, dict)]
    except Exception:  # noqa: BLE001
        return []


def _joined_content(blocks: list) -> str:
    """把所有块的 content 拼起来（解析时一起扫，避免漏表）"""
    parts = []
    for b in blocks:
        try:
            c = b.get("content")
            if c:
                parts.append(str(c))
        except Exception:  # noqa: BLE001
            continue
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 解析：估值
# ---------------------------------------------------------------------------
def _parse_valuation(text: str) -> dict | None:
    """从接口文本里解析估值：PE(TTM)、PB、5年分位、股息率、市值"""
    if not text:
        return None
    for headers, rows in _tables(text):
        hstr = "|".join(headers)
        # 必须同时含 PE 和 市值，才认为是估值表
        if "PE" not in hstr.upper() and "市盈率" not in hstr:
            continue
        for row in rows:
            d = _row_dict(headers, row)
            pe_ok, pe = _pick(d, "PE(TTM)", "市盈率(TTM)", "PE_TTM")
            cap_ok, cap = _pick(d, "总市值(元)", "总市值")
            pe_v = _num(pe)
            # 「未开盘」行没有 PE，跳过
            if pe_v is None and not cap_ok:
                continue
            out = {
                "pe_ttm": pe_v,
                "pe_pct_5y": _num(_pick(d, "PE近5年分位(%)", "PE近5年分位", "市盈率近5年分位")[1]),
                "pb": _num(_pick(d, "PB", "PB(LF)", "市净率")[1]),
                "pb_pct_5y": _num(_pick(d, "PB近5年分位(%)", "PB近5年分位", "市净率近5年分位")[1]),
                "dividend_yield": _num(_pick(d, "股息率(TTM)", "股息率")[1]),
                "mktcap": _num(cap),
                "float_cap": _num(_pick(d, "流通市值(元)", "流通市值")[1]),
                "date": str(_pick(d, "日期", "数据日期")[1] or "")[:10],
            }
            # 至少有一个有效数字才算成功
            if any(v is not None for k, v in out.items() if k != "date"):
                return out
    return None


# ---------------------------------------------------------------------------
# 解析：行业
# ---------------------------------------------------------------------------
def _parse_industry(text: str) -> dict | None:
    """从接口文本里解析申万行业三级分类 + 概念标签 + 主营业务

    注意：接口把「申万行业 / 所属概念 / 所属地域」写在同一行，用「；」分隔，
    所以正则必须按分号截断，否则会把后面的字段一起吞进行业名里。
    """
    if not text:
        return None
    out = {"sw_l1": None, "sw_l2": None, "sw_l3": None,
           "concepts": [], "main_business": None, "market": None}
    hit = False

    m = re.search(r"申万行业[：:]\s*([^；;\n]+)", text)
    if m:
        raw = m.group(1)
        # 去掉括号里的行业代码，如「食品饮料（01801120.PT）」
        raw = re.sub(r"（[^）]*）|\([^)]*\)", "", raw)
        parts = [p.strip() for p in re.split(r"[>＞]", raw) if p.strip()]
        if parts:
            out["sw_l1"] = parts[0]
            out["sw_l2"] = parts[1] if len(parts) > 1 else None
            out["sw_l3"] = parts[2] if len(parts) > 2 else None
            hit = True

    m = re.search(r"所属概念[：:]\s*([^；;\n]+)", text)
    if m:
        cs = [c.strip() for c in re.split(r"[、,，]", m.group(1)) if c.strip()]
        out["concepts"] = cs[:40]
        hit = True

    m = re.search(r"主营业务[：:]\s*([^；;\n]+)", text)
    if m:
        out["main_business"] = m.group(1).strip()[:300]
        hit = True

    m = re.search(r"市场[：:]\s*([^；;\n]+)", text)
    if m:
        out["market"] = m.group(1).strip()[:20]

    return out if hit else None


# ---------------------------------------------------------------------------
# 解析：分红
# ---------------------------------------------------------------------------
def _parse_dividend(text: str) -> list | None:
    """解析分红送配明细，返回 [{announce, ex_date, cash_per10, plan}, ...]"""
    if not text:
        return None
    lines = []
    for headers, rows in _tables(text):
        hstr = "|".join(headers)
        if "除权除息日" not in hstr and "每10股派息" not in hstr:
            continue
        for row in rows:
            d = _row_dict(headers, row)
            ex = str(_pick(d, "除权除息日")[1] or "").strip()
            if not ex or ex in ("--", "-", "—", "None"):
                continue
            if not re.match(r"^\d{4}[-/]?\d{1,2}[-/]?\d{1,2}$", ex):
                continue
            cash = _num(_pick(d, "每10股派息", "每10股派息(元)")[1])
            lines.append({
                "announce": str(_pick(d, "实施公告日")[1] or "")[:10],
                "ex_date": ex.replace("/", "-"),
                "cash_per10": cash,
                "plan": str(_pick(d, "分红方案")[1] or "")[:80],
            })
    if not lines:
        return None
    # 按除权日倒序、去重
    seen, out = set(), []
    for it in sorted(lines, key=lambda x: x["ex_date"], reverse=True):
        if it["ex_date"] in seen:
            continue
        seen.add(it["ex_date"])
        out.append(it)
    return out[:40]


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def _cache_path(symbol: str):
    return CACHE_DIR / ("%s.json" % symbol.replace(".", "_"))


def _read_cache(symbol: str) -> dict:
    """读缓存，任何异常返回空 dict；版本不符也当作没有"""
    try:
        p = _cache_path(symbol)
        if not p.is_file():
            return {}
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            return {}
        try:
            if int(obj.get("_ver") or 0) != CACHE_VER:
                return {}  # 旧版本缓存，解析规则可能已变，直接丢弃
        except (TypeError, ValueError):
            return {}
        return obj
    except Exception:  # noqa: BLE001
        return {}


def _write_cache(symbol: str, obj: dict) -> bool:
    """原子写缓存；失败返回 False，不抛异常"""
    tmp = None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _cache_path(symbol)
        tmp = p.with_suffix(".tmp")
        obj["_ver"] = CACHE_VER
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(str(tmp), str(p))
        return True
    except Exception:  # noqa: BLE001
        try:
            if tmp is not None and tmp.exists():
                tmp.unlink()
        except Exception:  # noqa: BLE001
            pass
        return False


def _fresh(entry, ttl: int) -> bool:
    try:
        ts = float(entry.get("ts") or 0)
        return (time.time() - ts) < ttl and isinstance(entry.get("data"), (dict, list))
    except Exception:  # noqa: BLE001
        return False


def _sym_lock(symbol: str):
    """同一只股票并发抓取时互斥，避免重复请求"""
    with _sym_locks_guard:
        if len(_sym_locks) > 800:
            _sym_locks.clear()
        lk = _sym_locks.get(symbol)
        if lk is None:
            lk = threading.Lock()
            _sym_locks[symbol] = lk
        return lk


# ---------------------------------------------------------------------------
# 对外 API（全部不抛异常）
# ---------------------------------------------------------------------------
def get_valuation(symbol, force: bool = False) -> dict | None:
    """估值：PE(TTM)/PB/近5年分位/股息率/市值。失败返回 None"""
    sym = normalize(symbol)
    if not sym:
        return None
    cache = _read_cache(sym)
    ent = cache.get("valuation")
    if not force and _fresh(ent, TTL_VALUATION):
        return ent.get("data")
    with _sym_lock(sym):
        cache = _read_cache(sym)  # 双检
        ent = cache.get("valuation")
        if not force and _fresh(ent, TTL_VALUATION):
            return ent.get("data")
        data = _post("%s 最新PE(TTM)、PB、PE近5年分位、PB近5年分位、股息率、总市值" % sym)
        if data is None:
            return ent.get("data") if isinstance(ent, dict) else None
        parsed = _parse_valuation(_joined_content(_api_blocks(data)))
        if not parsed:
            # 解析失败：保留旧缓存，别把好数据冲掉
            return ent.get("data") if isinstance(ent, dict) else None
        cache["symbol"] = sym
        cache["valuation"] = {"ts": time.time(), "data": parsed}
        _write_cache(sym, cache)
        return parsed


def get_industry(symbol, force: bool = False) -> dict | None:
    """申万三级行业 + 概念标签 + 主营业务。失败返回 None"""
    sym = normalize(symbol)
    if not sym:
        return None
    cache = _read_cache(sym)
    ent = cache.get("industry")
    if not force and _fresh(ent, TTL_INDUSTRY):
        return ent.get("data")
    with _sym_lock(sym):
        cache = _read_cache(sym)
        ent = cache.get("industry")
        if not force and _fresh(ent, TTL_INDUSTRY):
            return ent.get("data")
        data = _post("%s 所属申万行业分类、概念标签、主营业务" % sym)
        if data is None:
            return ent.get("data") if isinstance(ent, dict) else None
        parsed = _parse_industry(_joined_content(_api_blocks(data)))
        if not parsed:
            return ent.get("data") if isinstance(ent, dict) else None
        cache["symbol"] = sym
        cache["industry"] = {"ts": time.time(), "data": parsed}
        _write_cache(sym, cache)
        return parsed


def get_dividend(symbol, force: bool = False) -> list | None:
    """分红送配明细（含除权除息日）。失败返回 None"""
    sym = normalize(symbol)
    if not sym:
        return None
    cache = _read_cache(sym)
    ent = cache.get("dividend")
    if not force and _fresh(ent, TTL_DIVIDEND):
        return ent.get("data")
    with _sym_lock(sym):
        cache = _read_cache(sym)
        ent = cache.get("dividend")
        if not force and _fresh(ent, TTL_DIVIDEND):
            return ent.get("data")
        data = _post("%s 最近三年分红送配方案，每10股派息金额、除权除息日" % sym)
        if data is None:
            return ent.get("data") if isinstance(ent, list) else None
        parsed = _parse_dividend(_joined_content(_api_blocks(data)))
        if not parsed:
            return ent.get("data") if isinstance(ent, list) else None
        cache["symbol"] = sym
        cache["dividend"] = {"ts": time.time(), "data": parsed}
        _write_cache(sym, cache)
        return parsed


def get_all(symbol, force: bool = False) -> dict:
    """一次性返回三块数据（各自独立缓存/降级），用于详情展示"""
    return {
        "symbol": normalize(symbol),
        "valuation": get_valuation(symbol, force),
        "industry": get_industry(symbol, force),
        "dividend": get_dividend(symbol, force),
    }


def industry_name(symbol) -> str:
    """给 datasource.industry_of 用的精确行业名：优先三级，退二级，再退一级

    取不到返回 ""（调用方回退到原有粗分类）。
    注意：本函数可能联网。回测/归因等高频路径请用 industry_name_cached。
    """
    info = get_industry(symbol)
    if not info:
        return ""
    return (info.get("sw_l3") or info.get("sw_l2") or info.get("sw_l1") or "").strip()


def industry_name_cached(symbol) -> str:
    """只读缓存、绝不联网的行业名。

    专给回测收益归因这类会被高频调用的路径用——那里绝不能因为查行业去发网络请求。
    """
    sym = normalize(symbol)
    if not sym:
        return ""
    try:
        cache = _read_cache(sym)
        ent = cache.get("industry")
        if not _fresh(ent, TTL_INDUSTRY):
            return ""
        d = ent.get("data") or {}
        if not isinstance(d, dict):
            return ""
        return (d.get("sw_l3") or d.get("sw_l2") or d.get("sw_l1") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def status() -> dict:
    """状态页：令牌是否可用、缓存了多少只、最近错误"""
    cached = 0
    try:
        if CACHE_DIR.is_dir():
            cached = len([f for f in CACHE_DIR.iterdir() if f.suffix == ".json"])
    except Exception:  # noqa: BLE001
        cached = 0
    return {
        "enabled": True,
        "token_available": token_available(),
        "cache_dir": str(CACHE_DIR),
        "cached_symbols": cached,
        "endpoint": ENDPOINT,
        "ttl": {"valuation_hours": TTL_VALUATION // 3600,
                "industry_days": TTL_INDUSTRY // 86400,
                "dividend_days": TTL_DIVIDEND // 86400},
        "last_error": last_error(),
    }


def clear_cache() -> int:
    """清空缓存，返回删除的文件数；失败返回 0"""
    n = 0
    try:
        if not CACHE_DIR.is_dir():
            return 0
        for f in list(CACHE_DIR.iterdir()):
            if f.suffix in (".json", ".tmp"):
                try:
                    f.unlink()
                    n += 1
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        return n
    return n
