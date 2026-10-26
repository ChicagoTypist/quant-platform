# -*- coding: utf-8 -*-
"""本地数据库（SQLite）

存：策略、委托、成交、持仓快照、日志、自选股、回测结果。
没有装任何第三方库，Python 自带 sqlite3。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import datetime as dt
from typing import Any, Iterable

from . import config

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS kv (
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS strategies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        stype TEXT NOT NULL,
        params TEXT NOT NULL DEFAULT '{}',
        symbols TEXT NOT NULL DEFAULT '[]',
        mode TEXT NOT NULL DEFAULT 'signal',
        enabled INTEGER NOT NULL DEFAULT 0,
        created_at TEXT, updated_at TEXT,
        note TEXT DEFAULT '',
        last_run TEXT DEFAULT '',
        last_signal TEXT DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, strategy_id INTEGER, symbol TEXT, name TEXT,
        side TEXT, price REAL, qty INTEGER, filled_qty INTEGER DEFAULT 0,
        avg_price REAL DEFAULT 0, status TEXT, broker TEXT, mode TEXT,
        reason TEXT, fee REAL DEFAULT 0, entrust_no TEXT, message TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, order_id INTEGER, strategy_id INTEGER,
        symbol TEXT, name TEXT, side TEXT, price REAL, qty INTEGER,
        amount REAL, fee REAL, broker TEXT, mode TEXT,
        pnl REAL DEFAULT 0, note TEXT DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS paper_account (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        cash REAL, initial_capital REAL, updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS paper_positions (
        symbol TEXT PRIMARY KEY, name TEXT, qty INTEGER, available INTEGER,
        avg_cost REAL, last_price REAL, open_date TEXT, updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS equity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, mode TEXT, broker TEXT, total REAL, cash REAL,
        market_value REAL, day_pnl REAL
    )""",
    """CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, level TEXT, source TEXT, message TEXT, detail TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS watchlist (
        symbol TEXT PRIMARY KEY, name TEXT, added_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT, expires_at TEXT,
        source TEXT, strategy_id INTEGER, strategy_name TEXT,
        symbol TEXT, name TEXT, side TEXT, qty INTEGER, price REAL,
        order_type TEXT, reason TEXT, risk TEXT,
        status TEXT DEFAULT '待确认', sent_at TEXT, order_id INTEGER, result TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)",
    """CREATE TABLE IF NOT EXISTS backtests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, name TEXT, stype TEXT, params TEXT, symbols TEXT,
        result TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)",
    "CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts)",
    "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)",
    "CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts)",
]


# 策略「部署」相关字段。老版本数据库缺这些列，启动时自动补上。
STRATEGY_EXTRA_COLS = {
    "deployed": "INTEGER NOT NULL DEFAULT 0",     # 是否已授权自动交易
    "deployed_at": "TEXT DEFAULT ''",
    # 1 = 引擎直接把委托发给券商（全自动）；0 = 只生成「待确认委托单」等用户点发送
    "direct_send": "INTEGER NOT NULL DEFAULT 1",
    "capital_pct": "REAL DEFAULT 30.0",           # 这个策略最多用总资产的百分之多少
    "max_single_pct": "REAL DEFAULT 20.0",        # 单只上限（覆盖全局）
    "max_order_amount": "REAL DEFAULT 50000.0",   # 单笔上限（覆盖全局）
    "stop_loss_pct": "REAL DEFAULT 8.0",          # 策略级止损，部署时必须 > 0
    "take_profit_pct": "REAL DEFAULT 0.0",
    "expires_at": "TEXT DEFAULT ''",              # 授权到期时间
    "last_action_at": "TEXT DEFAULT ''",
    # 手动/优化器指定的每只股票权重，{"600519.SH": 12.5, ...}；空表示按策略默认
    "symbol_weights": "TEXT DEFAULT ''",
}


def _migrate(conn) -> None:
    """给老数据库补上新增的列（升级程序时不会丢数据）"""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(strategies)").fetchall()}
    except Exception:
        return
    for name, decl in STRATEGY_EXTRA_COLS.items():
        if name not in cols:
            try:
                conn.execute("ALTER TABLE strategies ADD COLUMN %s %s" % (name, decl))
            except Exception:
                pass


