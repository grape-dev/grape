"""帮助文档静态页生成器（v2）：将 help/usage + help/api 的 markdown 渲染为 src/api/static/help.html。

v2 设计（见 docs/weekly-02/help-page-prototype.md）：
- help.html 为轻量 SPA 壳：入口页(帮助中心)→ 使用文档 / API 文档 两个独立区块
- 目录按区块隔离：使用页=仅使用指南；API 页=仅 API；同一级小节同缩进（不互为父子）
- 点击目录子项左目录常驻，仅右侧内容区替换渲染对应 markdown
- 右侧单文档渲染，H2/H3 轻量标题，仅提示/代码块带框；当前章节/小节高亮 + scrollspy
- 主题切换(🌓 localStorage) + 移动端抽屉目录 + 侧栏搜索

数据流：help/*.md → 启动期生成 → /static/help.html（生成物，.gitignore）
"""
import html as _html
import json
import os
import re
from pathlib import Path

import markdown

_REPO_ROOT = Path(os.path.dirname(os.path.abspath(__file__))).parent
HELP_DIR = _REPO_ROOT / "help"
USAGE_DIR = HELP_DIR / "usage"
API_DIR = HELP_DIR / "api"
OUT_PATH = _REPO_ROOT / "src" / "api" / "static" / "help.html"

# 章节顺序与 tests/test_help_directory.py 的 EXPECTED_USAGE_FILES / EXPECTED_API_FILES 保持一致
USAGE_ORDER = [
    "overview.md",
    "quickstart.md",
    "agents.md",
    "agent-environment-variables.md",
    "pipeline.md",
    "skills.md",
    "runtimes.md",
    "monitoring.md",
    "knowledge-base.md",
    "faq.md",
]
_MD_EXTENSIONS = ["fenced_code", "tables", "toc", "sane_lists", "nl2br"]

# OpenAPI 契约下载：固定 URL（内容随 API 版本刷新，路径不变）
OPENAPI_STATIC_URL = "/static/openapi.json"
OPENAPI_STATIC_PATH = _REPO_ROOT / "src" / "api" / "static" / "openapi.json"
OPENAPI_HELP_PATH = HELP_DIR / "api" / "openapi.json"

