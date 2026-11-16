"""D109 建表方言守卫：逐条 execute（走 mysql_compat）+ 部分唯一索引的 MySQL 等价。

反证过的两种错误形态（都真实发生过）：
1. 用 `executescript`（sqlite3 专有 API）→ 不经过 mysql_compat 翻译层，MySQL 下 1064；
2. 干脆在 `get_db_backend() == "mysql"` 时 `return` 跳过建表 → 迁移照样盖章（0ms），
   六表一张没建，D109 在 MySQL 部署上静默失效，且版本号盖死后永不再跑。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
DDL = SRC / "api" / "platform_object_ddl.py"
DB_SETUP = SRC / "api" / "db_setup.py"
FN = "_fn_create_platform_object_tables"


def _ddl_module():
    ns: dict = {}
    exec(compile(DDL.read_text(encoding="utf-8"), str(DDL), "exec"), ns)  # noqa: S102
    return ns


def _fn_node() -> ast.FunctionDef:
    tree = ast.parse(DB_SETUP.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == FN:
            return node
    raise AssertionError(f"db_setup.py 找不到 {FN}")


def _called_methods(node: ast.AST) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
            elif isinstance(f, ast.Name):
                out.add(f.id)
    return out


def test_ddl_splits_into_tables_without_partial_index():
    mod = _ddl_module()
    stmts = mod["iter_statements"]()
    tables = [s for s in stmts if re.match(r"\s*CREATE\s+TABLE\b", s, re.I)]
    indexes = [s for s in stmts if re.match(r"\s*CREATE\s+(UNIQUE\s+)?INDEX\b", s, re.I)]
    # D110：bot 账号目录退役（bot 归平台行），部分唯一索引随之删除
    assert len(tables) == 4, [s[:60] for s in tables]
    assert indexes == [], [s[:60] for s in indexes]


def test_setup_never_uses_executescript():
    """executescript 绕过 mysql_compat：MySQL 后端会 1064。"""
    assert "executescript" not in _called_methods(_fn_node())


def test_setup_does_not_skip_mysql_backend():
    """MySQL 分支不得直接 return（曾导致 0ms 盖章 + 建表未执行）。"""
    src = ast.get_source_segment(DB_SETUP.read_text(encoding="utf-8"), _fn_node()) or ""
    assert "跳过建表" not in src and "未适配" not in src, "建表函数仍在 MySQL 下跳过"
