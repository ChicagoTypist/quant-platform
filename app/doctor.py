# -*- coding: utf-8 -*-
"""环境自检：一次性把"能不能跑起来"这件事查清楚

运行：  python -m app.doctor    或双击  自检.bat
"""
from __future__ import annotations

import os
import platform
import socket
import sys
import time
import traceback
from pathlib import Path

OK = "[ OK ]"
BAD = "[FAIL]"
WARN = "[WARN]"


def main() -> int:
    # 只改错误处理方式，不动编码 —— 这样在 cmd（GBK）和管道（UTF-8）下都不会乱码或崩溃
    for _stream in (sys.stdout, sys.stderr):
        try:
            if _stream is not None and hasattr(_stream, "reconfigure"):
                _stream.reconfigure(errors="replace")
        except Exception:
            pass

    print("=" * 68)
    print("  A股量化交易平台 · 环境自检")
    print("=" * 68)
    print("  时间:", time.strftime("%Y-%m-%d %H:%M:%S"))
    print("  系统:", platform.platform())
    print("  Python:", sys.version.split()[0], "(", sys.executable, ")")
    print()
    problems = []
    warnings = []

    # 1. Python 版本
    if sys.version_info < (3, 8):
        print(BAD, "Python 版本太低，需要 3.8 以上")
        problems.append("Python 版本太低")
    else:
        print(OK, "Python 版本满足要求（>=3.8）")

    # 2. 依赖（只用标准库，所以这里只报告可选项）
    for mod, tip in [("sqlite3", "标准库"), ("json", "标准库")]:
        try:
            __import__(mod)
            print(OK, "%s 可用（%s）" % (mod, tip))
        except Exception as e:  # noqa: BLE001
            print(BAD, "%s 不可用: %s" % (mod, e))
            problems.append("%s 不可用" % mod)

    try:
        import easytrader  # noqa: F401
        print(OK, "easytrader 已安装 —— 可以使用同花顺通道")
    except Exception:
        print(WARN, "easytrader 未安装 —— 想用同花顺通道需要执行: pip install easytrader")
        warnings.append("easytrader 未安装（只影响同花顺通道）")

    try:
        import xtquant  # noqa: F401
        print(OK, "xtquant 已安装 —— 可以使用 QMT 通道")
    except Exception:
        print(WARN, "xtquant 未安装 —— 想用 QMT 通道需要券商提供该库")
        warnings.append("xtquant 未安装（只影响 QMT 通道）")

    # 3. 目录与写权限
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app import config
        config.ensure_dirs()
        probe = config.DATA_DIR / "_doctor_probe.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print(OK, "数据目录可写:", config.DATA_DIR)
        print(OK, "配置目录:", config.BASE_DIR)
    except Exception as e:  # noqa: BLE001
        print(BAD, "数据目录不可写:", e)
        problems.append("数据目录不可写")
        return _finish(problems, warnings)

    # 4. 数据库
    try:
        from app import store
        store.connect()
        store.kv_set("doctor_probe", time.time())
        v = store.kv_get("doctor_probe")
        n = len(store.q("SELECT name FROM sqlite_master WHERE type='table'"))
        print(OK, "数据库正常，共 %d 张表（%s）" % (n, config.DB_FILE))
    except Exception as e:  # noqa: BLE001
        print(BAD, "数据库异常:", e)
        problems.append("数据库异常")

    # 5. 端口
    port = int(config.get("server.port", 8899))
    for p in range(port, port + 5):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            print(OK, "端口 %d 可用（平台将用这个端口）" % p)
            break
        except OSError:
            s.close()
            print(WARN, "端口 %d 被占用，平台会自动换一个" % p)

    # 6. 网络与行情源
    print()
    print("-" * 68)
    print("  行情数据源测试")
    print("-" * 68)
    from app import datasource as ds

    try:
        q = ds.quote("600519.SH")
        if q.get("price"):
            print(OK, "实时行情正常：%s %s 现价 %.2f（来源 %s）"
                  % (q.get("code"), q.get("name"), q.get("price"), q.get("source")))
        else:
            print(BAD, "实时行情返回异常:", q)
            problems.append("实时行情不可用")
    except Exception as e:  # noqa: BLE001
        print(BAD, "实时行情失败:", str(e)[:160])
        problems.append("实时行情不可用（检查网络/防火墙/代理）")

    try:
        ks = ds.kline("600519.SH", "daily", 1, 30)
        print(OK, "历史K线正常：%d 根，最新 %s 收 %.2f"
              % (len(ks), ks[-1]["date"], ks[-1]["close"]))
    except Exception as e:  # noqa: BLE001
        print(BAD, "历史K线失败:", str(e)[:160])
        problems.append("历史K线不可用")

    t0 = time.time()
    try:
        n = len(ds.stock_list())
        print(OK, "股票列表正常：%d 只（耗时 %.1fs，之后当天不再重复拉取）" % (n, time.time() - t0))
    except Exception as e:  # noqa: BLE001
        print(WARN, "股票列表获取失败（只影响按名字搜索）:", str(e)[:120])
        warnings.append("股票列表获取失败")

    try:
        st = ds.market_status()
        print(OK, "市场状态:", st.get("label"), "| 交易日:", st.get("trading_day"),
              "| 行情新鲜:", st.get("data_fresh"))
    except Exception as e:  # noqa: BLE001
        print(WARN, "市场状态查询失败:", str(e)[:120])

    # 7. 回测引擎
    print()
    print("-" * 68)
    print("  回测引擎测试")
    print("-" * 68)
    try:
        from app import backtest
        r = backtest.run("ma_cross", {"fast": 5, "slow": 20}, ["000001.SZ"],
                         start="2024-01-01", capital=200000, save=False, bars=800)
        if r.get("ok"):
            m = r["metrics"]
            print(OK, "回测跑通：收益 %.2f%% / 交易 %d 笔 / 回撤 %.2f%% / 耗时 %.1fs"
                  % (m["total_return_pct"], m["trade_count"], m["max_drawdown_pct"], r["elapsed_sec"]))
        else:
            print(BAD, "回测失败:", r.get("error"))
            problems.append("回测失败")
    except Exception as e:  # noqa: BLE001
        print(BAD, "回测异常:", str(e)[:200])
        traceback.print_exc()
        problems.append("回测异常")

    # 8. 券商通道
    print()
    print("-" * 68)
    print("  交易通道状态")
    print("-" * 68)
    try:
        from app.brokers import manager
        for b in manager.all_status():
            print("   %-8s %-30s 已连接=%-5s %s"
                  % (b["name"], b["display"], b["connected"],
                     "【会下真实委托】" if b["is_live"] else "【虚拟资金，安全】"))
        for name in ("ths", "qmt"):
            br = manager.instance(name)
            if hasattr(br, "check_env"):
                print()
                print("   %s 自检：" % name)
                for it in br.check_env().get("items", []):
                    print("     %s %s - %s" % (OK if it["ok"] else WARN, it["name"], it["detail"][:70]))
    except Exception as e:  # noqa: BLE001
        print(WARN, "通道检查失败:", str(e)[:160])

    return _finish(problems, warnings)


def _finish(problems, warnings) -> int:
    print()
    print("=" * 68)
    if problems:
        print("  结论：发现 %d 个问题，需要处理" % len(problems))
        for p in problems:
            print("     x", p)
        if warnings:
            print("  提醒：")
            for w in warnings:
                print("     !", w)
        print("=" * 68)
        return 1
    print("  结论：全部通过，可以直接双击 启动量化平台.bat 使用 ✔")
    if warnings:
        print("  提醒（不影响使用）：")
        for w in warnings:
            print("     !", w)
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
