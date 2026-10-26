# -*- coding: utf-8 -*-
"""配置管理模块

所有可调参数集中在这里，保存在 data/settings.json。
支持用点号路径读写，例如: cfg.get("risk.max_single_position_pct")
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
WEB_DIR = Path(__file__).resolve().parent / "web"
SETTINGS_FILE = DATA_DIR / "settings.json"
DB_FILE = DATA_DIR / "quant.db"

# ---------------------------------------------------------------------------
# 默认配置：这些就是"出厂设置"，网页上改的东西会覆盖它们
# ---------------------------------------------------------------------------
DEFAULTS = {
    "server": {
        "host": "127.0.0.1",
        "port": 8899,
        "open_browser": True,
    },
    # 模拟盘（纸上交易）参数
    "paper": {
        "initial_capital": 200000.0,
        "commission_rate": 0.00025,     # 佣金费率（万2.5）
        "commission_min": 5.0,          # 单笔最低佣金 5 元
        "stamp_tax_rate": 0.0005,       # 印花税（卖出单边，万5）
        "transfer_fee_rate": 0.00001,   # 过户费（双边，万0.1）
        "slippage_bps": 3.0,            # 滑点（基点，万分之），模拟成交价偏差
    },
    # A股交易规则
    "trading": {
        "lot_size": 100,                # 一手 100 股
        "price_limit_check": True,      # 是否做涨跌停限制
        "sessions": [["09:30", "11:30"], ["13:00", "15:00"]],
        "call_auction_start": "09:15",
        "poll_interval_sec": 5,         # 实盘引擎轮询间隔
        "order_cooldown_sec": 30,       # 同一只股票两次下单的最小间隔
        "allow_t0_emulation": False,    # A股是 T+1，默认严格
        "max_signal_age_bars": 1,       # 建仓信号最多"过期"多少根K线（1=必须是最新信号）
        # 下单方式：confirm = 策略出信号后生成"待确认委托单"，你看过再发（默认，最安全）
        #           auto    = 策略出信号后直接发给券商，不问你
        "order_mode": "confirm",
        "ticket_expire_min": 30,        # 待确认委托单多少分钟后自动失效
        "sync_broker_orders": True,     # 是否自动从券商同步委托状态
        "auto_start_engine": False,
    },
    # 风控（非常重要！！）
    "risk": {
        "enabled": True,
        "max_single_position_pct": 20.0,   # 单只股票最多占总资产 %
        "max_total_position_pct": 90.0,    # 总持仓最多占总资产 %
        "max_order_amount": 50000.0,       # 单笔委托最大金额（元）
        "min_order_amount": 1000.0,        # 小于这个金额不下单
        "max_orders_per_day": 50,          # 每天最多下单笔数
        "daily_loss_limit_pct": 5.0,       # 当日亏损超过该比例 -> 熔断停止开仓
        "stop_loss_pct": 0.0,              # 个股止损（0=关闭），如 -8 表示跌 8% 止损
        "take_profit_pct": 0.0,            # 个股止盈（0=关闭）
        "blacklist": [],                   # 黑名单股票代码，永不交易
        "block_st": True,                  # 自动排除 ST / *ST
        "block_new": False,                # 排除上市不足 N 天（未实现，保留）
    },
    # 券商通道
    "broker": {
        "active": "paper",              # paper=模拟盘, ths=同花顺, qmt=迅投QMT
        "ths": {
            "client_path": "",          # 同花顺下单程序 xiadan.exe 的完整路径
            "mode": "universal_client", # 同花顺通用版
            "exe_type": "ths",
            "username": "",             # 资金账号（可选，用于自动登录）
            "auto_login": False,        # 是否让程序帮你填账号密码自动登录
            # 注意：密码不在这里！密码用 Windows 系统加密后单独存放，见 app/secret.py
        },
        "qmt": {
            "userdata_path": "",        # QMT userdata_mini 目录
            "account_id": "",
            "account_type": "STOCK",
        },
    },
    # 实盘总开关（默认关闭，必须手动确认才打开）
    "live": {
        "enabled": False,
        "confirm_phrase": "我确认启用实盘",
        "enabled_at": "",
    },
    "ui": {
        "refresh_sec": 5,
    },
    # AI 选股助手
    "assistant": {
        "enabled": False,                                  # 是否用大模型润色（不填 Key 就是纯本地模式）
        "base_url": "https://api.deepseek.com/v1",         # 任何 OpenAI 兼容接口都行
        "api_key": "",
        "model": "deepseek-chat",
        "timeout": 60,
        "max_tokens": 1200,
    },
    # 基本面增强（可选）。不填令牌也能用：模块会自动读取本机技能缓存，
    # 都取不到时只使用磁盘缓存，绝不影响平台正常启动与回测。
    "fundamental": {
        "token": "",              # NeoData 令牌；也可用环境变量 NEODATA_TOKEN
        "enabled": True,          # 总开关：关掉后选股不再尝试联网抓基本面
        "screener_weight": 0.15,  # 选股时价值因子占的权重（0 = 不用价值因子）
    },
}

_lock = threading.RLock()
_cache: dict | None = None


def _deep_merge(base: dict, override: dict) -> dict:
    """把 override 合并进 base 的副本（递归）"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def load(force: bool = False) -> dict:
    """读取配置（带缓存）"""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        ensure_dirs()
        raw = {}
        if SETTINGS_FILE.exists():
            try:
                raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
        _cache = _deep_merge(DEFAULTS, raw)
        return _cache


def save(cfg: dict) -> dict:
    global _cache
    with _lock:
        ensure_dirs()
        merged = _deep_merge(DEFAULTS, cfg)
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
        _cache = merged
        return merged


def get(path: str, default=None):
    """按点号路径读取，如 get("risk.max_orders_per_day")"""
    node = load()
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def set_(path: str, value):
    """按点号路径写入并落盘"""
    cfg = copy.deepcopy(load())
    parts = path.split(".")
    node = cfg
    for p in parts[:-1]:
        if not isinstance(node.get(p), dict):
            node[p] = {}
        node = node[p]
    node[parts[-1]] = value
    return save(cfg)


def update(patch: dict) -> dict:
    """用嵌套字典做局部更新"""
    return save(_deep_merge(load(), patch))


def reset() -> dict:
    return save(copy.deepcopy(DEFAULTS))


def public_config() -> dict:
    """返回给网页的配置（同花顺路径等做脱敏/只读处理）"""
    cfg = copy.deepcopy(load())
    return cfg