_CSS = """\
:root{--bg:#fff;--c:#f7f8fa;--b:#e2e8f0;--t1:#1a202c;--t2:#4a5568;--t3:#a0aec0;--a:#3b82f6;--g:#22c55e;--y:#eab308;--r:#ef4444;--pk:#ec4899;--pu:#8b5cf6;--or:#f97316}
html[data-mode="dark"]{--bg:#0f1117;--c:#1a1d28;--b:#2d3142;--t1:#f1f5f9;--t2:#94a3b8;--t3:#64748b}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--t1);margin:0;line-height:1.7;font-size:15px}
h1{font-size:25px;border-bottom:1px solid var(--b);padding-bottom:8px;margin-top:8px}
h2{font-size:20px;margin-top:28px;padding-left:10px;border-left:3px solid var(--a)}
h3{font-size:16px;margin-top:22px;color:var(--t2)}
p{margin:8px 0}
ul,ol{padding-left:24px}
li{margin:4px 0}
code{background:var(--c);padding:2px 6px;border-radius:4px;font-size:13px;font-family:"SF Mono",Monaco,monospace;color:var(--pk)}
pre{background:var(--c);padding:14px 16px;border-radius:8px;overflow-x:auto;border:1px solid var(--b)}
pre code{background:none;padding:0;color:var(--t1)}
blockquote{margin:12px 0;padding:12px 16px;background:var(--c);border:1px solid var(--b);border-left:4px solid var(--a);border-radius:8px}
blockquote.card.tip{border-left-color:var(--g);background:rgba(34,197,94,.08)}
blockquote.card.warn{border-left-color:var(--y);background:rgba(234,179,8,.08)}
table{width:100%;border-collapse:collapse;margin:12px 0}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--b)}
th{background:var(--c);font-weight:600}
/* ── 壳 ── */
.topnav{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:6px;padding:10px 18px;background:var(--bg);border-bottom:1px solid var(--b)}
.brand{font-weight:700;font-size:15px;color:var(--t1);text-decoration:none;margin-right:10px}
.link{color:var(--t2);text-decoration:none;font-size:13px;padding:4px 12px;border-radius:6px}
.link:hover{color:var(--a)}
.link.active{background:var(--c);color:var(--a);font-weight:600}
.spacer{flex:1}
.btn{border:1px solid var(--b);background:var(--c);color:var(--t1);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:13px}
.burger{display:none}
/* 入口 */
.home{max-width:860px;margin:44px auto;padding:0 20px}
.home h1{font-size:28px;border-bottom:3px solid var(--a)}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-top:30px}
.card{display:block;background:var(--c);border:1px solid var(--b);border-radius:12px;padding:26px;text-decoration:none;color:var(--t1)}
.card:hover{border-color:var(--a);box-shadow:0 6px 20px rgba(0,0,0,.08)}
.card .big{font-size:22px;font-weight:700;margin-bottom:8px}
.card .go{margin-top:16px;color:var(--a);font-weight:600}
/* 骨架 */
.body{display:flex;max-width:1320px;margin:0 auto;padding:0 10px}
.side{width:272px;flex:0 0 272px;position:sticky;top:52px;height:calc(100vh - 52px);overflow-y:auto;padding:16px 8px 60px;border-right:1px solid var(--b)}
.side-group{font-weight:700;font-size:12px;color:var(--t3);letter-spacing:.5px;padding:6px 10px 4px}
.side .search{margin:6px 10px 10px}
.side .search input{width:100%;background:var(--c);border:1px solid var(--b);border-radius:6px;padding:6px 10px;color:var(--t1);font-size:12px}
.dn{display:block;padding:6px 10px;border-radius:6px;color:var(--t1);text-decoration:none;font-size:13px}
.dn:hover{background:var(--c)}
.dn.active{background:rgba(59,130,246,.12);color:var(--a);font-weight:600}
.ds{display:block;padding:4px 10px;border-radius:6px;color:var(--t2);text-decoration:none;font-size:12px;cursor:pointer}
.ds:hover{color:var(--a)}
.ds.active{background:var(--c);color:var(--a);font-weight:600}
.content{flex:1;min-width:0;max-width:940px;padding:26px 32px 90px}
.contract{display:flex;align-items:center;gap:14px;background:var(--c);border:1px solid var(--b);border-radius:10px;padding:14px 18px;margin-bottom:24px}
.contract .ct-info{flex:1;font-size:13px}
.contract .ct-info b{font-size:14px}
.contract .ct-info small{display:block;color:var(--t3);margin-top:2px}
.contract .dl{background:var(--a);color:#fff;text-decoration:none;border-radius:6px;padding:8px 16px;font-size:13px;font-weight:600;white-space:nowrap}
.contract .dl:hover{opacity:.9}
.back-top{position:fixed;bottom:26px;right:26px;background:var(--a);color:#fff;width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:pointer;border:none;font-size:18px;box-shadow:0 4px 12px rgba(0,0,0,.15)}
@media(max-width:900px){
  .burger{display:inline-block}
  .cards{grid-template-columns:1fr}
  .body{display:block}
  .side{position:fixed;left:0;top:52px;bottom:0;width:280px;z-index:30;background:var(--bg);display:none;border-right:1px solid var(--b)}
  .side.open{display:block}
  .content{padding:20px 18px 90px}
}
"""

_MIGRATION_COMMENT = """<!--
  ⚠️ 迁移说明：本文件为生成产物（v2 SPA 壳），内容由 src/help_page_gen.py 依据 help/usage + help/api 渲染。
  如需更新任何内容，请编辑 help/ 目录下对应 markdown 文件后重启服务（help.html 自动重新生成）。
-->"""

