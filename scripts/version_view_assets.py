#!/usr/bin/env python3
"""构建后版本同步：让「视图 JS + 视图 CSS + 运行时缓存版本」永远同源。

为什么需要：Vue 单文件组件的 scoped 样式会带 `data-v-<hash>`，该 hash 每次构建
都可能变；而 shell/index.html 里的 `all-views.css?v=N` 若手写不动，浏览器会继续
用旧 CSS，与新的 JS 组件 hash 对不上 → 样式整块失效（实测：下拉框塌成一个点、
提示文案是旧的）。这里以构建产物的内容摘要作为版本号，一次写完三处：

  1. index.html  -> all-views.css?v=<sha8>
  2. app_cache_ver -> '<sha8>'（spa.py / server_common.py / shell/js/app.js）

用法：npm run build 之后执行 `python3 scripts/sync_view_versions.py`。
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = ROOT / "src/api/frontend/static-dist/assets/viewcss/all-views.css"
INDEX = ROOT / "src/api/frontend/shell/index.html"
VERSION_HOSTS = [
    ROOT / "src/api/spa.py",
    ROOT / "src/api/server_common.py",
    ROOT / "src/api/frontend/shell/js/app.js",
]


def main() -> int:
    if not CSS.exists():
        print(f"[sync] 缺少构建产物: {CSS}", file=sys.stderr)
        return 1
    ver = hashlib.sha256(CSS.read_bytes()).hexdigest()[:8]

    html = INDEX.read_text(encoding="utf-8")
    new_html, n_html = re.subn(r"all-views\.css\?v=[0-9A-Za-z]+",
                               f"all-views.css?v={ver}", html)
    if n_html == 0:
        print("[sync] index.html 未找到 all-views.css?v= 引用", file=sys.stderr)
        return 1
    INDEX.write_text(new_html, encoding="utf-8")

    for host in VERSION_HOSTS:
        text = host.read_text(encoding="utf-8")
        updated, n = re.subn(r"app_cache_ver', '[0-9A-Za-z]+'",
                             f"app_cache_ver', '{ver}'", text)
        host.write_text(updated, encoding="utf-8")
        print(f"[sync] {host.relative_to(ROOT)}: {n} 处 -> {ver}")

    print(f"[sync] 版本统一为 {ver}（css {n_html} 处）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())