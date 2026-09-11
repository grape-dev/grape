"""帮助文档静态页生成器：将 help/usage + help/api 的 markdown 渲染为 src/api/static/help.html。

- 单一事实源 = 仓库根目录 help/（usage 9 章 + api 13 章，Issue #97 拆分）
- 服务启动时（src/api/main.py lifespan）调用 generate() 覆写 help.html
- help.html 为生成物（已在 .gitignore），不做手工编辑；改 help/*.md 后重启即刷新
"""
import html as _html
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
    "pipeline.md",
    "skills.md",
    "runtimes.md",
    "monitoring.md",
    "knowledge-base.md",
    "faq.md",
]
_MD_EXTENSIONS = ["fenced_code", "tables", "toc", "sane_lists", "nl2br"]

_CSS = """\
:root{--bg:#fff;--c:#f7f8fa;--b:#e2e8f0;--t1:#1a202c;--t2:#4a5568;--t3:#a0aec0;--a:#3b82f6;--g:#22c55e;--y:#eab308;--r:#ef4444;--pk:#ec4899;--pu:#8b5cf6;--or:#f97316}
[data-theme="apple"][data-theme-mode="dark"]{--bg:#0f1117;--c:#1a1d28;--b:#2d3142;--t1:#f1f5f9;--t2:#94a3b8;--t3:#64748b}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--t1);margin:0;line-height:1.7;font-size:15px}
.wrap{max-width:1080px;margin:0 auto;padding:40px 24px}
h1{font-size:26px;border-bottom:3px solid var(--a);padding-bottom:10px;margin-top:40px}
h1:first-of-type{margin-top:0}
h2{font-size:20px;margin-top:32px;padding-left:12px;border-left:4px solid var(--a)}
h3{font-size:17px;margin-top:24px;color:var(--t2)}
p{margin:8px 0}
ul,ol{padding-left:24px}
li{margin:4px 0}
code{background:var(--c);padding:2px 6px;border-radius:4px;font-size:13px;font-family:"SF Mono",Monaco,monospace;color:var(--pk)}
pre{background:var(--c);padding:16px;border-radius:8px;overflow-x:auto;border:1px solid var(--b)}
pre code{background:none;padding:0;color:var(--t1)}
blockquote{margin:12px 0;padding:12px 16px;background:var(--c);border:1px solid var(--b);border-left:4px solid var(--a);border-radius:8px}
.blockquote.tip,blockquote.card.tip{border-left-color:var(--g);background:rgba(34,197,94,.08)}
.blockquote.warn,blockquote.card.warn{border-left-color:var(--y);background:rgba(234,179,8,.08)}
.card{background:var(--c);border:1px solid var(--b);border-radius:10px;padding:16px 20px;margin:12px 0}
.card-title{font-weight:700;font-size:16px;margin-bottom:8px}
.tip{border-left:4px solid var(--g);background:rgba(34,197,94,.08)}
.warn{border-left:4px solid var(--y);background:rgba(234,179,8,.08)}
.danger{border-left:4px solid var(--r);background:rgba(239,68,68,.08)}
.toc{position:sticky;top:20px;background:var(--c);border:1px solid var(--b);border-radius:10px;padding:16px 20px;font-size:13px}
.toc a{color:var(--t2);text-decoration:none;display:block;padding:3px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.toc a:hover{color:var(--a)}
.toc-group{margin-top:16px;padding-top:12px;border-top:1px solid var(--b);font-weight:700;color:var(--t1);font-size:13px}
.grid{display:grid;grid-template-columns:260px 1fr;gap:24px}
table{width:100%;border-collapse:collapse;margin:12px 0}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--b)}
th{background:var(--c);font-weight:600}
.back-top{position:fixed;bottom:30px;right:30px;background:var(--a);color:#fff;width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:pointer;border:none;font-size:18px;box-shadow:0 4px 12px rgba(0,0,0,.15)}
@media(max-width:768px){.grid{grid-template-columns:1fr}.toc{display:none}}
"""

_MIGRATION_COMMENT = """<!--
  ⚠️ 本文件为生成产物（migration note）：由 src/help_page_gen.py 依据 help/usage + help/api 渲染生成。
  如需更新内容，请编辑 help/ 目录下对应 markdown 文件后重启服务（help.html 会自动重新生成）。
-->"""

_API_SEP = '<hr style="margin:40px 0;border:none;border-top:1px solid var(--b)" />'


def render_markdown(text: str) -> str:
    """渲染单个 markdown 文档为 HTML（返回纯正文，不含页面壳）。"""
    md = markdown.Markdown(extensions=_MD_EXTENSIONS)
    body = md.convert(text)
    return body


def _render_file(path: Path) -> tuple[str, list]:
    md = markdown.Markdown(extensions=_MD_EXTENSIONS)
    body = md.convert(path.read_text(encoding="utf-8"))
    tokens = md.toc_tokens or []
    return body, tokens


