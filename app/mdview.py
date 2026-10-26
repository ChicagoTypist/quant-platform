# -*- coding: utf-8 -*-
"""极简 Markdown → HTML 渲染器

只处理学习手册里用到的那几种语法：标题、表格、列表、加粗、代码块、分隔线、引用。
不引第三方库。
"""
from __future__ import annotations

import html
import re

BT = chr(96)
FENCE = BT * 3
BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(t: str) -> str:
    t = html.escape(t)
    t = BOLD.sub(r"<b>\1</b>", t)
    return t.replace(BT, "")


def render(md_text: str) -> str:
    lines = md_text.splitlines()
    out = []
    i = 0
    in_ul = in_ol = False

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    while i < len(lines):
        s = lines[i].strip()

        if s.startswith(FENCE):
            close_lists()
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith(FENCE):
                buf.append(html.escape(lines[i]))
                i += 1
            i += 1
            out.append("<pre class='code'>" + "\n".join(buf) + "</pre>")
            continue

        if s.startswith("|"):
            close_lists()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                out.append("<table class='mdtable'><thead><tr>"
                           + "".join("<th>%s</th>" % _inline(c) for c in rows[0])
                           + "</tr></thead><tbody>")
                for r in rows[1:]:
                    out.append("<tr>" + "".join("<td>%s</td>" % _inline(c) for c in r) + "</tr>")
                out.append("</tbody></table>")
            continue

        if s == "---":
            close_lists()
            out.append("<hr>")
            i += 1
            continue
        if s.startswith("#### "):
            close_lists(); out.append("<h4>%s</h4>" % _inline(s[5:])); i += 1; continue
        if s.startswith("### "):
            close_lists(); out.append("<h3>%s</h3>" % _inline(s[4:])); i += 1; continue
        if s.startswith("## "):
            close_lists(); out.append("<h2>%s</h2>" % _inline(s[3:])); i += 1; continue
        if s.startswith("# "):
            close_lists(); out.append("<h1>%s</h1>" % _inline(s[2:])); i += 1; continue
        if s.startswith("> "):
            close_lists(); out.append("<blockquote>%s</blockquote>" % _inline(s[2:])); i += 1; continue

        m = re.match(r"^[-*] \[ \] (.+)$", s)
        if m:
            if not in_ul:
                close_lists(); out.append("<ul class='todo'>"); in_ul = True
            out.append("<li>%s</li>" % _inline(m.group(1)))
            i += 1
            continue
        m = re.match(r"^[-*] (.+)$", s)
        if m:
            if not in_ul:
                close_lists(); out.append("<ul>"); in_ul = True
            out.append("<li>%s</li>" % _inline(m.group(1)))
            i += 1
            continue
        m = re.match(r"^(\d+)\. (.+)$", s)
        if m:
            if not in_ol:
                close_lists(); out.append("<ol>"); in_ol = True
            out.append("<li>%s</li>" % _inline(m.group(2)))
            i += 1
            continue

        if not s:
            close_lists()
            i += 1
            continue
        close_lists()
        out.append("<p>%s</p>" % _inline(s))
        i += 1
    close_lists()
    return "\n".join(out)


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>@@TITLE@@ · A股量化交易平台</title>
<style>
body{background:#0d1117;color:#e6edf3;font-family:"Microsoft YaHei","PingFang SC",sans-serif;
 line-height:1.85;max-width:900px;margin:0 auto;padding:28px 20px 80px;font-size:14.5px}
h1{font-size:26px;border-bottom:2px solid #3b82f6;padding-bottom:10px;margin-top:34px}
h2{font-size:21px;color:#58a6ff;margin-top:36px;border-left:4px solid #3b82f6;padding-left:12px}
h3{font-size:17px;color:#7ee787;margin-top:26px}
h4{font-size:15px;color:#d2a8ff}
p{margin:10px 0}
a{color:#58a6ff}
table.mdtable{border-collapse:collapse;width:100%;margin:14px 0;font-size:13px}
.mdtable th{background:#1c2330;color:#9aa7b4;text-align:left;padding:7px 10px;border:1px solid #2a3240}
.mdtable td{padding:6px 10px;border:1px solid #2a3240;vertical-align:top}
.mdtable tr:hover td{background:#161b22}
pre.code{background:#11161f;border:1px solid #2a3240;border-left:3px solid #3b82f6;
 border-radius:6px;padding:12px 14px;overflow-x:auto;font-family:Consolas,Monaco,monospace;
 font-size:12.5px;line-height:1.65;color:#a5d6ff}
blockquote{border-left:3px solid #d29922;background:#1a1710;margin:12px 0;padding:9px 14px;
 color:#e3c46a;border-radius:0 6px 6px 0}
hr{border:none;border-top:1px solid #2a3240;margin:26px 0}
ul,ol{margin:10px 0;padding-left:26px}
li{margin:5px 0}
ul.todo{list-style:none;padding-left:4px}
ul.todo li:before{content:"☐ ";color:#6b7684}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;
 color:#9aa7b4;font-size:12.5px}
.top a{text-decoration:none}
</style></head><body>
<div class="top"><span>@@TITLE@@</span>
<span><a href="/handbook">学习手册</a> ｜ <a href="/manual">操作手册</a> ｜ <a href="/">回到平台</a></span></div>
@@BODY@@
</body></html>"""


def render_page(md_text: str, title: str = "学习手册") -> str:
    return (PAGE.replace("@@TITLE@@", html.escape(title))
                .replace("@@BODY@@", render(md_text)))
