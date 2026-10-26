# -*- coding: utf-8 -*-
"""程序入口： python -m app.main   或   python app/main.py

会用默认浏览器自动打开平台页面。
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

# 保证在 cmd 的 GBK 控制台下，任何字符都不会因为编码问题让程序崩掉
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="replace")
    except Exception:
        pass

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import config, store                      # type: ignore
    from app.server import serve                       # type: ignore
    from app.engine import engine                     # type: ignore
    from app.brokers import manager                   # type: ignore
else:
    from . import config, store
    from .server import serve, find_free_port
    from .engine import engine
    from .brokers import manager

def _version_text() -> str:
    try:
        from . import __version__, __version_name__
        return "%s %s" % (__version__, __version_name__)
    except Exception:
        try:
            from app import __version__, __version_name__  # type: ignore
            return "%s %s" % (__version__, __version_name__)
        except Exception:
            return ""


def _already_running(host: str, port: int) -> str:
    """检查这个端口上是不是已经跑着【同一版本】的本平台。

    如果跑的是旧版本（比如你升级过程序但旧窗口没关），就不认它，
    让新版本换个端口起来，免得你双击后看到的还是老界面。
    """
    import json as _json
    import urllib.request
    addr = "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, port)
    try:
        with urllib.request.urlopen(addr + "api/health", timeout=2.0) as r:
            data = _json.loads(r.read().decode("utf-8", "replace"))
        if data.get("app") != "A股量化交易平台":
            return ""
        try:
            from .api import build_id as _bid
        except Exception:
            from app.api import build_id as _bid  # type: ignore
        mine = _bid()
        theirs = data.get("build")
        # 关键：拿不到 build 说明那是个【不认识 build 的老版本】，一样不能复用
        if theirs != mine:
            print("  注意：端口 %d 上跑着一个旧版本（build=%s），当前版本是 %s。"
                  % (port, theirs or "老版本(无build)", mine))
            print("        我会换个端口启动新版本。建议把那个旧的黑窗口关掉。")
            return ""
        return addr
    except Exception:
        return ""
    return ""


BANNER = """
================================================================
                A 股 量 化 交 易 平 台   v{ver}
================================================================
  行情数据：东方财富 / 腾讯 / 新浪（免费，无需注册）
  回测引擎：14 种内置策略，真实手续费 + T+1 + 涨跌停
  因子研究：124 个因子（含 65 个 Alpha101）+ IC/分层收益 + 行业中性化
  组合优化：等权 / 因子加权 / 最小方差 / 风险平价 / 最大夏普
  AI 助手：内选股问答（本地可跑，可选接大模型）
  交易通道：模拟盘 / 同花顺(easytrader) / QMT迅投
  风    控：仓位上限 / 单笔上限 / 每日熔断 / 一键急停
----------------------------------------------------------------
  * 默认是模拟盘，不会动你的真钱
  * 要实盘必须先连接通道 + 手动输入确认口令
  * 关掉这个黑窗口 = 关掉程序
================================================================
"""


def main():
    ap = argparse.ArgumentParser(description="A股量化交易平台")
    ap.add_argument("--port", type=int, default=None, help="监听端口，默认 8899")
    ap.add_argument("--host", default=None, help="监听地址，默认 127.0.0.1（只有本机能访问）")
    ap.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    ap.add_argument("--no-engine", action="store_true", help="启动时不要自动开引擎")
    ap.add_argument("--reset-settings", action="store_true", help="恢复默认设置后启动")
    args = ap.parse_args()

    config.ensure_dirs()
    if args.reset_settings:
        config.reset()
        print("已恢复默认设置。")
    store.connect()

    host = args.host or str(config.get("server.host", "127.0.0.1"))
    port = args.port or int(config.get("server.port", 8899))
    url = "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, port)

    # 如果已经开了一个【同版本】的，就别再开第二个（两个进程抢一个端口会互相打架）
    running = ""
    for p in range(port, port + 12):
        running = _already_running(host, p)
        if running:
            break
    if running:
        print(BANNER.format(ver=_version_text()))
        print("  平台已经在运行了，地址： %s" % running)
        print("  直接给你打开浏览器；如果不想再开一个窗口，关掉这个黑框即可。")
        if not args.no_browser:
            try:
                webbrowser.open(running)
            except Exception:
                pass
        return 0

    ver = _version_text()
    print(BANNER.format(ver=ver))
    print("  平台地址： %s" % url)
    print("  数据目录： %s" % config.DATA_DIR)
    print("  按 Ctrl+C 或者直接关掉这个窗口即可退出程序。")
    print("-" * 62)

    if not args.no_browser and config.get("server.open_browser", True):
        def _open():
            time.sleep(1.2)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    # 自动启动引擎（如果配置里开着）
    if not args.no_engine and config.get("trading.auto_start_engine", False):
        try:
            engine.start()
        except Exception as e:  # noqa: BLE001
            print("自动启动引擎失败:", e)

    try:
        serve(host=host, port=port, quiet=False)
    except KeyboardInterrupt:
        print("\n已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