_JS_APP = r"""
(function () {
  'use strict';
  var OPENAPI_URL = '__OPENAPI_URL__';
  var DATA = (function () { var el = document.getElementById('help-data'); try { return el ? JSON.parse(el.textContent) : {}; } catch (e) { return {}; } })();
  var app = document.getElementById('app');
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function setMode(m) { document.documentElement.setAttribute('data-mode', m); try { localStorage.setItem('help_theme', m); } catch (e) {} }
  function initMode() { var m = 'light'; try { m = localStorage.getItem('help_theme') || 'light'; } catch (e) {} setMode(m); }
  function docs(s) { return DATA[s] || []; }
  function docOf(s, id) { var d = docs(s); for (var i = 0; i < d.length; i++) { if (d[i].id === id) return d[i]; } return d[0] || null; }
  function subLinks(cur) {
    if (!cur) return '';
    var out = '', t = cur.toc || [], i, r;
    for (i = 0; i < t.length; i++) { r = t[i]; out += '<a class="ds" data-id="' + r.id + '" style="padding-left:' + (8 + 12 * (r.level - 1)) + 'px">' + esc(r.name) + '</a>'; }
    return out;
  }
  function sideList(sec, cur) {
    var out = '<div class="side-group">' + esc(sec === 'usage' ? '使用指南' : 'API 参考') + '</div>';
    out += '<div class="search"><input id="sbox" type="text" placeholder="搜索' + esc(sec === 'usage' ? '使用指南' : 'API 文档') + '..."></div>';
    var h1id = (cur && (cur.html.match(/<h1\b[^>]*id="([^"]+)"/) || [])[1]) || '';
    var d, i;
    for (i = 0; i < docs(sec).length; i++) {
      d = docs(sec)[i];
      out += '<a class="dn' + (d.id === cur.id ? ' active' : '') + '" href="#/' + sec + '/' + d.id + '" data-id="' + d.id + '"' + (d.id === cur.id && h1id ? ' data-h1="' + h1id + '"' : '') + '>' + esc(d.title) + '</a>';
      if (d.id === cur.id) { out += subLinks(cur); }
    }
    return out;
  }
  function topbar(sec) {
    return '<div class="topnav"><a class="brand" href="#/">Grape 帮助中心</a>'
      + '<a class="link' + (sec === 'usage' ? ' active' : '') + '" href="#/usage">使用文档</a>'
      + '<a class="link' + (sec === 'api' ? ' active' : '') + '" href="#/api">API 文档</a>'
      + '<span class="spacer"></span>'
      + '<button class="btn theme-toggle" type="button" title="切换主题">\u2600\ufe0f/\ud83c\udf19</button>'
      + '<button class="btn burger" type="button" title="目录">\u2630</button></div>';
  }
  function renderHome() {
    app.innerHTML = topbar(null)
      + '<div class="home"><h1>Grape 帮助中心</h1>'
      + '<p>帮助文档与 API 参考，按需进入。</p>'
      + '<div class="cards">'
      + '<a class="card" href="#/usage"><div class="big">\ud83d\udcd6 使用文档</div><div>快速上手 · Agent 详解 · 流水线 · 技能 · FAQ</div><div class="go">进入使用文档 \u2192</div></a>'
      + '<a class="card" href="#/api"><div class="big">\u26ed API 文档</div><div>认证与通用约定 · 任务/仓库/技能 等接口参考</div><div class="go">进入 API 文档 \u2192</div></a>'
      + '</div></div>';
  }
  function initSpy(cur) {
    if (!cur) return;
    var map = {};
    var subs = Array.prototype.slice.call(document.querySelectorAll('.side .ds'));
    for (var i = 0; i < subs.length; i++) { map[subs[i].getAttribute('data-id')] = subs[i]; }
    var main = document.querySelector('.side .dn.active');
    var h1 = document.getElementById('content') ? document.querySelector('#content h1[id]') : null;
    if (h1 && main) { map[h1.id] = main; }
    var links = Array.prototype.slice.call(document.querySelectorAll('.side .dn,.side .ds'));
    function onScroll() {
      var els = Array.prototype.slice.call(document.querySelectorAll('#content h1[id],#content h2[id],#content h3[id]'));
      var mark = 106, curId = null;
      for (var k = 0; k < els.length; k++) { if (els[k].getBoundingClientRect().top <= mark) { curId = els[k].id; } }
      for (var j = 0; j < links.length; j++) { links[j].classList.remove('active'); }
      var target = map[curId];
      if (target) { target.classList.add('active'); }
    }
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }
  function bindSearch() {
    var box = document.getElementById('sbox');
    if (!box) return;
    box.addEventListener('input', function () {
      var q = (box.value || '').toLowerCase();
      var items = Array.prototype.slice.call(document.querySelectorAll('.side .dn'));
      for (var i = 0; i < items.length; i++) {
        items[i].style.display = (!q || items[i].textContent.toLowerCase().indexOf(q) >= 0) ? '' : 'none';
      }
    });
    box.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        var q = (box.value || '').toLowerCase();
        var items = Array.prototype.slice.call(document.querySelectorAll('.side .dn'));
        for (var i = 0; i < items.length; i++) {
          if (items[i].style.display !== 'none' && items[i].textContent.toLowerCase().indexOf(q) >= 0) { location.hash = items[i].getAttribute('href'); return; }
        }
      }
    });
  }
  function contractBar() {
    return '<div class="contract"><div class="ct-info"><b>OpenAPI 契约</b><small>OpenAPI 3.1 \u00b7 对外安全接口 \u00b7 Bearer / PRIVATE-TOKEN / access_token 三通道认证</small></div><a class="dl" href="' + OPENAPI_URL + '" download>⬇ 下载 openapi.json</a></div>';
  }
  function renderSection(sec, cur) {
    var body = cur ? cur.html : '';
    if (sec === 'api') { body = contractBar() + body; }
    app.innerHTML = topbar(sec) + '<div class="body"><aside class="side" id="side">' + sideList(sec, cur) + '</aside><main class="content" id="content"></main></div>';
    document.getElementById('content').innerHTML = body;
    window.scrollTo(0, 0);
    var sideEl = document.getElementById('side');
    if (sideEl) { sideEl.classList.remove('open'); }
    initSpy(cur);
    bindSearch();
  }
  function route() {
    var raw = (location.hash || '').replace(/^#/, '');
    var parts = raw.split('/').filter(Boolean);
    var sec = parts[0];
    if (sec !== 'usage' && sec !== 'api') { renderHome(); return; }
    renderSection(sec, docOf(sec, parts[1]));
  }
  app.addEventListener('click', function (e) {
    var sub = e.target.closest ? e.target.closest('a.ds') : null;
    if (sub) {
      e.preventDefault();
      var el = document.getElementById(sub.getAttribute('data-id'));
      if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
      var links = Array.prototype.slice.call(document.querySelectorAll('.side .ds'));
      for (var i = 0; i < links.length; i++) { links[i].classList.toggle('active', links[i] === sub); }
      return;
    }
    if (e.target.closest('.theme-toggle')) {
      var curMode = document.documentElement.getAttribute('data-mode') || 'light';
      setMode(curMode === 'light' ? 'dark' : 'light');
      return;
    }
    if (e.target.closest('.burger')) {
      var side = document.getElementById('side');
      if (side) { side.classList.toggle('open'); }
    }
  });
  window.addEventListener('hashchange', route);
  initMode();
  route();
})();
"""


