#!/usr/bin/env python3
"""把 index.html 里静态引用的 ?v= 同步成产物内容的哈希 —— 幂等，可反复跑。

为什么需要它：`?v=` 原本是手工维护的整数。改了视图内容但忘了改数字，浏览器就会
继续用缓存里的旧文件，表现为「代码明明改了、页面没变」。产物文件名本身不带 hash
（vite 的 viewjs 输出是固定名 dashboard-view.js），所以缓存失效完全依赖这个查询参数。

做法：对 index.html 里每个 `viewjs/*.js?v=…` 和 `assets/*.css?v=…` 的引用，
取对应产物文件内容 md5 的前 8 位十六进制作为版本号；内容没变则不改（幂等），
变了就重写。哈希比递增整数更可靠：内容回滚时版本号也回滚，不会出现
"版本号涨了但文件是旧的" 这种自相矛盾的状态。

用法：
    python3 scripts/bump_asset_versions.py            # 改写 index.html
    python3 scripts/bump_asset_versions.py --check     # 只报告差异，不写；有差异时退出码 1
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HTML = REPO / "src/api/frontend/shell/index.html"
DIST = REPO / "src/api/frontend/static-dist"

# 只覆盖由构建产物提供的两类引用；其它 ?v=（如内联资源、第三方 CDN）不动。
PATTERN = re.compile(r'((?:viewjs|assets)/[A-Za-z0-9._-]+\.(?:js|css))\?v=([0-9a-zA-Z]+)')


def digest(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()[:8]


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    if not HTML.exists():
        print(f"FAIL: 找不到 {HTML}", file=sys.stderr)
        return 2
    if not DIST.is_dir():
        print(f"FAIL: 找不到产物目录 {DIST}（先 npm run build）", file=sys.stderr)
        return 2

    html = HTML.read_text(encoding="utf-8")
    seen: dict[str, str] = {}
    changes: list[tuple[str, str, str]] = []
    missing: list[str] = []

    def repl(m: re.Match[str]) -> str:
        rel, cur = m.group(1), m.group(2)
        asset = DIST / rel
        if not asset.is_file():
            missing.append(rel)
            return m.group(0)          # 产物缺失时保持原样，交由上层报错
        seen[rel] = cur
        new = digest(asset)
        if new == cur:
            return m.group(0)
        changes.append((rel, cur, new))
        return f"{rel}?v={new}"

    updated = PATTERN.sub(repl, html)
    changed = changes

    if missing:
        print("  WARN 产物缺失（无法计算版本号）:", ", ".join(sorted(set(missing))[:6]), file=sys.stderr)

    if not changed:
        print(f"  资产版本号已最新（{len(seen)} 条引用）")
        return 0

    for rel, old, new in changed:
        print(f"  {rel}: v={old} -> v={new}")

    if check_only:
        print(f"  --check: {len(changed)} 条需要更新（未写入）")
        return 1

    HTML.write_text(updated, encoding="utf-8")
    print(f"  已写入 {HTML.relative_to(REPO)}（{len(changed)} 条更新）")
    return 0


if __name__ == "__main__":
    sys.exit(main())