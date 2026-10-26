# -*- coding: utf-8 -*-
"""A股交易费用计算

买入：佣金（有最低收费）+ 过户费
卖出：佣金（有最低收费）+ 印花税 + 过户费
"""
from __future__ import annotations

from . import config


def fee_config(overrides: dict | None = None) -> dict:
    cfg = {
        "commission_rate": float(config.get("paper.commission_rate", 0.00025)),
        "commission_min": float(config.get("paper.commission_min", 5.0)),
        "stamp_tax_rate": float(config.get("paper.stamp_tax_rate", 0.0005)),
        "transfer_fee_rate": float(config.get("paper.transfer_fee_rate", 0.00001)),
        "slippage": float(config.get("paper.slippage_bps", 3.0)) / 10000.0,
    }
    cfg.update(overrides or {})
    return cfg


def buy_fee(amount: float, cfg: dict) -> float:
    return (max(amount * cfg["commission_rate"], cfg["commission_min"])
            + amount * cfg["transfer_fee_rate"])


def sell_fee(amount: float, cfg: dict) -> float:
    return (max(amount * cfg["commission_rate"], cfg["commission_min"])
            + amount * cfg["stamp_tax_rate"]
            + amount * cfg["transfer_fee_rate"])


def slip(price: float, side: str, cfg: dict) -> float:
    """滑点：买入按不利方向上浮，卖出按不利方向下调"""
    s = float(cfg.get("slippage", 0.0))
    return price * (1.0 + s) if side == "buy" else price * (1.0 - s)
