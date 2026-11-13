"""Issue #2092 护栏：worker 随包模块的第三方依赖必须被 requirements-worker.txt 声明。

背景（上游 #11 / GitCode #2092）：`src/kernel/worker_package.py` 的 MANIFEST 包含
`src/kernel/mysql_compat.py`，它模块级 `import pymysql`；而 requirements-worker.txt 只声明了
aiohttp/bcrypt/click/httpx → 按文档装 worker，一 import 就 ModuleNotFoundError。

钉两层：
1. worker 启动/执行链上模块的**模块级**第三方 import 必须在依赖清单里；
2. mysql_compat 的两处具体依赖：模块级 pymysql、懒加载 DBUtils（MySQL 连接池）。

注：MANIFEST 里 `src/agent/`、`src/domains/` 等还有若干模块级第三方 import（PIL / rapidocr /
prometheus-client / fastapi 等），不在 worker 启动链上，属独立依赖面，不在本 issue 范围。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQ = ROOT / "requirements-worker.txt"

# worker 启动 → 执行链（MANIFEST 里 import 期会求值的那批）
HOT_PATH = [
    "src/__init__.py",
    "src/kernel/config.py",
    "src/kernel/prompt_builder.py",
    "src/kernel/session_manager.py",
    "src/kernel/mysql_compat.py",
    "src/api/time_utils.py",
    "src/api/session_store.py",
]

DIST_MAP = {
    "pymysql": "pymysql",
    "dbutils": "dbutils",
    "aiohttp": "aiohttp",
    "bcrypt": "bcrypt",
    "click": "click",
    "httpx": "httpx",
    "yaml": "PyYAML",
    "jwt": "PyJWT",
    # opentelemetry 的 import 根名由 opentelemetry-sdk/api 提供，清单按发行包声明
    "opentelemetry": "opentelemetry-sdk",
}


def _declared_dists() -> set[str]:
    out = set()
    for line in REQ.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!\[;]", line, 1)[0].strip().lower()
        if name:
            out.add(name)
    return out


def _module_level_roots(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    nodes: list[ast.AST] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Try):
            continue
        nodes.extend(ast.walk(stmt))
    roots = []
    for node in nodes:
        if isinstance(node, ast.Import):
            roots += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.append(node.module.split(".")[0])
    return roots


def test_worker_hot_path_imports_are_declared():
    declared = _declared_dists()
    stdlib = set(sys.stdlib_module_names)
    missing: dict[str, list[str]] = {}
    for rel in HOT_PATH:
        p = ROOT / rel
        if not p.is_file():
            continue
        for root in _module_level_roots(p):
            if root in stdlib or root == "src":
                continue
            dist = DIST_MAP.get(root, root).lower()
            if dist not in declared:
                missing.setdefault(dist, []).append(rel)
    assert not missing, (
        "worker 启动链模块 import 的第三方包未在 requirements-worker.txt 声明：\n"
        + "\n".join(f"  - {d}: {sorted(set(f))}" for d, f in sorted(missing.items()))
    )


def test_pymysql_declared_for_mysql_compat():
    text = (ROOT / "src/kernel/mysql_compat.py").read_text(encoding="utf-8", errors="replace")
    assert re.search(r"^import pymysql\b", text, re.M), "mysql_compat 仍是模块级 import pymysql（回归断言）"
    assert "pymysql" in _declared_dists(), "requirements-worker.txt 必须声明 pymysql"


def test_dbutils_declared_for_mysql_pool():
    text = (ROOT / "src/kernel/mysql_compat.py").read_text(encoding="utf-8", errors="replace")
    assert "from dbutils.pooled_db import PooledDB" in text
    assert "dbutils" in _declared_dists(), "requirements-worker.txt 必须声明 DBUtils（提供 dbutils）"
