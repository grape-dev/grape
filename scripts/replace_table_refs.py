#!/usr/bin/env python3
"""表名引用批量替换工具（Issue #699 Phase 2 通道 B，渐进采用）。

把指定 Python 文件中 SQL 字符串里表名语法位置（FROM/JOIN/INTO/UPDATE/TABLE/TO 后）
的注册表裸表名替换为 {t_xxx('name')} f-string 插值，并补 import。行为与
kernel/db.qualify_sql 运行时改写等价，用于把热点模块逐步迁移为显式 t_*() 调用
（Phase 5 契约收口配套）。默认仅检查（--check），不写文件。

⚠️ 前置条件：写回（非 --check）后的代码要求目标库已开启 GRAPE_SCHEMA_MODE=1
（物理表已带 mgmt_/rt_/ins_ 前缀或 grape_x. 限定名）。t_*() 遵循 schema_mode_enabled()：
模式关闭时返回裸表名、模式开启时返回前缀/限定名，因此写回产物在两种模式下均可运行，
但若意图使用分 Schema 行为（前缀表），须先执行 scripts/migrate_schemas.py 迁移再开启开关。

用法：
  python scripts/replace_table_refs.py --check src/api/db_setup.py
  python scripts/replace_table_refs.py src/api/db_setup.py      # 写回
  python scripts/replace_table_refs.py --check --list src/      # 目录扫描统计
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.kernel.db import SCHEMA_MGMT, SCHEMA_RUNTIME, SCHEMA_INSIGHT, TABLE_REGISTRY  # noqa: E402

_FUNC = {SCHEMA_MGMT: "t_mgmt", SCHEMA_RUNTIME: "t_runtime", SCHEMA_INSIGHT: "t_insight"}
_IMPORT_RE = re.compile(r"^from src\.kernel\.db import (.+)$", re.M)

_KEYWORDS = r"FROM|JOIN|INTO|UPDATE|TABLE|TO"

# 字符串字面量（含可选 f/r/b 前缀；单/双引号）
_STR_RE = re.compile(r"""(?P<prefix>[fFrRbB]{0,2})(?P<q>["'])(?P<body>(?:\\.|(?!\2).)*)(?P=q)""")
# 字面量内部的表名语法位置
_INNER_RE = re.compile(r"(?i)\b(" + _KEYWORDS + r")(\s+)(?:IF\s+NOT\s+EXISTS\s+)?(\w+)")


def _rewrite_inner(inner: str) -> tuple[str, int]:
    """把字符串字面量内部表名语法位置的注册表表名改写为 {t_x('name')} 插值。"""
    count = 0

    def _sub(m):
        nonlocal count
        kw, ws, name = m.group(1), m.group(2), m.group(3)
        schema = TABLE_REGISTRY.get(name)
        if not schema or "'" in inner or '"' in inner:
            return m.group(0)
        count += 1
        return f"{kw}{ws}{{{_FUNC[schema]}('{name}')}}"

    return _INNER_RE.sub(_sub, inner), count


def rewrite_source(src: str) -> tuple[str, int]:
    """重写源码：SQL 字符串字面量内的表名语法位置表名 → {t_x('name')} 插值。"""
    out = []
    pos = 0
    count = 0
    for m in _STR_RE.finditer(src):
        out.append(src[pos:m.start()])
        prefix, q, body = m.group("prefix"), m.group("q"), m.group("body")
        if any(c in prefix for c in "rRbB") or "{" in body or "}" in body:
            # raw/bytes 字面量或已含 f-string 插值：保守跳过
            out.append(m.group(0))
        else:
            new_body, n = _rewrite_inner(body)
            if n:
                count += n
                out.append("f" + q + new_body + q)
            else:
                out.append(m.group(0))
        pos = m.end()
    out.append(src[pos:])
    new_src = "".join(out)
    if count:
        new_src = _add_import(new_src, _used_funcs(new_src))
    return new_src, count


def _used_funcs(src: str) -> set:
    used = set()
    for fn in ("t_mgmt", "t_runtime", "t_insight"):
        if re.search(r"\{\s*" + fn + r"\(", src):
            used.add(fn)
    return used


def _add_import(src: str, funcs) -> str:
    funcs = sorted(funcs)
    m = _IMPORT_RE.search(src)
    if m:
        have = {f.strip() for f in m.group(1).split(",")}
        have |= set(funcs)
        return src[: m.start(1)] + ", ".join(sorted(have)) + src[m.end(1):]
    # 在最后一个顶层 import 后插入
    lines = src.split("\n")
    last_import = 0
    in_doc = False
    for i, line in enumerate(lines[:200]):
        if line.startswith(('"""', "'''")):
            in_doc = not in_doc if line.count(line[:3]) == 1 else in_doc
            continue
        if not in_doc and (line.startswith("import ") or line.startswith("from ")):
            last_import = i
    lines.insert(last_import + 1, "from src.kernel.db import " + ", ".join(funcs))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Issue #699 table-ref codemod")
    ap.add_argument("paths", nargs="+", help="files or dirs")
    ap.add_argument("--check", action="store_true", help="report only, do not write")
    ap.add_argument("--list", action="store_true", help="list matching files only")
    args = ap.parse_args()

    total = 0
    files_changed = 0
    for p in args.paths:
        if os.path.isdir(p):
            pyfiles = []
            for root, _, fs in os.walk(p):
                if any(x in root for x in ("node_modules", ".git", "static")):
                    continue
                pyfiles += [os.path.join(root, f) for f in fs if f.endswith(".py")]
        else:
            pyfiles = [p]
        for fp in pyfiles:
            try:
                src = open(fp, encoding="utf-8").read()
            except Exception:
                continue
            new_src, n = rewrite_source(src)
            if n:
                total += n
                files_changed += 1
                if args.list:
                    print(f"{fp}: {n}")
                elif args.check:
                    print(f"would rewrite {n} ref(s): {fp}")
                elif new_src != src:
                    with open(fp, "w", encoding="utf-8") as f:
                        f.write(new_src)
                    print(f"rewrote {n} ref(s): {fp}")
    print(f"total: {total} ref(s) in {files_changed} file(s){' (check mode)' if args.check else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