def render_markdown(text: str) -> str:
    """渲染单个 markdown 文档为 HTML 片段（不含页面壳）。"""
    md = markdown.Markdown(extensions=_MD_EXTENSIONS)
    return md.convert(text)


def _render_file(path: Path) -> tuple[str, list]:
    md = markdown.Markdown(extensions=_MD_EXTENSIONS)
    body = md.convert(path.read_text(encoding="utf-8"))
    return body, (md.toc_tokens or [])


def _prefix_heading_ids(body: str, tokens: list, prefix: str) -> str:
    for token in tokens:
        oid = token.get("id")
        if oid:
            body = re.sub(
                rf'<(h[1-6]) id="{re.escape(oid)}"',
                rf'<\1 id="{prefix}{oid}"',
                body,
            )
        for child in token.get("children", []):
            body = _prefix_heading_ids(body, [child], prefix)
    return body


def _collect_toc(tokens: list, prefix: str) -> list[dict]:
    """收集 level>=2 的标题（level1 即文档标题，单独存 title）。"""
    rows: list[dict] = []

    def walk(nodes: list):
        for t in nodes:
            level = t.get("level", 1)
            if level > 1:
                rows.append({"level": level, "id": prefix + t["id"], "name": t.get("name", "")})
            walk(t.get("children", []))

    walk(tokens)
    return rows