def _prefix_heading_ids(body: str, tokens: list, prefix: str) -> str:
    """给当前文档的标题 id 加前缀，保证跨文档锚点唯一（各文档独立 Markdown 实例，slug 可能撞车）。"""
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


def _collect_links(tokens: list, prefix: str) -> list[str]:
    """把文档标题树拍平为侧边目录链接（含级联缩进，还原原 help.html 的层级目录观感）。"""
    links: list[str] = []

    def walk(nodes: list):
        for t in nodes:
            level = t.get("level", 1)
            name = _html.escape(t.get("name", ""))
            anchor = t["id"]
            if level == 1:
                links.append(f'<a href="#{prefix}{anchor}">{name}</a>')
            else:
                indent = min(12 * (level - 1) + 8, 28)
                links.append(
                    f'<a href="#{prefix}{anchor}" style="padding-left:{indent}px;font-size:12px;color:var(--t3)">{name}</a>'
                )
            walk(t.get("children", []))

    walk(tokens)
    return links


def _adorn_blockquotes(body: str) -> str:
    """把以 💡/⚠️ 开头的引用块还原成原 help.html 的提示卡片样式（增强，非阻塞）。"""

    def _repl(m):
        open_tag, inner = m.group(1), m.group(2)
        head = inner.lstrip()[:24]
        if "💡" in head or "TIP" in head.upper():
            return open_tag.replace("<blockquote>", '<blockquote class="card tip">')
        if any(k in head for k in ("⚠️", "⚠", "❌", "🚫", "注意", "安全")):
            return open_tag.replace("<blockquote>", '<blockquote class="card warn">')
        return open_tag

    return re.sub(r"(<blockquote>)(.*?)(</blockquote>)", _repl, body, flags=re.DOTALL)


def _docs(usage=True) -> list[tuple[Path, str]]:
    """返回 (path, 文件名) 有序列表。"""
    if usage:
        return [(USAGE_DIR / f, f) for f in USAGE_ORDER]
    return sorted(
        ((API_DIR / f, f) for f in (p.name for p in API_DIR.glob("*.md"))),
        key=lambda x: (x[1] != "README.md", x[1]),
    )


def build_help_html() -> str:
    """读 help/ 下全部 markdown，渲染为完整 help.html 字符串（UTF-8 无 BOM）。"""
    usage_links: list[str] = []
    usage_parts: list[str] = []
    api_links: list[str] = []
    api_parts: list[str] = []

    for path, fname in _docs(usage=True):
        if not path.is_file():
            continue
        body, tokens = _render_file(path)
        prefix = re.sub(r"[^0-9a-zA-Z]", "-", path.stem).strip("-") + "-"
        body = _prefix_heading_ids(body, tokens, prefix)
        body = _adorn_blockquotes(body)
        usage_links.extend(_collect_links(tokens, prefix))
        usage_parts.append(body)

    for path, fname in _docs(usage=False):
        if not path.is_file():
            continue
        body, tokens = _render_file(path)
        prefix = re.sub(r"[^0-9a-zA-Z]", "-", path.stem).strip("-") + "-"
        body = _prefix_heading_ids(body, tokens, prefix)
        body = _adorn_blockquotes(body)
        api_links.extend(_collect_links(tokens, prefix))
        api_parts.append(body)

    toc = _html.escape("目录")
    toc_html = (
        f'<div class="toc"><h3 style="margin-top:0">{toc}</h3>'
        f'<div class="toc-group">使用指南</div>'
        + ("".join(usage_links) if usage_links else '<a href="#">（无）</a>')
        + f'<div class="toc-group">API 参考</div>'
        + ("".join(api_links) if api_links else '<a href="#">（无）</a>')
        + "</div>"
    )
    content = (
        "".join(usage_parts)
        + _API_SEP
        + "".join(api_parts)
        if usage_parts and api_parts
        else "".join(usage_parts + api_parts)
    )

    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">\n'
        '<title>Grape 使用指导 &amp; FAQ</title>\n'
        '<link rel="icon" type="image/png" href="/static/logo-64.png">\n'
        f"{_MIGRATION_COMMENT}\n"
        "<style>"
        f"{_CSS}"
        "</style>\n</head>\n<body>\n"
        '<div class="wrap"><div class="grid"><div>'
        f"{toc_html}"
        "</div><div>"
        f"{content}"
        "</div></div></div>\n"
        '<button class="back-top" onclick="window.scrollTo({top:0,behavior:\'smooth\'})" title="回到顶部">\u2191</button>\n'
        "</body>\n</html>"
    )


def generate() -> int:
    """生成并覆写 src/api/static/help.html；返回渲染的 markdown 文件数。"""
    out_dir = OUT_PATH.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    html = build_help_html()
    OUT_PATH.write_text(html, encoding="utf-8")
    count = len([p for p, _ in _docs(usage=True) if p.is_file()]) + len(
        [p for p, _ in _docs(usage=False) if p.is_file()]
    )
    return count


if __name__ == "__main__":
    n = generate()
    print(f"help.html regenerated from {n} markdown sources -> {OUT_PATH}")