def now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            config.ensure_dirs()
            _conn = sqlite3.connect(str(config.DB_FILE), check_same_thread=False, timeout=15)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            for stmt in SCHEMA:
                _conn.execute(stmt)
            _migrate(_conn)
            _conn.commit()
            # 保证模拟账户存在
            row = _conn.execute("SELECT id FROM paper_account WHERE id=1").fetchone()
            if row is None:
                cap = float(config.get("paper.initial_capital", 200000.0))
                _conn.execute(
                    "INSERT INTO paper_account(id, cash, initial_capital, updated_at) VALUES(1,?,?,?)",
                    (cap, cap, now_str()),
                )
                _conn.commit()
        return _conn


def q(sql: str, args: Iterable = ()) -> list[dict]:
    with _lock:
        cur = connect().execute(sql, tuple(args))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def q1(sql: str, args: Iterable = ()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: Iterable = ()) -> int:
    with _lock:
        conn = connect()
        cur = conn.execute(sql, tuple(args))
        conn.commit()
        rid = cur.lastrowid
        cur.close()
        return rid


def executemany(sql: str, seq) -> None:
    with _lock:
        conn = connect()
        conn.executemany(sql, list(seq))
        conn.commit()


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def log(level: str, source: str, message: str, detail: Any = None) -> None:
    try:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        execute(
            "INSERT INTO logs(ts, level, source, message, detail) VALUES(?,?,?,?,?)",
            (now_str(), level.upper(), source, message, detail or ""),
        )
        # 顺手在控制台打印一份，方便排查
        print("[%s][%s] %s" % (level.upper(), source, message), flush=True)
    except Exception as e:  # 日志失败也不能影响主流程
        print("log error:", e, flush=True)


def recent_logs(limit: int = 200, level: str | None = None, source: str | None = None) -> list[dict]:
    sql = "SELECT * FROM logs WHERE 1=1"
    args: list = []
    if level:
        sql += " AND level=?"
        args.append(level.upper())
    if source:
        sql += " AND source=?"
        args.append(source)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return q(sql, args)


def clear_logs() -> None:
    execute("DELETE FROM logs")


