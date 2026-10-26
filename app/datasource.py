# -*- coding: utf-8 -*-
"""行情数据源

主源：东方财富（实时行情 / 历史K线 / 全市场股票列表）
备源：腾讯财经、新浪财经（实时行情容错）

全部用 Python 标准库实现，不需要装任何东西，也不需要 API Key。
"""
from __future__ import annotations

import gzip
import http.client
import io
import json
import os
import re
import threading
import time
import datetime as dt
import urllib.parse
import urllib.request

from . import config

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
EM_HEADERS = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/", "Accept": "*/*"}
TX_HEADERS = {"User-Agent": UA, "Referer": "https://gu.qq.com/"}
SINA_HEADERS = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}

STOCK_LIST_FILE = config.DATA_DIR / "stock_list.json"
KLINE_CACHE_DIR = config.DATA_DIR / "kline_cache"

_mem_cache: dict = {}
_cache_lock = threading.RLock()
MEM_CACHE_MAX = 3000          # 内存缓存条目上限，超了按时间淘汰最旧的 20%

# ---------------------------------------------------------------------------
# HTTP 层
#
# 为什么不用 urllib.request.open 就完事：
#   它每次请求都新建一条 TCP+TLS 连接。东财/腾讯/新浪全是 HTTPS，
#   一次 TLS 握手 100~300ms，扫一个股票池几百次请求就是几十秒纯浪费，
#   而且握手风暴本身就会触发对方的限流。
#
# 这里改成：每个线程、每个站点复用一条连接（HTTP/1.1 keep-alive）。
# 同时按站点做「最小请求间隔 + 并发上限」，别让 4 个线程同时猛打一个域名。
# 连接被服务器踹掉时自动丢掉重建重试一次；直连彻底不通时回退到支持系统代理的 urllib。
# ---------------------------------------------------------------------------
_tls = threading.local()          # 每线程自己的连接池
_host_meta: dict = {}             # host -> {"sem": Semaphore, "last": 上次请求时间}
_meta_lock = threading.Lock()

MIN_HOST_INTERVAL = 0.10          # 同一站点两次请求最小间隔（秒）
MAX_HOST_CONCURRENCY = 4          # 同一站点同时最多几个请求
CONN_IDLE_MAX = 6.0               # 复用的连接空闲超过这个秒数就丢弃重建（秒）

# 说明：东财这类站点会在服务端静默关闭空闲的 keep-alive 连接（约 5~10 秒），
# 客户端不知道，下一次请求发到这条「已死但本地还认为活着」的连接上，
# 对端直接 RST，Python 抛的就是 http.client.RemoteDisconnected。
# 所以复用前必须检查空闲时长，超过 CONN_IDLE_MAX 就主动重建，别去撞这个必死的坑。


def _host_state(host: str) -> dict:
    with _meta_lock:
        st = _host_meta.get(host)
        if st is None:
            st = {"sem": threading.Semaphore(MAX_HOST_CONCURRENCY), "last": 0.0}
            _host_meta[host] = st
        return st


def _throttle_host(host: str) -> None:
    """同一站点两次请求之间至少隔 MIN_HOST_INTERVAL，避免把自己打成限流"""
    st = _host_state(host)
    with _meta_lock:
        now = time.time()
        wait = MIN_HOST_INTERVAL - (now - st["last"])
        st["last"] = max(now, st["last"] + MIN_HOST_INTERVAL)
    if wait > 0:
        time.sleep(wait)


def _conn_pool() -> dict:
    pool = getattr(_tls, "conns", None)
    if pool is None:
        pool = {}
        _tls.conns = pool
    return pool


def _conn_get(scheme: str, host: str, timeout: float):
    pool = _conn_pool()
    key = (scheme, host)
    item = pool.get(key)
    if item is not None:
        conn, idle_since = item
        # 空闲太久的连接大概率已被对端关掉，复用必抛 RemoteDisconnected，直接重建
        if (time.time() - idle_since) > CONN_IDLE_MAX:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            pool.pop(key, None)
            item = None
    if item is not None:
        return item[0], True        # 复用的是可能已失效的旧连接
    if scheme == "https":
        conn = http.client.HTTPSConnection(host, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(host, timeout=timeout)
    pool[key] = (conn, time.time())
    return conn, False


def _conn_touch(scheme: str, host: str, conn) -> None:
    """标记连接刚被用过，避免空闲超时判断失真"""
    _conn_pool()[(scheme, host)] = (conn, time.time())


def _conn_drop(scheme: str, host: str) -> None:
    item = _conn_pool().pop((scheme, host), None)
    conn = item[0] if isinstance(item, tuple) else item
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _fetch_keepalive(url: str, headers: dict | None, timeout: float) -> bytes:
    """走 keep-alive 复用连接。只在「连接已失效」时抛异常，HTTP 错误照常抛 HTTPError。"""
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    host = parts.netloc
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    if scheme not in ("http", "https"):
        raise DataError("不支持的协议: %s" % scheme)
    hdrs = dict(headers or {"User-Agent": UA})

    # 只有「复用了一条旧连接」才值得重试一次：旧连接可能已被服务器关掉，
    # 丢掉重建通常立刻就好。而全新连接都失败，说明源本身不通，重试纯属浪费时间。
    key = (scheme, host)
    reused = _conn_pool().get(key) is not None
    last_err = None
    for attempt in range(2 if reused else 1):
        was_reused = False
        try:
            conn, was_reused = _conn_get(scheme, host, timeout)
            conn.request("GET", path, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()       # 必须读完，否则连接不能复用
            if resp.status >= 400:
                raise urllib.error.HTTPError(
                    url, resp.status, resp.reason, resp.headers, None)
            enc = (resp.getheader("Content-Encoding") or "").lower()
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            _conn_touch(scheme, host, conn)
            return raw
        except urllib.error.HTTPError:
            _conn_drop(scheme, host)    # 4xx/5xx 后连接状态不可靠，丢掉
            raise
        except (http.client.HTTPException, OSError) as e:
            last_err = e
            _conn_drop(scheme, host)
            if not was_reused:
                raise
            if attempt == 0:
                time.sleep(0.15)        # 给对端一点时间，别紧接着又撞上
    raise last_err or DataError("连接失败")


def _fetch_urllib(url: str, headers: dict | None, timeout: float, use_proxy: bool) -> bytes:
    """回退路径：urllib（支持系统代理，兼容需要代理上网的环境）"""
    if not _openers:
        _openers.append(urllib.request.build_opener())
        _openers.append(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    op = _openers[0] if use_proxy else _openers[1]
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with op.open(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw


_openers = []
# 记录上次哪种连接方式成功过（仅回退路径用）。默认直连优先。
_opener_ok = {"idx": 1}


class DataError(Exception):
    pass


def err_brief(e) -> str:
    """把异常压成一句人话，去掉又长又没用的 URL。

    日志和界面里出现 "网络请求失败: https://push2his.eastmoney.com/api/qt/stock/
    kline/get?secid=1.600519&fields1=f1..." 这种，对用户没有任何帮助，
    只会把真正的原因（HTTP 501 / 连接被拒 / 超时）淹没掉。
    """
    if e is None:
        return "所有数据源都在冷却中"
    s = str(e)
    if "网络请求失败" in s:
        return "网络请求失败 " + s[s.rfind("("):] if s.rstrip().endswith(")") and "(" in s \
            else "网络请求失败"
    if "返回不是合法 JSON" in s:
        return "数据源返回了异常内容"
    return s[:60]


# 这些状态码说明服务器明确拒绝（限流/接口下线），重试只会更慢
_NO_RETRY_CODES = {400, 401, 403, 404, 405, 429, 500, 501, 502, 503, 504}

# 可重试的「连接被掐」类异常。
# 必须显式列出 ConnectionAbortedError：Windows 上对端 RST 抛的是它（10053），
# 既不是 RemoteDisconnected 也不是 ConnectionResetError，不列出来就会漏掉。
_RETRY_CONN_ERRORS = (http.client.RemoteDisconnected, ConnectionResetError,
                      ConnectionAbortedError, BrokenPipeError)


def http_get(url: str, headers: dict | None = None, timeout: float = 5.0, retries: int = 2) -> bytes:
    """带重试、keep-alive 复用、按站点限流的 GET

    分层策略（每一层都是为了让「死源」几秒内就放弃，而不是拖几十秒）：
      1. keep-alive 直连（快，绝大多数请求走这里）
      2. RemoteDisconnected（服务器收下请求但不回包就掐断）-> 丢连接重建 + 退避重试
      3. 连接被拒 / 被重置 -> 立刻失败，不重试、不换路径（换也没用）
      4. 超时类异常 -> 回退 urllib 直连试一次（兼容必须直连的环境）
      5. 4xx/5xx（限流、502 网关）-> 不浪费轮次，直接放弃换源

    ⚠️ 关于 RemoteDisconnected 的重要修正：
    http.client.RemoteDisconnected 继承自 ConnectionResetError，以前被
    `except (ConnectionRefusedError, ConnectionResetError): break` 一起吞掉，
    于是「服务器瞬时限流掐一次连接」被误判成「这个源彻底死了」，一次都不重试。
    东财对列表类接口（clist / ulist）的限流恰恰就表现为 RemoteDisconnected，
    结果就是接口长期报「网络请求失败」，偶尔又能刷出来——极不稳定。
    现在单独拎出来按「可重试」处理，其余情况保持原来的快速失败。
    """
    last = None
    host = urllib.parse.urlsplit(url).netloc
    tries = max(1, int(retries))
    fell_back = False        # 超时后的 urllib 回退最多只做一次，否则超时会成倍叠加
    for attempt in range(tries):
        try:
            _throttle_host(host)
            with _host_state(host)["sem"]:
                return _fetch_keepalive(url, headers, timeout)
        except urllib.error.HTTPError as e:
            last = e
            # 服务器明确拒绝：重试和换路径都不会成功
            if int(getattr(e, "code", 0) or 0) in _NO_RETRY_CODES:
                break
        except ConnectionRefusedError as e:
            # 端口都不通（服务没起 / 被防火墙拒），重试没有任何意义，快速失败
            last = e
            break
        except _RETRY_CONN_ERRORS as e:
            # 服务器「收下请求但不回响应就关闭」，按可重试处理。
            #
            # 注意这不是多余的防御，三个异常在不同平台上都会出现：
            #   RemoteDisconnected   Linux/macOS 上对端 FIN 后读到空响应
            #   ConnectionAborted   Windows 上对端 RST（WSAECONNABORTED 10053）—— 本项目跑在 Windows，
            #                       实测东财掐连接抛的就是它，以前完全没被覆盖
            #   ConnectionReset     Windows 上 WSAECONNRESET 10054
            # 以前这些和 ConnectionRefusedError 一起被 break 掉，一次都不重试，
            # 于是「服务器偶尔抖一下」被误判成「这个源彻底死了」。
            #
            # 仍然不回退 urllib 代理：代理不通会白等一个完整超时，再乘重试次数，
            # 一只股票就能卡几十秒。只重建直连、退避后再试。
            last = e
            _conn_drop(*_host_key(url))
            if attempt + 1 >= tries:
                break
            time.sleep(0.35 * (2 ** attempt))   # 0.35s / 0.7s ... 指数退避
            continue
        except Exception as e:  # noqa: BLE001  （主要是超时）
            last = e
            # 超时可能是环境必须走代理，回退 urllib 直连再试**一次**。
            # 以前每次 attempt 都回退一次，3 次重试 = 6 个超时窗口，
            # 一个连不上的地址能卡 15 秒——用户看到的就是「点了没反应」。
            if fell_back:
                break
            fell_back = True
            try:
                _throttle_host(host)
                return _fetch_urllib(url, headers, timeout, False)
            except (ConnectionRefusedError, ConnectionResetError) as e2:
                last = e2
                break
            except Exception as e2:  # noqa: BLE001
                last = e2
                break
        time.sleep(0.4 * (2 ** attempt))
    if isinstance(last, urllib.error.HTTPError):
        err = "HTTP %s" % getattr(last, "code", "?")
    else:
        err = type(last).__name__ if last is not None else "unknown"
    raise DataError("网络请求失败: %s (%s)" % (url[:110], err))


def _host_key(url: str):
    parts = urllib.parse.urlsplit(url)
    return ((parts.scheme or "https").lower(), parts.netloc)


def http_get_text(url: str, headers: dict | None = None, encoding: str = "utf-8",
                  timeout: float = 8.0, retries: int = 1) -> str:
    # retries 默认给 1：K线有东财/腾讯/新浪三源容错，单个源内部再重试 3 次纯属浪费时间
    return http_get(url, headers, timeout, retries).decode(encoding, "replace")


def http_get_json(url: str, headers: dict | None = None, timeout: float = 8.0, retries: int = 1) -> dict:
    txt = http_get_text(url, headers or EM_HEADERS, "utf-8", timeout, retries)
    try:
        return json.loads(txt)
    except Exception as e:
        raise DataError("返回不是合法 JSON: %s" % txt[:160]) from e


def cache_get(key: str, ttl: float):
    with _cache_lock:
        item = _mem_cache.get(key)
        if item and time.time() - item[0] < ttl:
            return item[1]
    return None


def cache_put(key: str, value):
    with _cache_lock:
        _mem_cache[key] = (time.time(), value)
        # 内存缓存加个上限：跑一整天，K线/行情/股票列表会不断往里塞，
        # 不设上限的话内存只涨不降（几千只股票 × 几百根K线很容易上G）。
        if len(_mem_cache) > MEM_CACHE_MAX:
            items = sorted(_mem_cache.items(), key=lambda kv: kv[1][0])
            for k, _ in items[:max(1, len(items) // 5)]:
                _mem_cache.pop(k, None)
    return value


# ---------------------------------------------------------------------------
# K线磁盘缓存：把拉到过的历史K线存到本地文件，避免反复请求东财被限流。
# 这是「东财断连」问题的治本手段——历史日K当天不会变，缓存一次能用一整天，
# 前端每几秒刷新、引擎轮询、选股器扫全市场时，绝大部分请求都命中本地缓存。
# ---------------------------------------------------------------------------
def _kline_disk_path(sym: str, period: str, adjust: int, limit: int):
    try:
        KLINE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    # v2：加了除权修正后，旧缓存里可能存着「未修正的不复权数据」，
    # 换个版本号让它们自然失效，避免读到脏数据还以为已经复权了。
    fn = "%s_%s_%d_%d_v2.json" % (sym.replace(".", "_"), period, int(adjust), int(limit or 0))
    return KLINE_CACHE_DIR / fn


def _kline_cache_ttl(data: list) -> float:
    """按数据新鲜度决定磁盘缓存时长（秒）"""
    if not data:
        return 0.0
    try:
        last_date = str(data[-1].get("date"))[:10]
        if last_date < dt.date.today().isoformat():
            # 最后一根K线不是今天 → 稳定历史数据，当天不会再变，缓存 6 小时
            return 6 * 3600.0
        # 最后一根是今天（盘中）→ 短缓存 2 分钟
        return 120.0
    except Exception:
        return 60.0


def _kline_disk_get(sym: str, period: str, adjust: int, limit: int) -> list | None:
    try:
        p = _kline_disk_path(sym, period, adjust, limit)
        if not p or not p.exists():
            return None
        blob = json.loads(p.read_text(encoding="utf-8"))
        data = blob.get("data")
        if not data:
            return None
        age = time.time() - p.stat().st_mtime
        if age > _kline_cache_ttl(data):
            return None
        return data
    except Exception:
        return None


def _kline_disk_put(sym: str, period: str, adjust: int, limit: int, data: list) -> None:
    try:
        p = _kline_disk_path(sym, period, adjust, limit)
        if not p:
            return
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "data": data}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 股票代码处理
# ---------------------------------------------------------------------------
INDEX_TABLE = {
    "000001.SH": ("上证指数", 1, "000001"),
    "399001.SZ": ("深证成指", 0, "399001"),
    "399006.SZ": ("创业板指", 0, "399006"),
    "000300.SH": ("沪深300", 1, "000300"),
    "000905.SH": ("中证500", 1, "000905"),
    "000688.SH": ("科创50", 1, "000688"),
    "899050.BJ": ("北证50", 0, "899050"),
    "399005.SZ": ("中小100", 0, "399005"),
    "000016.SH": ("上证50", 1, "000016"),
}

MAIN_INDEX = ["000001.SH", "399001.SZ", "399006.SZ", "000300.SH", "000688.SH", "399005.SZ"]


def normalize(code: str) -> str:
    """把各种写法统一成 '600519.SH' 这种格式"""
    if not code:
        return ""
    s = str(code).strip().upper().replace(" ", "")
    # 已经是 000001.SH 形式
    m = re.match(r"^(\d{6})\.(SH|SZ|BJ)$", s)
    if m:
        return s
    # SH600519 / 600519SH / sh.600519
    m = re.match(r"^(SH|SZ|BJ)[.\-]?(\d{6})$", s)
    if m:
        return "%s.%s" % (m.group(2), m.group(1))
    m = re.match(r"^(\d{6})[.\-]?(SH|SZ|BJ)$", s)
    if m:
        return "%s.%s" % (m.group(1), m.group(2))
    digits = re.sub(r"\D", "", s)
    if len(digits) != 6:
        raise DataError("看不懂的股票代码: %s" % code)
    return "%s.%s" % (digits, guess_market(digits))


def guess_market(d6: str) -> str:
    if d6.startswith(("60", "68", "51", "58", "56", "50", "11", "90", "9")):
        return "SH"
    if d6.startswith(("00", "30", "12", "15", "16", "18", "20", "39")):
        return "SZ"
    if d6.startswith(("4", "8", "92")):
        return "BJ"
    return "SH" if d6.startswith("6") else "SZ"


def secid(code: str) -> str:
    """转成东方财富的 secid，如 1.600519"""
    n = normalize(code)
    d6, mk = n.split(".")
    return ("1." if mk == "SH" else "0.") + d6


def board_of(code: str) -> str:
    """判断板块：main=主板, gem=创业板, star=科创板, bj=北交所"""
    n = normalize(code)
    d6, mk = n.split(".")
    if mk == "BJ":
        return "bj"
    if d6.startswith("300") or d6.startswith("301"):
        return "gem"
    if d6.startswith("688") or d6.startswith("689"):
        return "star"
    return "main"


def limit_pct(code: str, name: str = "") -> float:
    """涨跌停幅度（百分数）"""
    nm = (name or "").upper()
    board = board_of(code)
    if board == "bj":
        return 30.0
    if "ST" in nm.replace(" ", ""):
        return 5.0
    if board in ("gem", "star"):
        return 20.0
    return 10.0


# ---------------------------------------------------------------------------
# 实时行情
# ---------------------------------------------------------------------------
EM_QUOTE_FIELDS = ("f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
                   "f20,f21,f23,f24,f25,f62,f86,f115,f124,f152")


def _em_num(v):
    if v is None or v == "-" or v == "":
        return None
    try:
        f = float(v)
    except Exception:
        return None
    return f


def quotes(codes: list[str], ttl: float = 2.0) -> dict:
    """批量获取实时行情，返回 {标准化代码: {...}}"""
    norm = []
    for c in codes:
        try:
            norm.append(normalize(c))
        except Exception:
            continue
    norm = list(dict.fromkeys(norm))
    if not norm:
        return {}
    key = "q:" + ",".join(sorted(norm))
    hit = cache_get(key, ttl)
    if hit is not None:
        return hit
    try:
        out = _em_quotes(norm)
    except Exception:
        out = {}
    missing = [c for c in norm if c not in out]
    if missing:
        try:
            out.update(_tx_quotes(missing))
        except Exception:
            pass
    missing = [c for c in norm if c not in out]
    if missing:
        try:
            out.update(_sina_quotes(missing))
        except Exception:
            pass
    for c in norm:
        out.setdefault(c, {"symbol": c, "price": None, "error": "行情获取失败"})
    return cache_put(key, out)


def _em_quotes(norm: list[str]) -> dict:
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&np=1&fields="
           + urllib.parse.quote(EM_QUOTE_FIELDS, safe=",")
           + "&secids=" + ",".join(secid(c) for c in norm))
    js = http_get_json(url)
    data = (js or {}).get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    out = {}
    for it in diff:
        d6 = str(it.get("f12") or "")
        mk = "SH" if it.get("f13") == 1 else ("BJ" if d6.startswith(("4", "8", "92")) else "SZ")
        sym = "%s.%s" % (d6, mk)
        price = _em_num(it.get("f2"))
        prev = _em_num(it.get("f18"))
        name = it.get("f14") or ""
        out[sym] = {
            "symbol": sym, "code": d6, "market": mk, "name": name,
            "price": price, "prev_close": prev,
            "open": _em_num(it.get("f17")), "high": _em_num(it.get("f15")),
            "low": _em_num(it.get("f16")), "change": _em_num(it.get("f4")),
            "change_pct": _em_num(it.get("f3")),
            "volume": _em_num(it.get("f5")), "amount": _em_num(it.get("f6")),
            "amplitude": _em_num(it.get("f7")), "turnover": _em_num(it.get("f8")),
            "pe": _em_num(it.get("f9")), "pb": _em_num(it.get("f23")),
            "volume_ratio": _em_num(it.get("f10")),
            "market_cap": _em_num(it.get("f20")), "float_cap": _em_num(it.get("f21")),
            "main_inflow": _em_num(it.get("f62")),
            "ts": it.get("f124") or it.get("f86"),
            "limit_up": round(prev * (1 + limit_pct(sym, name) / 100.0), 2) if prev else None,
            "limit_down": round(prev * (1 - limit_pct(sym, name) / 100.0), 2) if prev else None,
            "source": "eastmoney",
        }
    return out


def _tx_quotes(norm: list[str]) -> dict:
    codes = []
    for c in norm:
        d6, mk = c.split(".")
        codes.append(("sh" if mk == "SH" else ("bj" if mk == "BJ" else "sz")) + d6)
    txt = http_get_text("https://qt.gtimg.cn/q=" + ",".join(codes), TX_HEADERS, "gbk")
    out = {}
    for line in txt.split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        head, _, body = line.partition("=")
        body = body.strip().strip('"')
        parts = body.split("~")
        if len(parts) < 46:
            continue
        d6 = parts[2]
        try:
            mk = normalize(d6).split(".")[1]
        except Exception:
            continue
        sym = "%s.%s" % (d6, mk)

        def f(i):
            try:
                return float(parts[i])
            except Exception:
                return None
        out[sym] = {
            "symbol": sym, "code": d6, "market": mk, "name": parts[1],
            "price": f(3), "prev_close": f(4), "open": f(5),
            "volume": f(6), "change": f(31), "change_pct": f(32),
            "high": f(33), "low": f(34), "amount": (f(37) or 0) * 10000.0,
            "turnover": f(38), "pe": f(39), "amplitude": f(43),
            "float_cap": (f(44) or 0) * 1e8, "market_cap": (f(45) or 0) * 1e8,
            "pb": f(46) if len(parts) > 46 else None,
            "limit_up": f(47) if len(parts) > 47 else None,
            "limit_down": f(48) if len(parts) > 48 else None,
            "ts": parts[30] if len(parts) > 30 else None,
            "source": "tencent",
        }
    return out


def _sina_quotes(norm: list[str]) -> dict:
    codes = []
    for c in norm:
        d6, mk = c.split(".")
        codes.append(("sh" if mk == "SH" else ("bj" if mk == "BJ" else "sz")) + d6)
    txt = http_get_text("https://hq.sinajs.cn/list=" + ",".join(codes), SINA_HEADERS, "gbk")
    out = {}
    for m in re.finditer(r'hq_str_([a-z]{2})(\d{6})="([^"]*)"', txt):
        mk2, d6, body = m.group(1), m.group(2), m.group(3)
        parts = body.split(",")
        if len(parts) < 32 or not parts[0]:
            continue
        mk = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(mk2, "SH")
        sym = "%s.%s" % (d6, mk)

        def f(i):
            try:
                return float(parts[i])
            except Exception:
                return None
        prev = f(2)
        out[sym] = {
            "symbol": sym, "code": d6, "market": mk, "name": parts[0],
            "open": f(1), "prev_close": prev, "price": f(3),
            "high": f(4), "low": f(5), "volume": (f(8) or 0) / 100.0,
            "amount": f(9), "change": (f(3) - prev) if (f(3) is not None and prev) else None,
            "change_pct": ((f(3) / prev - 1) * 100.0) if (f(3) is not None and prev) else None,
            "ts": parts[30] if len(parts) > 30 else None,
            "limit_up": round(prev * (1 + limit_pct(sym, parts[0]) / 100.0), 2) if prev else None,
            "limit_down": round(prev * (1 - limit_pct(sym, parts[0]) / 100.0), 2) if prev else None,
            "source": "sina",
        }
    return out


def quote(code: str) -> dict:
    return quotes([code]).get(normalize(code), {})


# ---------------------------------------------------------------------------
# K 线
# ---------------------------------------------------------------------------
PERIOD_KLT = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60,
              "daily": 101, "weekly": 102, "monthly": 103}


# ---------------------------------------------------------------------------
# K 线：多数据源容错
#
# 为什么必须做这件事：东方财富对高频请求会直接断连
#（报 Remote end closed connection without response），
# 而 K 线只有它一个源的话，引擎就会「一只股票都算不出来」，
# 表现就是日志刷满「取 XXX 数据失败」而且完全没有委托单。
#
# 所以东财拿不到就自动换腾讯，腾讯不行再换新浪。
# 连续失败 3 次就临时跳过这个源 3 分钟，免得每次都白等。
# ---------------------------------------------------------------------------
_KLINE_SRC = {"东财": {"fails": 0, "skip_until": 0.0},
              "腾讯": {"fails": 0, "skip_until": 0.0},
              "新浪": {"fails": 0, "skip_until": 0.0}}


def _src_ready(name: str) -> bool:
    return time.time() >= _KLINE_SRC[name]["skip_until"]


def _src_hit(name: str) -> None:
    _KLINE_SRC[name]["fails"] = 0
    _KLINE_SRC[name]["skip_until"] = 0.0


def _kline_src_fail(name: str) -> None:
    # 注意：这里必须叫 _kline_src_fail，不能叫 _src_fail。
    # 文件后面排行源（rank）也定义了一个 _src_fail(name, err)（双参数），
    # Python 后定义会覆盖先定义，导致这里 kline 的单参数调用 _src_fail(name)
    # 在运行时抛 "missing 1 required positional argument: 'err'"，
    # 把 K 线源真正的失败原因整个盖掉。两组熔断各用各的名字，互不覆盖。
    h = _KLINE_SRC[name]
    h["fails"] += 1
    # 连续 2 次失败就冷却 4 分钟（原来是 3 次/3 分钟）。
    # 一个源真的挂了的时候，拖着每只股票都去撞它一次纯粹是浪费时间，
    # 早点熔断、专心用还能用的源，整体速度能快一个数量级。
    if h["fails"] >= 2:
        h["skip_until"] = time.time() + 240.0
        h["fails"] = 0


_last_reset = {"at": 0.0}


def _src_reset_all() -> None:
    """把所有源的熔断冷却清零。

    用在「一个源都试不了」的极端情况：如果四个源全在冷却，而我们现在
    一只股票的数据都没有，那冷却就没意义了 —— 宁可去撞一次限流，
    也不能什么都不返回（那会让上层误判成「这只股票没数据」）。

    但也不能无限重置（否则每只股票都撞一轮，等于没有熔断），
    所以 60 秒内最多重置一次。
    """
    now = time.time()
    if now - _last_reset["at"] < 60.0:
        return
    _last_reset["at"] = now
    for h in _KLINE_SRC.values():
        h["fails"] = 0
        h["skip_until"] = 0.0


def kline_source_status() -> dict:
    """给自检/日志用：看看哪个源现在被临时跳过了"""
    return {k: {"ok": _src_ready(k), "skip_until": v["skip_until"]}
            for k, v in _KLINE_SRC.items()}


def _prefix(sym: str) -> str:
    mk = sym.split(".")[-1] if "." in sym else "SH"
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(mk, "sh")


def _bar(date, o, c, h, l, v, amount=0.0, name="", sym=""):
    return {"date": str(date), "open": float(o), "close": float(c),
            "high": float(h), "low": float(l), "volume": float(v or 0),
            "amount": float(amount or 0), "amplitude": None,
            "change_pct": None, "change": None, "turnover": None,
            "name": name, "symbol": sym}


def _pad_derived(rows: list) -> list:
    """腾讯/新浪不直接给的字段，自己补上

    特别重要：成交额 amount。它们是按「手」给成交量的，而且不给成交额，
    但平台里有因子（成交额规模、Amihud 非流动性）要算 log(成交额)，
    补 0 的话这些因子会整段变成 None，进而把整个因子组合的分都变成 None
    —— 表现就是「策略一个信号都没有，永远不下单」。
    """
    for i, r in enumerate(rows):
        # 成交量单位是手，1 手 = 100 股；成交额 ≈ 量(股) × 均价
        vol = r.get("volume") or 0
        if vol and not r.get("amount"):
            h, l, c = r.get("high"), r.get("low"), r.get("close")
            if h is not None and l is not None and c is not None:
                avg = (h + l + c) / 3.0
            else:
                avg = c or 0
            r["amount"] = round(vol * 100.0 * avg, 2)
        if i == 0:
            continue
        pc = rows[i - 1].get("close")
        if not pc:
            continue
        r["change"] = round(r["close"] - pc, 4)
        r["change_pct"] = round((r["close"] - pc) / pc * 100, 4)
        if r.get("high") is not None and r.get("low") is not None:
            r["amplitude"] = round((r["high"] - r["low"]) / pc * 100, 4)
    return rows


# ---------------------------------------------------------------------------
# 除权检测 + 简易前复权
#
# 为什么必须做：复权源全挂时会降级用不复权数据。不复权的价格在除权日会断崖，
# 比如 10 送 10，股价从 100 直接变 50，看起来是 -50%。因子会把这一天算成
# "暴跌"，策略据此发出买入信号 —— 但你实际上什么都没亏，只是股数翻倍。
# 这种脏数据会把 IC、回测、信号全部污染，而且很难被发现（数据看着是"正常"的）。
#
# 纯标准库能做的：用「跌幅是否超过该板块的跌停限制」来判断是不是除权。
# 正常跌停最多 -10%（主板）/ -20%（创业板科创板）/ -30%（北交所），
# 一旦跌幅明显超过这个数，又不是真跌停（收盘价不等于跌停价），那基本就是除权。
# 然后把除权日之前的价格按比例缩放，等效于前复权。
# ---------------------------------------------------------------------------
def _board_limit(sym: str, name: str = "") -> float:
    try:
        return float(limit_pct(sym, name))
    except Exception:  # noqa: BLE001
        return 10.0


def detect_adjust_events(klines: list, sym: str = "", tol: float = 1.5) -> list:
    """找出疑似除权日。

    返回 [{"i": 下标, "date": 日期, "ratio": 复权比例, "drop_pct": 跌幅%}, ...]

    判据：单日跌幅超过「该板块跌停幅度 + tol」个百分点，且收盘价不等于跌停价。
    tol 给 1.5 是留出四舍五入和停牌复牌的余量，避免把正常跌停误判成除权。
    """
    out = []
    if len(klines) < 2:
        return out
    pct = _board_limit(sym, (klines[-1].get("name") or ""))
    threshold = -(pct + tol) / 100.0
    for i in range(1, len(klines)):
        c0 = klines[i - 1].get("close")
        c1 = klines[i].get("close")
        if not c0 or not c1:
            continue
        try:
            c0f, c1f = float(c0), float(c1)
        except Exception:  # noqa: BLE001
            continue
        if c0f <= 0 or c1f <= 0:
            continue
        chg = c1f / c0f - 1.0
        if chg >= threshold:
            continue
        # 真跌停不算除权：收盘价正好压在跌停价上
        ld = round(c0f * (1 - pct / 100.0), 2)
        if abs(c1f - ld) < 0.02:
            continue
        out.append({"i": i, "date": str(klines[i].get("date"))[:10],
                    "ratio": round(c1f / c0f, 6), "drop_pct": round(chg * 100, 2)})
    return out


def readjust_forward(klines: list, events: list) -> list:
    """按检测到的除权比例，把除权日【之前】的价格向前复权。

    前复权的含义：让历史价格和最新价格可比。所以每次除权，都把该日之前的
    所有 OHLC 乘上比例；多次除权时按时间正序处理，效果自然累积。
    """
    if not events:
        return klines
    out = [dict(k) for k in klines]
    for ev in events:
        idx, r = int(ev["i"]), float(ev["ratio"])
        if r <= 0:
            continue
        for j in range(idx):
            row = out[j]
            for f in ("open", "close", "high", "low"):
                v = row.get(f)
                if v is not None:
                    try:
                        row[f] = round(float(v) * r, 4)
                    except Exception:  # noqa: BLE001
                        pass
            # 成交额随价格等比缩放（成交量不变）
            a = row.get("amount")
            if a:
                try:
                    row["amount"] = round(float(a) * r, 2)
                except Exception:  # noqa: BLE001
                    pass
    return out


def fix_unadjusted(klines: list, sym: str = "") -> tuple:
    """对不复权 K 线做「除权检测 + 前复权」，返回 (修正后数据, 事件列表)。"""
    ev = detect_adjust_events(klines, sym)
    if not ev:
        return klines, []
    return readjust_forward(klines, ev), ev


# 记录哪些股票做过除权修正，供自检/日志查看
_ADJUST_LOG: dict = {}


def adjust_log() -> dict:
    """查看最近做过除权修正的股票（自检用）"""
    return dict(_ADJUST_LOG)


def _kline_em(sym, period, adjust, limit, start, end):
    klt = PERIOD_KLT.get(period, 101)
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           "&klt=%d&fqt=%d&beg=%s&end=%s&lmt=%d&_=%d"
           % (secid(sym), klt, int(adjust), start or "0", end or "20500101",
              int(limit), int(time.time() * 1000)))
    js = http_get_json(url)
    data = (js or {}).get("data") or {}
    name = data.get("name") or ""
    out = []
    for s in (data.get("klines") or []):
        p = s.split(",")
        if len(p) < 7:
            continue
        try:
            bar = _bar(p[0], p[1], p[2], p[3], p[4], p[5], p[6], name, sym)
            bar["amplitude"] = float(p[7]) if len(p) > 7 and p[7] not in ("", "-") else None
            bar["change_pct"] = float(p[8]) if len(p) > 8 and p[8] not in ("", "-") else None
            bar["change"] = float(p[9]) if len(p) > 9 and p[9] not in ("", "-") else None
            bar["turnover"] = float(p[10]) if len(p) > 10 and p[10] not in ("", "-") else None
            out.append(bar)
        except Exception:
            continue
    return out


def _kline_tencent(sym, period, adjust, limit, start, end):
    """腾讯行情。支持 前复权/后复权/不复权，日线周线月线。"""
    per = {"daily": "day", "weekly": "week", "monthly": "month"}.get(period)
    if not per:
        return []          # 分钟线这个接口不支持，交给别的源
    fq = {0: "", 1: "qfq", 2: "hfq"}.get(int(adjust), "qfq")
    pre = _prefix(sym)
    code = sym.split(".")[0]
    n = max(int(limit or 320), 20)
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s%s,%s,,,%d,%s"
           % (pre, code, per, n, fq))
    js = http_get_json(url, timeout=8.0)
    d = ((js or {}).get("data") or {}).get(pre + code) or {}
    rows = d.get(fq + per) or d.get(per) or []
    if not rows and fq:
        rows = d.get(per) or []
    out = []
    for r in rows:
        if len(r) < 6:
            continue
        try:
            # 腾讯的顺序是 [日期, 开, 收, 高, 低, 量]
            out.append(_bar(r[0], r[1], r[2], r[3], r[4], r[5], 0.0, "", sym))
        except Exception:
            continue
    return _pad_derived(out)


def _kline_sina(sym, period, adjust, limit, start, end):
    """新浪行情。只用于不复权兜底（它这个接口不给复权价）。"""
    if int(adjust) != 0:
        return []
    scale = {"daily": 240, "weekly": 1200, "monthly": 7200}.get(period)
    if not scale:
        return []
    pre = _prefix(sym)
    code = sym.split(".")[0]
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s%s&scale=%d&ma=no&datalen=%d"
           % (pre, code, scale, max(int(limit or 320), 20)))
    txt = http_get_text(url, timeout=8.0)
    try:
        arr = json.loads(txt)
    except Exception:
        # 新浪偶尔会返回不带引号的键（不是标准 JSON），自己按 键:值 拆一下
        arr = []
        for chunk in txt.split("},"):
            row = {}
            for piece in chunk.replace("{", "").replace("}", "").replace("[", "").replace("]", "").split(","):
                if ":" not in piece:
                    continue
                k, _, v = piece.partition(":")
                row[k.strip().strip('"')] = v.strip().strip('"')
            if row.get("day") or row.get("date"):
                arr.append(row)
    out = []
    for r in (arr or []):
        try:
            out.append(_bar(r.get("day") or r.get("date"), r.get("open"), r.get("close"),
                            r.get("high"), r.get("low"), r.get("volume"), 0.0, "", sym))
        except Exception:
            continue
    return _pad_derived(out)


def _kline_netease(sym, period, adjust, limit, start, end):
    """网易财经历史K线（CSV 接口，只支持日线、不复权）

    【已下线，当前未被 kline() 调用】
    2026-10 实测该接口返回 502 Bad Gateway，网易财经的历史数据服务已停止。
    函数保留在这里：一是别人想换源时有参考模板，二是万一哪天恢复，
    只要把它加回 kline() 的 sources 列表即可。
    """
    if period != "daily":
        return []
    if int(adjust) != 0:
        return []                       # 它只给不复权
    d6, mk = sym.split(".")
    if mk == "BJ":
        return []                       # 北交所它不提供
    code = ("0" if mk == "SH" else "1") + d6
    s = (start or "").replace("-", "")
    e = (end or "").replace("-", "")
    if not s:
        # 按想要的根数估算起始日（多留余量，避开周末/节假日）
        back = max(int(limit or 320) * 2, 420)
        s = (dt.date.today() - dt.timedelta(days=back)).strftime("%Y%m%d")
    if not e:
        e = dt.date.today().strftime("%Y%m%d")
    url = ("https://quotes.money.163.com/service/chddata.html?code=%s&start=%s&end=%s"
           "&fields=TCLOSE;HIGH;LOW;TOPEN;LCLOSE;CHG;PCHG;TURNOVER;VOTURNOVER;VATURNOVER"
           % (code, s, e))
    txt = http_get_text(url, timeout=10.0, encoding="gbk")
    lines = [ln for ln in txt.splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    out = []
    for ln in lines[1:]:                # 第一行是表头
        p = ln.split(",")
        if len(p) < 7:
            continue
        try:
            # 日期,股票代码,名称,收盘,最高,最低,开盘,前收,涨跌额,涨跌幅,换手率,成交量(股),成交额(元)
            date = p[0].strip()
            close, high, low, openp = float(p[3]), float(p[4]), float(p[5]), float(p[6])
            vol = 0.0
            if len(p) > 11 and p[11] not in ("", "None", "-"):
                vol = float(p[11]) / 100.0      # 网易给的是「股」，统一转成「手」
            amt = 0.0
            if len(p) > 12 and p[12] not in ("", "None", "-"):
                amt = float(p[12])
            if close <= 0:
                continue
            out.append(_bar(date, openp, close, high, low, vol, amt, "", sym))
        except Exception:
            continue
    out.sort(key=lambda x: x["date"])
    return _pad_derived(out)


def kline(code: str, period: str = "daily", adjust: int = 1, limit: int = 500,
          start: str = "", end: str = "", use_cache: bool = True) -> list[dict]:
    """历史K线。adjust: 0不复权 1前复权 2后复权

    四个源依次尝试：东财 -> 腾讯 -> 新浪 -> 网易。
    拿了不到还会做两级兜底：
      1) 有源在「冷却」里没试过 -> 强制解除冷却再试一次（宁可撞限流也不静默失败）
      2) 要复权数据却一条都没有 -> 降级用不复权数据顶上（算因子够用）
      3) 网络全挂 -> 用磁盘缓存（哪怕过期）
    只有以上全部失败才抛 DataError。
    """
    sym = normalize(code)
    klt = PERIOD_KLT.get(period, 101)
    key = "k:%s:%s:%s:%s:%s:%s" % (sym, klt, adjust, limit, start, end)
    if use_cache:
        hit = cache_get(key, 60 if klt == 101 else 20)
        if hit is not None:
            return hit
        # 磁盘缓存兜底：历史日K当天不变，命中磁盘就免去一次网络请求
        disk = _kline_disk_get(sym, period, adjust, limit)
        if disk is not None:
            cache_put(key, disk)
            return disk

    sources = (("东财", _kline_em), ("腾讯", _kline_tencent),
               ("新浪", _kline_sina))

    out = []
    last_err = None
    skipped = []

    for name, fn in sources:
        if not _src_ready(name):
            skipped.append(name)
            continue
        try:
            got = fn(sym, period, adjust, limit, start, end) or []
            if got:
                _src_hit(name)
                out = got
                break
        except Exception as e:  # noqa: BLE001
            last_err = e
            _kline_src_fail(name)

    # ---- 兜底 1：刚才被冷却跳过的源，强制解除冷却再给一次机会 ----
    # 这一步很关键：四个源如果全在冷却期，上面的循环会一次都不执行，
    # 以前就会静默返回空列表，上层只能报「没取到行情数据」，根本没法诊断。
    if not out and skipped:
        for name in skipped:
            _KLINE_SRC[name]["fails"] = 0
            _KLINE_SRC[name]["skip_until"] = 0.0
        for name, fn in sources:
            if name not in skipped:
                continue
            try:
                got = fn(sym, period, adjust, limit, start, end) or []
                if got:
                    _src_hit(name)
                    out = got
                    break
            except Exception as e:  # noqa: BLE001
                last_err = e
                _kline_src_fail(name)

    # ---- 兜底 2：要复权却拿不到，降级用不复权数据（因子只看相对变化，够用） ----
    # 降级时把「本来就只给不复权的新浪」放最前面：它不挑复权参数，最可能一次就成。
    # 关键：不复权数据必须做除权修正，否则除权日的假跳空会污染 IC / 回测 / 信号。
    if not out and int(adjust) != 0:
        for name, fn in (("新浪", _kline_sina), ("东财", _kline_em), ("腾讯", _kline_tencent)):
            try:
                got = fn(sym, period, 0, limit, start, end) or []
                if got:
                    _src_hit(name)
                    got, evs = fix_unadjusted(got, sym)
                    if evs:
                        _ADJUST_LOG[sym] = {
                            "count": len(evs),
                            "recent": evs[-3:],
                            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        try:
                            from . import store as _store   # 懒导入，避免循环依赖
                            _store.log("WARN", "datasource",
                                       "%s 复权源不可用，已用不复权数据 + 检测到 %d 次除权并做前复权修正"
                                       % (sym, len(evs)))
                        except Exception:  # noqa: BLE001
                            pass
                    out = got
                    break
            except Exception as e:  # noqa: BLE001
                last_err = e

    if not out:
        # ---- 兜底 3：网络全挂时用磁盘缓存，哪怕过期了也比完全没有强 ----
        for adj in ((adjust,) if int(adjust) == 0 else (adjust, 0)):
            stale = _kline_disk_get(sym, period, adj, limit)
            if stale is not None:
                cache_put(key, stale)
                return stale
        raise DataError("K线获取失败: %s（%s）" % (sym, err_brief(last_err)))

    if limit and limit > 0 and len(out) > limit:
        out = out[-limit:]
    if use_cache and out:
        cache_put(key, out)
        _kline_disk_put(sym, period, adjust, limit, out)
    return out


def minute(code: str) -> list[dict]:
    """今日分时"""
    return kline(code, "1m", 0, 300)


# ---------------------------------------------------------------------------
# 全市场股票列表 / 搜索
# ---------------------------------------------------------------------------
ALL_A_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


_list_fetching = threading.Event()


def _list_page(pn: int) -> list:
    url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1&fltt=2&invt=2"
           "&fid=f12&fs=%s&fields=f12,f13,f14,f100" % (pn, urllib.parse.quote(ALL_A_FS, safe=":,")))
    js = http_get_json(url, timeout=8.0, retries=2)
    data = (js or {}).get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    out = []
    for it in diff:
        d6 = str(it.get("f12") or "")
        if len(d6) != 6:
            continue
        mk = "SH" if it.get("f13") == 1 else ("BJ" if d6.startswith(("4", "8", "92")) else "SZ")
        out.append({"symbol": "%s.%s" % (d6, mk), "code": d6, "name": it.get("f14") or "",
                    "market": mk, "industry": it.get("f100") or ""})
    return out, int(data.get("total") or 0)


def fetch_stock_list(force: bool = False) -> list[dict]:
    """拉取全市场 A 股列表并缓存到本地文件（每天更新一次）

    一共几千只股票，东财每页最多 100 条，串行拉要一两分钟，
    所以这里用多线程并发拉，几秒就能拉完。
    """
    today = dt.date.today().isoformat()
    if not force and STOCK_LIST_FILE.exists():
        try:
            blob = json.loads(STOCK_LIST_FILE.read_text(encoding="utf-8"))
            lst = blob.get("list") or []
            # 老版本缓存里没有行业字段，发现就重新拉一次
            if blob.get("date") == today and lst and "industry" in lst[0]:
                return lst
        except Exception:
            pass
    first, total = [], 0
    try:
        first, total = _list_page(1)
    except Exception:
        first, total = [], 0
    items = list(first)
    if total > 100:
        pages = min((total + 99) // 100, 120)
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=10) as ex:
                for res in ex.map(lambda p: _safe_page(p), range(2, pages + 1)):
                    items.extend(res)
        except Exception:
            pass
    if items:
        # 去重 + 排序
        seen = {}
        for it in items:
            seen[it["code"]] = it
        items = sorted(seen.values(), key=lambda x: x["code"])
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STOCK_LIST_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"date": today, "list": items}, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, STOCK_LIST_FILE)
        except Exception:
            pass
        cache_put("stocklist", items)
        return items
    return []


def _safe_page(pn: int) -> list:
    try:
        return _list_page(pn)[0]
    except Exception:
        return []


def warm_stock_list() -> None:
    """后台预热股票列表，用户第一次搜索时就不用等"""
    if _list_fetching.is_set():
        return
    _list_fetching.set()

    def _run():
        try:
            n = len(stock_list())
            if n:
                pass
        except Exception:
            pass
        finally:
            pass

    threading.Thread(target=_run, name="warm-stock-list", daemon=True).start()


def stock_list() -> list[dict]:
    hit = cache_get("stocklist", 3600)
    if hit is not None:
        return hit
    try:
        items = fetch_stock_list()
    except Exception:
        items = []
    if not items:
        try:
            blob = json.loads(STOCK_LIST_FILE.read_text(encoding="utf-8"))
            items = blob.get("list") or []
        except Exception:
            items = []
    return cache_put("stocklist", items)


def industry_map() -> dict:
    """{标准化代码: 行业名}，数据来自每天缓存的全市场列表"""
    hit = cache_get("industry_map", 1800)
    if hit is not None:
        return hit
    out = {}
    for it in stock_list():
        if it.get("industry"):
            out[it["symbol"]] = it["industry"]
    return cache_put("industry_map", out)


def industry_of(symbol: str) -> str:
    """股票所属行业名。

    优先用基本面增强模块缓存的「申万三级行业」（更精确，能区分白酒Ⅱ/白酒Ⅲ），
    取不到再回退到全市场列表里的粗分类。

    ⚠️ 这里只读缓存、绝不联网：本函数在回测收益归因里会被高频调用，
    一旦触发网络请求会把回测拖慢甚至卡死。
    """
    try:
        sym = normalize(symbol)
        try:
            from . import fundamental as _fnd
            if _fnd and config.get("fundamental.enabled", True):
                nm = _fnd.industry_name_cached(sym)
                if nm:
                    return nm
        except Exception:
            pass
        return industry_map().get(sym, "") or ""
    except Exception:
        return ""


def search(kw: str, limit: int = 20) -> list[dict]:
    """按代码或名称搜股票"""
    kw = (kw or "").strip()
    if not kw:
        return []
    kw_u = kw.upper()
    items = stock_list()
    out = []
    for it in items:
        if kw_u in it["code"] or kw in it["name"] or kw_u in it["symbol"]:
            out.append(it)
            if len(out) >= limit:
                break
    if not out and len(kw) == 6 and kw.isdigit():
        try:
            q = quote(kw)
            if q.get("name"):
                out.append({"symbol": q["symbol"], "code": q["code"], "name": q["name"], "market": q["market"]})
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# 涨跌幅 / 成交额 / 换手率 排行
#
# 为什么需要三个数据源：
#   东财 clist 是最全的（5925 只、字段最多），但它对「列表类接口」按来源 IP 做
#   限流，而且限流方式是**直接 RST 断连**（RemoteDisconnected），不是返回 403。
#   同一个 host 的 stock/get（单只查询）是 200 正常，clist 却是 RST——实测确认
#   这不是 URL/请求头/编码的问题，是接口级别的封禁，一旦被盯上会持续很久。
#   所以必须有备源，否则一个接口被封，前端涨跌幅榜就整块空白。
#
#   源优先级：新浪 Market_Center -> 东财 clist -> 本地全市场快照（离线兜底）
#   新浪实测稳定（连续 10/10 通过、字段够涨跌幅/成交额/换手榜用），东财 clist 已确认
#   按来源 IP 长期封禁，所以把新浪提为主源：正常路径根本不碰东财，也就不会每次刷新
#   都「失败一次 + 告警一次」。失败的源进入冷却期（指数增长、上限 2 小时），冷却期内
#   静默跳过，不白等超时、不刷屏。
# ---------------------------------------------------------------------------
RANK_TTL_SESSION = 15.0      # 交易时段内的内存缓存时长（秒）
RANK_TTL_IDLE = 90.0         # 非交易时段的缓存时长（秒）
RANK_STALE_MAX = 1800.0      # 全部数据源都挂了，最多拿多久以前的旧数据顶（秒）
RANK_SOURCE_COOLDOWN = 120.0 # 某个源失败后冷却多久再试（秒）

_rank_cache: dict = {}       # (kind, limit) -> {"ts": float, "rows": list, "source": str}
_rank_lock = threading.RLock()
_rank_health: dict = {}      # 源名 -> {"until": 冷却截止时间, "fails": 连续失败次数, "err": 最后错误}

_RANK_FID = {"gainers": "f3", "losers": "f3", "amount": "f6", "turnover": "f8"}
_RANK_FIELDS = "f2,f3,f4,f5,f6,f8,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23"
# 新浪排序字段：涨跌幅 / 成交额 / 换手率
_RANK_SINA_SORT = {"gainers": ("changepercent", 0), "losers": ("changepercent", 1),
                   "amount": ("amount", 0), "turnover": ("turnoverratio", 0)}


def _rank_log(level: str, msg: str, detail=None) -> None:
    """排行相关日志。写库失败也不能影响主流程。"""
    try:
        from . import store
        store.log(level, "rank", msg, detail)
    except Exception:  # noqa: BLE001
        print("[%s][rank] %s" % (level.upper(), msg), flush=True)


def _src_available(name: str) -> bool:
    st = _rank_health.get(name)
    return not (st and st.get("until", 0) > time.time())


def _src_fail(name: str, err) -> None:
    with _rank_lock:
        st = _rank_health.setdefault(name, {"fails": 0})
        st["fails"] = int(st.get("fails", 0)) + 1
        st["err"] = str(err)[:120]
        # 冷却时间随连续失败次数指数增长，上限 2 小时。
        #
        # 为什么上限要从 10 分钟拉到 2 小时：东财 clist 是「列表接口按来源 IP
        # 封禁」，一旦被盯上会持续数小时（实测 20 分钟以上都不恢复，且是 RST
        # 断连而非 403）。冷却太短的话，每到期一次就去撞一次被封的源、又记一条
        # 告警，日志被同一件事反复刷屏，用户看着就像「永远修不好」。
        # 拉长冷却 + 冷却期内静默跳过，才能让告警真正安静下来。
        # 一旦源恢复（下次成功会 _src_ok 清掉这里），冷却自动解除，不会永久封死。
        cool = min(RANK_SOURCE_COOLDOWN * (2 ** (st["fails"] - 1)), 7200.0)
        st["until"] = time.time() + cool


def _src_ok(name: str) -> None:
    with _rank_lock:
        _rank_health.pop(name, None)


def _mk_market(code: str, em_flag=None, sina_prefix: str = "") -> str:
    """推断交易所后缀"""
    p = (sina_prefix or "").lower()
    if p in ("sh", "sz", "bj"):
        return p.upper()
    if em_flag == 1:
        return "SH"
    return "BJ" if str(code).startswith(("4", "8", "92")) else "SZ"


def _rank_em(kind: str, limit: int, timeout: float = 4.0) -> list[dict]:
    """东财 clist 排行（主源）。

    ⚠️ pz（每页条数）硬上限 100，传更大也只返回 100 条，这里主动夹住。
    """
    fid = _RANK_FID.get(kind, "f3")
    po = 0 if kind == "losers" else 1
    url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=%d&po=%d&np=1&fltt=2&invt=2"
           "&ut=bd1d9ddb04089700cf9c27f6f7426281&fid=%s&fs=%s&fields=%s"
           % (limit, po, fid, urllib.parse.quote(ALL_A_FS, safe=":,"), _RANK_FIELDS))
    # retries=3：RemoteDisconnected 是瞬时的，重建连接重试成功率高，且失败也几乎不耗时
    js = http_get_json(url, EM_HEADERS, timeout, retries=3)
    diff = ((js or {}).get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    out = []
    for it in diff:
        code = str(it.get("f12") or "").strip()
        if len(code) != 6:
            continue
        mk = _mk_market(code, it.get("f13"))
        out.append({
            "symbol": "%s.%s" % (code, mk), "code": code, "name": it.get("f14") or "",
            "price": _em_num(it.get("f2")), "change_pct": _em_num(it.get("f3")),
            "change": _em_num(it.get("f4")),
            "volume": _em_num(it.get("f5")), "amount": _em_num(it.get("f6")),
            "turnover": _em_num(it.get("f8")), "high": _em_num(it.get("f15")),
            "low": _em_num(it.get("f16")), "open": _em_num(it.get("f17")),
            "prev_close": _em_num(it.get("f18")), "market_cap": _em_num(it.get("f20")),
            "float_cap": _em_num(it.get("f21")), "pb": _em_num(it.get("f23")),
        })
    if not out:
        raise DataError("东财 clist 返回空列表")
    return out


def _rank_sina(kind: str, limit: int, timeout: float = 6.0) -> list[dict]:
    """新浪 Market_Center 排行（备源）。

    URL 形如 .../Market_Center.getHQNodeData?page=1&num=100&node=hs_a&sort=changepercent&asc=0
    node=hs_a 是沪深A股全市场（含北交所）。实测单次 100 条稳定，返回 JSON 数组。
    """
    sort, asc = _RANK_SINA_SORT.get(kind, ("changepercent", 0))
    base = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData")
    out: list[dict] = []
    page = 1
    while len(out) < limit and page <= 5:
        num = min(100, limit - len(out))
        if num <= 0:
            break
        url = ("%s?page=%d&num=%d&node=hs_a&sort=%s&asc=%d&symbol=&_s_r_a=page"
               % (base, page, num, sort, asc))
        txt = http_get_text(url, SINA_HEADERS, "utf-8", timeout, retries=2)
        try:
            data = json.loads(txt)
        except Exception as e:  # noqa: BLE001
            raise DataError("新浪排行返回不是合法 JSON") from e
        if not isinstance(data, list) or not data:
            break
        for it in data:
            code = str(it.get("code") or "").strip()
            if len(code) != 6:
                continue
            sym = str(it.get("symbol") or "")
            mk = _mk_market(code, None, sym[:2])
            price = _em_num(it.get("trade"))
            if not price or price <= 0:      # 停牌/未上市，price=0
                continue
            vol = _em_num(it.get("volume"))
            amt = _em_num(it.get("amount"))
            cap_wan = _em_num(it.get("mktcap"))     # 新浪 mktcap 单位是万元
            nmc_wan = _em_num(it.get("nmc"))
            out.append({
                "symbol": "%s.%s" % (code, mk), "code": code,
                "name": it.get("name") or "", "price": price,
                "change_pct": _em_num(it.get("changepercent")),
                "change": _em_num(it.get("pricechange")),
                "volume": vol, "amount": amt,
                "turnover": _em_num(it.get("turnoverratio")),
                "high": _em_num(it.get("high")), "low": _em_num(it.get("low")),
                "open": _em_num(it.get("open")), "prev_close": _em_num(it.get("settlement")),
                "market_cap": cap_wan * 10000 if cap_wan else None,
                "float_cap": nmc_wan * 10000 if nmc_wan else None,
                "pb": _em_num(it.get("pb")),
            })
            if len(out) >= limit:
                break
        if len(data) < num:      # 最后一页
            break
        page += 1
    if not out:
        raise DataError("新浪排行返回空列表")
    return out[:limit]


def _rank_local(kind: str, limit: int) -> list[dict]:
    """离线兜底：用本地全市场快照（screener 抓过的）在内存里排序。

    东财 clist 被封、新浪也不通时至少还有东西可看。数据是上次抓取的快照，会标 stale。
    """
    from . import screener      # 延迟导入，避免循环依赖
    snap = screener._load_snap(max_age_hours=72.0)
    if not snap:
        return []
    key = "chg_pct" if kind in ("gainers", "losers") else ("amount" if kind == "amount" else "turnover")
    rows = [r for r in snap.get("rows") or [] if (r.get("price") or 0) > 0]
    rows.sort(key=lambda r: (r.get(key) if r.get(key) is not None else -1e18),
              reverse=(kind != "losers"))
    out = []
    for r in rows[:limit]:
        out.append({
            "symbol": r.get("symbol"), "code": r.get("code"), "name": r.get("name"),
            "price": r.get("price"), "change_pct": r.get("chg_pct"),
            "change": None, "volume": r.get("volume"), "amount": r.get("amount"),
            "turnover": r.get("turnover"), "high": None, "low": None,
            "open": None, "prev_close": None,
            # 快照里的字段名是 mktcap / float_cap（screener 的命名），别写成 market_cap
            "market_cap": r.get("mktcap"), "float_cap": r.get("float_cap"),
            "pb": r.get("pb"),
        })
    return out


def rank_with_meta(kind: str = "gainers", limit: int = 20, force: bool = False) -> dict:
    """涨跌幅 / 成交额 / 换手率排行（带来源与状态信息）

    返回 {"list": [...], "source": "eastmoney"/"sina"/"snapshot"/"cache", "stale": bool, "message": str}
    永远不抛异常：最差情况返回 list=[]，message 说明原因。
    """
    try:
        limit = max(1, min(int(limit or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    kind = kind if kind in _RANK_FID else "gainers"
    ck = (kind, limit)
    now = time.time()
    ttl = RANK_TTL_SESSION if in_session() else RANK_TTL_IDLE

    if not force:
        with _rank_lock:
            hit = _rank_cache.get(ck)
        if hit and (now - hit["ts"]) < ttl:
            return {"list": hit["rows"], "source": hit["source"], "stale": False, "message": ""}

    errs = []
    for name, fn in (("sina", _rank_sina), ("eastmoney", _rank_em)):
        if not _src_available(name):
            # 冷却期内静默跳过：源还在封禁里，去撞只会再失败一次、再刷一条告警。
            # 不把「冷却中」写进 errs、也不打日志——告警只在「真正尝试且失败」时产生。
            continue
        t0 = time.time()
        try:
            rows = fn(kind, limit)
            if not rows:
                raise DataError("返回空")
            cost = int((time.time() - t0) * 1000)
            _src_ok(name)
            with _rank_lock:
                _rank_cache[ck] = {"ts": time.time(), "rows": rows, "source": name}
            if errs:
                _rank_log("WARN", "排行主源失败，已切换备源 %s（%s）" % (name, "; ".join(errs)),
                          {"kind": kind, "limit": limit, "ms": cost})
            return {"list": rows, "source": name, "stale": False, "message": ""}
        except Exception as e:  # noqa: BLE001
            cost = int((time.time() - t0) * 1000)
            brief = err_brief(e)
            errs.append("%s: %s" % (name, brief))
            _src_fail(name, e)
            _rank_log("WARN", "排行源 %s 失败：%s" % (name, brief),
                      {"kind": kind, "attempt_ms": cost, "err": type(e).__name__})

    # 在线源全挂：用旧缓存顶（stale），再不行用本地快照
    with _rank_lock:
        hit = _rank_cache.get(ck)
    if hit and (time.time() - hit["ts"]) < RANK_STALE_MAX:
        age = int(time.time() - hit["ts"])
        _rank_log("WARN", "排行全部在线源失败，使用 %d 秒前的缓存" % age, {"errs": errs})
        return {"list": hit["rows"], "source": hit["source"], "stale": True,
                "message": "数据源不稳定，显示 %d 秒前的数据" % age}

    try:
        rows = _rank_local(kind, limit)
    except Exception as e:  # noqa: BLE001
        rows = []
        errs.append("snapshot: %s" % err_brief(e))
    if rows:
        _rank_log("WARN", "排行降级为本地快照", {"errs": errs})
        return {"list": rows, "source": "snapshot", "stale": True,
                "message": "在线行情源不可用，显示本地快照数据"}

    _rank_log("ERROR", "排行全部数据源失败", {"kind": kind, "errs": errs})
    return {"list": [], "source": "", "stale": False,
            "message": "行情源暂不可用（%s）" % "; ".join(errs[:2])}


def rank(kind: str = "gainers", limit: int = 20) -> list[dict]:
    """涨跌幅榜 / 成交额榜（只要列表的简版接口，兼容老调用）"""
    return rank_with_meta(kind, limit).get("list") or []


def indices(ttl: float = 5.0) -> list[dict]:
    hit = cache_get("indices", ttl)
    if hit is not None:
        return hit
    try:
        q = quotes(MAIN_INDEX, ttl=ttl)
    except Exception:
        return []
    out = []
    for s in MAIN_INDEX:
        it = q.get(s)
        if it:
            out.append(it)
    return cache_put("indices", out)


def _indices_old() -> list[dict]:
    try:
        q = quotes(MAIN_INDEX, ttl=3)
    except Exception:
        return []
    out = []
    for s in MAIN_INDEX:
        it = q.get(s)
        if it:
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# 市场状态 / 交易日
# ---------------------------------------------------------------------------
def now() -> dt.datetime:
    return dt.datetime.now()


def in_session(t: dt.time | None = None) -> bool:
    t = t or now().time()
    for s, e in config.get("trading.sessions", [["09:30", "11:30"], ["13:00", "15:00"]]):
        sh, sm = [int(x) for x in s.split(":")]
        eh, em = [int(x) for x in e.split(":")]
        if dt.time(sh, sm) <= t <= dt.time(eh, em):
            return True
    return False


def is_weekday(d: dt.date | None = None) -> bool:
    d = d or now().date()
    return d.weekday() < 5


def index_trade_dates(force: bool = False) -> set:
    """用上证指数的日K日期来判断哪天是交易日"""
    hit = cache_get("trade_dates", 0 if force else 1800)
    if hit is not None:
        return hit
    dates = set()
    try:
        ks = kline("000001.SH", "daily", 0, 90, use_cache=not force)
        dates = {k["date"][:10] for k in ks}
    except Exception:
        dates = set()
    # 拿到空结果就不缓存，避免网络抖动时把「空日历」缓存 30 分钟，
    # 导致后续 is_trading_day 一直走不到兜底逻辑。
    if dates:
        cache_put("trade_dates", dates)
    return dates


def is_trading_day(d: dt.date | None = None) -> bool:
    d = d or now().date()
    if not is_weekday(d):
        return False
    dates = index_trade_dates()
    if not dates:
        # 拿不到交易日历（网络失败），退化成「工作日即交易日」
        return True
    if d.isoformat() in dates:
        return True
    # 关键：日K里「今天」往往还没收盘生成（盘中时最新只到昨天），
    # 所以今天不在 dates 里是常态，不能据此判「非交易日」。
    # 正确做法：只要今天是工作日，且不是「明显落在长假区间内」的日期，就认为是交易日。
    maxd = dt.date.fromisoformat(max(dates))
    gap = (d - maxd).days
    # 1) 间隔 0~3 天：覆盖普通周末/单日节假日，今天必然是交易日（最常见、最关键的场景）
    if 0 <= gap <= 3:
        return True
    # 2) 间隔 > 3 天：要么是长假（国庆/春节），要么是长假后首个交易日。
    #    这里的处理原则是「宁可乐观判交易日，也别把真实交易日误判成休市」——
    #    误判休市会让用户错过交易时段（更严重）；而误判交易日顶多显示「交易中」，
    #    配合 data_fresh（行情新鲜度）字段前端能自行提示「行情未更新」。
    #    长假后首个交易日开盘前，日K通常已能拉到当天数据，多数情况会命中上面的 in dates。
    return True


def _ts_is_fresh(raw: str, tolerance: float = 900.0) -> bool:
    """行情时间戳是否是最近的（支持 10 位秒 和 14 位 yyyymmddHHMMSS 两种格式）"""
    s = (raw or "").strip()
    if not s or not s.isdigit():
        return False
    try:
        if len(s) == 14:
            when = dt.datetime.strptime(s, "%Y%m%d%H%M%S")
        elif len(s) >= 12:
            when = dt.datetime.strptime(s[:14], "%Y%m%d%H%M%S")
        else:
            return (time.time() - float(s)) < tolerance
    except Exception:
        return False
    return abs((dt.datetime.now() - when).total_seconds()) < tolerance


def market_status(ttl: float = 3.0) -> dict:
    """市场状态。带短缓存 —— 前端每隔几秒就刷一次，不能每次都打网络。"""
    hit = cache_get("mstatus", ttl)
    if hit is not None:
        return hit
    out = _market_status_uncached()
    return cache_put("mstatus", out)


def _market_status_uncached() -> dict:
    t = now()
    weekday_ok = is_weekday(t.date())
    session_ok = in_session(t.time())
    trading_day = is_trading_day(t.date())
    fresh = False
    last_ts = ""
    try:
        q = quote("000001.SH")
        last_ts = str(q.get("ts") or "")
        fresh = _ts_is_fresh(last_ts)
    except Exception:
        pass
    if not trading_day:
        state, label = "holiday", "休市（非交易日）"
    elif session_ok:
        state, label = "open", "交易中"
    elif t.time() < dt.time(9, 15):
        state, label = "pre", "盘前"
    elif dt.time(9, 15) <= t.time() < dt.time(9, 30):
        state, label = "auction", "集合竞价"
    elif dt.time(11, 30) < t.time() < dt.time(13, 0):
        state, label = "break", "午间休市"
    else:
        state, label = "closed", "已收盘"
    return {
        "state": state, "label": label, "trading_day": trading_day,
        "session_open": session_ok, "weekday": weekday_ok,
        "now": t.strftime("%Y-%m-%d %H:%M:%S"),
        "data_fresh": fresh, "last_quote_ts": last_ts,
    }
