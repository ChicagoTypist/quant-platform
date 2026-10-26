# -*- coding: utf-8 -*-
"""内置 HTTP 服务器（Python 标准库，不需要装任何东西）

  * /api/* 交给 app/api.py 处理，统一返回 JSON
  * 其它路径当静态文件，从 app/web/ 目录读
  * 默认只监听 127.0.0.1，外网访问不到，安全
"""
from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import api
from . import config
from . import store
from . import __version__

MAX_BODY = 8 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    # 版本号从 app/__init__.py 读，别再手写死（以前写死 1.0，改多少次都不变）
    server_version = "QuantPlatform/" + __version__
    protocol_version = "HTTP/1.1"

    # 不把每个请求都打到控制台，太吵
    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        pass

    # ---------------- 工具 ----------------
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

    def _json(self, status: int, obj):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _parse(self):
        parsed = urllib.parse.urlparse(self.path)
        query = {k: (v[0] if len(v) == 1 else v)
                 for k, v in urllib.parse.parse_qs(parsed.query).items()}
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            if length > MAX_BODY:
                raise ValueError("请求体太大")
            raw = self.rfile.read(length)
            ctype = (self.headers.get("Content-Type") or "").lower()
            try:
                if "json" in ctype or raw.lstrip()[:1] in (b"{", b"["):
                    body = json.loads(raw.decode("utf-8"))
                else:
                    body = {k: (v[0] if len(v) == 1 else v)
                            for k, v in urllib.parse.parse_qs(raw.decode("utf-8")).items()}
            except Exception:
                body = None
        return parsed.path, query, body

    # ---------------- 方法 ----------------
    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        try:
            path, query, body = self._parse()
        except Exception as e:  # noqa: BLE001
            return self._json(400, {"ok": False, "message": str(e)})
        if path.startswith("/api/"):
            try:
                status, obj = api.handle(method, path, query, body)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"ok": False, "message": str(e)[:300]})
            return self._json(status, obj)
        doc = {"handbook": "学习手册", "学习手册": "学习手册", "docs": "学习手册",
               "manual": "操作手册", "操作手册": "操作手册"}.get(path.lstrip("/"))
        if doc:
            return self._render_doc(doc)
        return self._static(path)

    def _render_doc(self, name: str):
        """把 Markdown 手册渲染成网页，边用平台边看"""
        from . import mdview
        src = config.BASE_DIR / (name + ".md")
        if not src.exists():
            return self._send(404, ("还没生成 %s。运行 python tools/make_docs.py 即可。" % name)
                                   .encode("utf-8"), "text/plain; charset=utf-8")
        try:
            html_txt = mdview.render_page(src.read_text(encoding="utf-8"), title=name)
        except Exception as e:  # noqa: BLE001
            return self._send(500, ("渲染失败: %s" % e).encode("utf-8"), "text/plain; charset=utf-8")
        return self._send(200, html_txt.encode("utf-8"), "text/html; charset=utf-8")

    def _static(self, path: str):
        rel = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
        if rel in ("", ".", "/"):
            rel = "index.html"
        target = (config.WEB_DIR / rel).resolve()
        web_root = config.WEB_DIR.resolve()
        # 防目录穿越
        if not str(target).startswith(str(web_root)):
            return self._send(403, b"forbidden", "text/plain; charset=utf-8")
        if not target.is_file():
            return self._send(404, "找不到页面".encode("utf-8"), "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        try:
            data = target.read_bytes()
        except Exception as e:  # noqa: BLE001
            return self._send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")
        return self._send(200, data, ctype)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上千万不要开 SO_REUSEADDR：它允许两个进程绑同一个端口，
    # 结果请求可能被一个"僵尸"老进程接走，表现为莫名其妙的 404 / 502。
    allow_reuse_address = False


def find_free_port(host: str, start: int, tries: int = 30) -> int:
    """找一个真正没人用的端口（不开 SO_REUSEADDR，避免端口被占也"验证通过"）"""
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return start


def make_server(host: str, port: int, tries: int = 30):
    """真实绑定端口；万一被占用就往后找，返回 (httpd, 实际端口)"""
    last = None
    for p in range(port, port + tries):
        try:
            return Server((host, p), Handler), p
        except OSError as e:
            last = e
            continue
    raise RuntimeError("从 %d 开始连续 %d 个端口都被占用了：%s" % (port, tries, last))


def _warm_up():
    """后台把全市场股票列表、指数等缓存准备好，第一次打开页面就不用等"""
    try:
        from . import datasource as ds
        ds.warm_stock_list()
        time.sleep(1.5)
        ds.indices()
        ds.market_status()
        # 预热「今日选股」，这样用户第一次问 AI 助手就不用等几十秒
        try:
            from . import assistant as ast
            ast.warm_picks()
        except Exception:
            pass
    except Exception:
        pass


def serve(host: str | None = None, port: int | None = None, quiet: bool = False):
    config.ensure_dirs()
    store.connect()
    threading.Thread(target=_warm_up, name="warm-up", daemon=True).start()
    host = host or str(config.get("server.host", "127.0.0.1"))
    port = int(port or config.get("server.port", 8899))
    httpd, port = make_server(host, port)
    if port != int(config.get("server.port", 8899)) and not quiet:
        print("默认端口被占用了，自动改用 %d" % port)
    url = "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, port)
    if not quiet:
        print("服务已启动:", url)
    store.log("INFO", "server", "Web 服务启动于 %s" % url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        store.log("INFO", "server", "Web 服务已停止")
    return url


def serve_background(host: str | None = None, port: int | None = None):
    """在后台线程里跑，返回 (thread, url)"""
    host = host or str(config.get("server.host", "127.0.0.1"))
    port = int(port or config.get("server.port", 8899))
    httpd, port = make_server(host, port)
    url = "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, port)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return t, url, httpd