def _adorn_blockquotes(body: str) -> str:
    def _repl(m):
        open_tag, inner = m.group(1), m.group(2)
        head = (inner or "").lstrip()[:24]
        if "💡" in head or "TIP" in head.upper():
            return open_tag.replace("<blockquote>", '<blockquote class="card tip">')
        if any(k in head for k in ("⚠️", "⚠", "❌", "🚫", "注意", "安全")):
            return open_tag.replace("<blockquote>", '<blockquote class="card warn">')
        return open_tag

    return re.sub(r"(<blockquote>)(.*?)(</blockquote>)", _repl, body, flags=re.DOTALL)


def _rewrite_md_hrefs(body: str, section: str) -> str:
    """把文档内相对 .md 链接改写为 SPA 路由（#/{section}/{stem}）。

    - 只改 <a href> 中相对路径且以 .md 结尾（含 .md#anchor）的链接
    - 换行里生成 #/api/00-auth 等路由，点击即切换到对应文档（不再拼成 /static/xx.md 404）
    - 外部（http/https）链接 / 绝对路径（/ 开头）/ 纯锚点（# 开头）保持原样
    - md 源文件不动（GitHub/仓库预览仍是 .md 相对链接）
    """

    def _repl(m):
        prefix, href = m.group(1), m.group(2)
        if href.startswith(("#", "/", "http://", "https://", "mailto:")) or "://" in href:
            return m.group(0)
        path, _, frag = href.partition("#")
        if not path.endswith(".md"):
            return m.group(0)
        route = f"#/{section}/{path[:-3]}"
        if frag:
            route += f"#{frag}"
        return f'{prefix} href="{route}"'

    return re.sub(r'(<a\b[^>]*?)\shref="([^"]*)"', _repl, body)


def _docs(usage: bool) -> list[tuple[Path, str]]:
    if usage:
        return [(USAGE_DIR / f, f) for f in USAGE_ORDER]
    return sorted(
        ((API_DIR / f, f) for f in (p.name for p in API_DIR.glob("*.md"))),
        key=lambda x: (x[1] != "README.md", x[1]),
    )


def _section_data(usage: bool) -> list[dict]:
    section = "usage" if usage else "api"
    items = []
    for path, fname in _docs(usage):
        if not path.is_file():
            continue
        body, tokens = _render_file(path)
        prefix = re.sub(r"[^0-9a-zA-Z]", "-", path.stem).strip("-") + "-"
        body = _prefix_heading_ids(body, tokens, prefix)
        body = _adorn_blockquotes(body)
        body = _rewrite_md_hrefs(body, section)
        title = next((t.get("name", "") for t in tokens if t.get("level") == 1), path.stem)
        items.append({"id": path.stem, "title": title, "html": body, "toc": _collect_toc(tokens, prefix)})
    return items


def build_data() -> dict:
    return {"usage": _section_data(True), "api": _section_data(False)}


def build_help_html(data: dict | None = None) -> str:
    data = data or build_data()
    data_tag = json.dumps(data, ensure_ascii=False).replace("</", "<\\u002f")
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">\n'
        '<title>Grape 帮助中心</title>\n'
        '<link rel="icon" type="image/png" href="/static/logo-64.png">\n'
        f"{_MIGRATION_COMMENT}\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        '<div id="app"></div>\n'
        f'<script type="application/json" id="help-data">{data_tag}</script>\n'
        f"<script>{_JS_APP.replace('__OPENAPI_URL__', OPENAPI_STATIC_URL)}</script>\n"
        "</body>\n</html>"
    )


def generate() -> int:
    """生成并覆写 src/api/static/help.html；返回渲染的 markdown 文件数。"""
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = build_data()
    OUT_PATH.write_text(build_help_html(data), encoding="utf-8")
    return len(data["usage"]) + len(data["api"])


def write_openapi_contract(schema: dict, help_path: Path | None = None, static_path: Path | None = None) -> int:
    """把 OpenAPI 契约 schema 落盘（固定下载 URL=/static/openapi.json）。

    规范副本写 help/api/openapi.json（需求要求），提供副本写 static 目录供下载。
    返回架构 paths 数。
    """
    import json as _json

    text = _json.dumps(schema, ensure_ascii=False, indent=2)
    for p in (help_path or OPENAPI_HELP_PATH, static_path or OPENAPI_STATIC_PATH):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return len(schema.get("paths", {}))


if __name__ == "__main__":
    n = generate()
    print(f"help.html regenerated (v2 SPA) from {n} markdown sources -> {OUT_PATH}")