# ---------------------------------------------------------------------------
# 键值
# ---------------------------------------------------------------------------
def kv_get(key: str, default: Any = None) -> Any:
    row = q1("SELECT value FROM kv WHERE key=?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def kv_set(key: str, value: Any) -> None:
    execute(
        "INSERT INTO kv(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False, default=str)),
    )


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------
def _hydrate(r: dict) -> dict:
    r["params"] = json.loads(r["params"] or "{}")
    r["symbols"] = json.loads(r["symbols"] or "[]")
    r["enabled"] = bool(r["enabled"])
    r["deployed"] = bool(r.get("deployed"))
    r["direct_send"] = bool(r.get("direct_send", 1))
    for k in ("capital_pct", "max_single_pct", "max_order_amount",
              "stop_loss_pct", "take_profit_pct"):
        try:
            r[k] = float(r.get(k) or 0)
        except Exception:
            r[k] = 0.0
    try:
        r["symbol_weights"] = json.loads(r.get("symbol_weights") or "{}")
    except Exception:
        r["symbol_weights"] = {}
    return r


def list_strategies() -> list[dict]:
    return [_hydrate(dict(r)) for r in q("SELECT * FROM strategies ORDER BY id DESC")]


def get_strategy(sid: int) -> dict | None:
    row = q1("SELECT * FROM strategies WHERE id=?", (sid,))
    return _hydrate(dict(row)) if row else None


def save_strategy(s: dict) -> int:
    params = json.dumps(s.get("params") or {}, ensure_ascii=False)
    symbols = json.dumps(s.get("symbols") or [], ensure_ascii=False)

    def num(k, d):
        try:
            return float(s.get(k, d))
        except Exception:
            return float(d)

    sw = s.get("symbol_weights")
    sw_json = json.dumps(sw, ensure_ascii=False) if isinstance(sw, dict) and sw else ""

    vals = (
        s.get("name", "未命名"), s.get("stype", ""), params, symbols,
        s.get("mode", "signal"), 1 if s.get("enabled") else 0, s.get("note", ""),
        1 if s.get("deployed") else 0, s.get("deployed_at", "") or "",
        1 if s.get("direct_send", True) else 0,
        num("capital_pct", 30.0), num("max_single_pct", 20.0),
        num("max_order_amount", 50000.0), num("stop_loss_pct", 8.0),
        num("take_profit_pct", 0.0), s.get("expires_at", "") or "", sw_json,
    )
    sid = s.get("id")
    if sid:
        execute(
            """UPDATE strategies SET name=?,stype=?,params=?,symbols=?,mode=?,enabled=?,note=?,
               deployed=?,deployed_at=?,direct_send=?,capital_pct=?,max_single_pct=?,
               max_order_amount=?,stop_loss_pct=?,take_profit_pct=?,expires_at=?,
               symbol_weights=?, updated_at=? WHERE id=?""",
            vals + (now_str(), int(sid)),
        )
        return int(sid)
    cols = ("name,stype,params,symbols,mode,enabled,note,deployed,deployed_at,direct_send,"
            "capital_pct,max_single_pct,max_order_amount,stop_loss_pct,take_profit_pct,expires_at,"
            "symbol_weights,created_at,updated_at")
    return execute("INSERT INTO strategies(%s) VALUES(%s)" % (cols, ",".join("?" * 19)),
                   vals + (now_str(), now_str()))


def delete_strategy(sid: int) -> None:
    execute("DELETE FROM strategies WHERE id=?", (sid,))


def touch_strategy(sid: int, last_signal: str = "") -> None:
    execute("UPDATE strategies SET last_run=?, last_signal=? WHERE id=?", (now_str(), last_signal, sid))


# ---------------------------------------------------------------------------
# 委托 / 成交
# ---------------------------------------------------------------------------
def add_order(o: dict) -> int:
    return execute(
        """INSERT INTO orders(ts,strategy_id,symbol,name,side,price,qty,filled_qty,avg_price,
           status,broker,mode,reason,fee,entrust_no,message)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (o.get("ts") or now_str(), o.get("strategy_id"), o.get("symbol"), o.get("name", ""),
         o.get("side"), float(o.get("price") or 0), int(o.get("qty") or 0),
         int(o.get("filled_qty") or 0), float(o.get("avg_price") or 0),
         o.get("status", "已报"), o.get("broker", "paper"), o.get("mode", "paper"),
         o.get("reason", ""), float(o.get("fee") or 0), o.get("entrust_no", ""), o.get("message", "")),
    )


def update_order(oid: int, **fields) -> None:
    if not fields:
        return
    cols = ",".join("%s=?" % k for k in fields)
    execute("UPDATE orders SET %s WHERE id=?" % cols, list(fields.values()) + [oid])


def list_orders(limit: int = 200, mode: str | None = None) -> list[dict]:
    if mode:
        return q("SELECT * FROM orders WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, limit))
    return q("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))


def add_trade(t: dict) -> int:
    return execute(
        """INSERT INTO trades(ts,order_id,strategy_id,symbol,name,side,price,qty,amount,fee,broker,mode,pnl,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (t.get("ts") or now_str(), t.get("order_id"), t.get("strategy_id"), t.get("symbol"),
         t.get("name", ""), t.get("side"), float(t.get("price") or 0), int(t.get("qty") or 0),
         float(t.get("amount") or 0), float(t.get("fee") or 0), t.get("broker", "paper"),
         t.get("mode", "paper"), float(t.get("pnl") or 0), t.get("note", "")),
    )


def list_trades(limit: int = 200, mode: str | None = None) -> list[dict]:
    if mode:
        return q("SELECT * FROM trades WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, limit))
    return q("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))


def count_orders_today(mode: str = "live") -> int:
    today = dt.date.today().isoformat()
    row = q1("SELECT COUNT(*) AS c FROM orders WHERE mode=? AND ts LIKE ?", (mode, today + "%"))
    return int(row["c"]) if row else 0


def realized_pnl_today(mode: str = "paper") -> float:
    today = dt.date.today().isoformat()
    row = q1("SELECT COALESCE(SUM(pnl),0) AS s FROM trades WHERE mode=? AND ts LIKE ?", (mode, today + "%"))
    return float(row["s"]) if row else 0.0


# ---------------------------------------------------------------------------
# 交易成本（手续费）
# ---------------------------------------------------------------------------
def fee_summary(mode: str = "paper") -> dict:
    """累计手续费 + 换手额统计。

    为什么要单独算这个：手续费在账上是"隐形"的，它不单独占一行账目——
      * 买入手续费被摊进了持仓成本（avg_cost = (金额+费)/数量），
        只表现为"浮盈比你以为的少一点"；
      * 卖出手续费直接在现金里扣，混进了已实现盈亏。
    所以总资产其实早就扣干净了，但界面上一个数字都看不到，很容易
    被当成"程序没扣手续费"。这里把费从成交表里捞出来单独给界面看。

    永不抛异常：出错就返回全 0，界面显示"暂无"即可。
    """
    out = {"buy_count": 0, "buy_amount": 0.0, "buy_fee": 0.0,
           "sell_count": 0, "sell_amount": 0.0, "sell_fee": 0.0,
           "total_fee": 0.0, "turnover": 0.0}
    try:
        rows = q("SELECT side, COUNT(*) AS n, COALESCE(SUM(amount),0) AS amt, "
                 "COALESCE(SUM(fee),0) AS f FROM trades WHERE mode=? GROUP BY side", (mode,))
    except Exception:  # noqa: BLE001
        return out
    for r in rows:
        side = (r["side"] or "").lower()
        if side == "buy":
            out["buy_count"] = int(r["n"])
            out["buy_amount"] = float(r["amt"])
            out["buy_fee"] = float(r["f"])
        elif side == "sell":
            out["sell_count"] = int(r["n"])
            out["sell_amount"] = float(r["amt"])
            out["sell_fee"] = float(r["f"])
    out["total_fee"] = out["buy_fee"] + out["sell_fee"]
    out["turnover"] = out["buy_amount"] + out["sell_amount"]
    return out


def fee_recalc(mode: str = "paper", cfg: dict | None = None) -> float | None:
    """按「当前设置里的费率」把历史成交重算一遍，本该收多少费。

    用途：用户在设置里改了佣金/印花税/过户费之后，界面要能显示
    「换成这个费率，这些成交的费会变成多少」，这样改动才有联动感。

    边界必须说清楚：历史成交是**按当时的费率真实入账**的，改设置不会
    追溯修改台账（否则现金和持仓成本就对不上了）。这里的重算只是一个
    「如果当初就是这个费率」的对照值，不参与任何账目。
    滑点已经烙进成交价（price）里，事后无法从价格里拆出来，故不含滑点。

    返回 None 表示算不了（没有成交或出错），调用方按"暂无"处理。
    """
    try:
        from . import fees as fee_mod
        c = cfg or fee_mod.fee_config()
        rows = q("SELECT side, amount FROM trades WHERE mode=?", (mode,))
    except Exception:  # noqa: BLE001
        return None
    total = 0.0
    try:
        for r in rows:
            amt = float(r["amount"] or 0)
            if amt <= 0:
                continue
            if (r["side"] or "").lower() == "buy":
                total += fee_mod.buy_fee(amt, c)
            else:
                total += fee_mod.sell_fee(amt, c)
    except Exception:  # noqa: BLE001
        return None
    return round(total, 2)


# ---------------------------------------------------------------------------
# 资金曲线
# ---------------------------------------------------------------------------
def add_equity(mode: str, broker: str, total: float, cash: float, mv: float, day_pnl: float = 0.0) -> None:
    execute(
        "INSERT INTO equity(ts,mode,broker,total,cash,market_value,day_pnl) VALUES(?,?,?,?,?,?,?)",
        (now_str(), mode, broker, total, cash, mv, day_pnl),
    )


def equity_curve(mode: str = "paper", limit: int = 1000) -> list[dict]:
    rows = q("SELECT * FROM equity WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, limit))
    return list(reversed(rows))


# ---------------------------------------------------------------------------
# 自选股
# ---------------------------------------------------------------------------
def list_watchlist() -> list[dict]:
    return q("SELECT * FROM watchlist ORDER BY added_at DESC")


def add_watch(symbol: str, name: str = "") -> None:
    execute(
        "INSERT INTO watchlist(symbol,name,added_at) VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET name=excluded.name",
        (symbol, name, now_str()),
    )


def remove_watch(symbol: str) -> None:
    execute("DELETE FROM watchlist WHERE symbol=?", (symbol,))


# ---------------------------------------------------------------------------
# 待确认委托单
# ---------------------------------------------------------------------------
def add_ticket(t: dict) -> int:
    return execute(
        """INSERT INTO tickets(created_at,expires_at,source,strategy_id,strategy_name,
           symbol,name,side,qty,price,order_type,reason,risk,status)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (t.get("created_at") or now_str(), t.get("expires_at", ""), t.get("source", "manual"),
         t.get("strategy_id"), t.get("strategy_name", ""), t.get("symbol"), t.get("name", ""),
         t.get("side"), int(t.get("qty") or 0), float(t.get("price") or 0),
         t.get("order_type", "limit"), t.get("reason", ""), t.get("risk", ""),
         t.get("status", "待确认")),
    )


def list_tickets(status: str | None = None, limit: int = 200) -> list[dict]:
    if status:
        return q("SELECT * FROM tickets WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
    return q("SELECT * FROM tickets ORDER BY id DESC LIMIT ?", (limit,))


def get_ticket(tid: int) -> dict | None:
    return q1("SELECT * FROM tickets WHERE id=?", (tid,))


def update_ticket(tid: int, **fields) -> None:
    if not fields:
        return
    cols = ",".join("%s=?" % k for k in fields)
    execute("UPDATE tickets SET %s WHERE id=?" % cols, list(fields.values()) + [tid])


def clear_tickets(only_done: bool = True) -> int:
    if only_done:
        execute("DELETE FROM tickets WHERE status != '待确认'")
    else:
        execute("DELETE FROM tickets")
    row = q1("SELECT COUNT(*) AS c FROM tickets WHERE status='待确认'")
    return int(row["c"]) if row else 0


def expire_tickets(minutes: int) -> int:
    """把过期的待确认单标掉，返回处理条数"""
    if minutes <= 0:
        return 0
    cutoff = (dt.datetime.now() - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    rows = q("SELECT id FROM tickets WHERE status='待确认' AND created_at < ?", (cutoff,))
    for r in rows:
        update_ticket(int(r["id"]), status="已过期")
    return len(rows)


# ---------------------------------------------------------------------------
# 回测结果
# ---------------------------------------------------------------------------
def save_backtest(name: str, stype: str, params: dict, symbols: list, result: dict) -> int:
    return execute(
        "INSERT INTO backtests(ts,name,stype,params,symbols,result) VALUES(?,?,?,?,?,?)",
        (now_str(), name, stype, json.dumps(params, ensure_ascii=False),
         json.dumps(symbols, ensure_ascii=False), json.dumps(result, ensure_ascii=False, default=str)),
    )


def list_backtests(limit: int = 50) -> list[dict]:
    rows = q("SELECT id,ts,name,stype,params,symbols FROM backtests ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["params"] = json.loads(r["params"] or "{}")
        r["symbols"] = json.loads(r["symbols"] or "[]")
    return rows


def get_backtest(bid: int) -> dict | None:
    row = q1("SELECT * FROM backtests WHERE id=?", (bid,))
    if not row:
        return None
    row["params"] = json.loads(row["params"] or "{}")
    row["symbols"] = json.loads(row["symbols"] or "[]")
    row["result"] = json.loads(row["result"] or "{}")
    return row


def delete_backtest(bid: int) -> None:
    execute("DELETE FROM backtests WHERE id=?", (bid,))
