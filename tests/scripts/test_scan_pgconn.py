# -*- coding: utf-8 -*-
"""scan_pgconn 判定口径的看护用例（不依赖数据库）。"""
import importlib.util
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCANNER = os.path.join(ROOT, "scripts", "scan_pgconn.py")
OWNED = {"agent_runtimes"}

CASE_PG = '''
from src.kernel.session_manager import db, db_lock

def f(x):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM agent_runtimes WHERE id=%s", (x,))
'''

CASE_OWNER = '''
from src.kernel.owner_db import db_pair

def f(x):
    db, lock = db_pair("runtime")
    with lock:
        c = db.cursor()
        c.execute("SELECT status FROM agent_runtimes WHERE id=%s", (x,))
'''

CASE_DOMAIN = '''
from src.kernel.domain_db import cursor

def f(x):
    with cursor("runtime") as c:
        c.execute("SELECT status FROM agent_runtimes WHERE id=%s", (x,))
'''

CASE_FOREIGN_TABLE = '''
from src.kernel.session_manager import db

def f():
    c = db.cursor()
    c.execute("SELECT name FROM meta")
'''


def _load_scanner():
    spec = importlib.util.spec_from_file_location("scan_pgconn", SCANNER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scan(tmp_path, src, domain="runtime"):
    p = tmp_path / "case.py"
    p.write_text(src, encoding="utf-8")
    return _load_scanner().scan_file(str(p), "src/domains/runtime/case.py", domain, OWNED)


def test_pg_connection_site_detected(tmp_path):
    hits = _scan(tmp_path, CASE_PG)
    assert len(hits) == 1
    assert hits[0]["tables"] == "agent_runtimes"
    assert hits[0]["func"] == "f"


def test_owner_db_pair_excluded(tmp_path):
    assert _scan(tmp_path, CASE_OWNER) == []


def test_domain_cursor_excluded(tmp_path):
    assert _scan(tmp_path, CASE_DOMAIN) == []


def test_sql_on_foreign_table_ignored(tmp_path):
    # meta 不在 runtime 自有表集合 → 属跨域口径，由 foreign_table_refs 负责
    assert _scan(tmp_path, CASE_FOREIGN_TABLE) == []


def test_function_local_assignment_does_not_leak_into_module_env(tmp_path):
    # 函数体内的 `c = db.cursor()` 不得泄入模块环境：兄弟函数的同名参数游标来源未知，不应误报
    case = '''
from src.kernel.session_manager import db

def f(x):
    c = db.cursor()
    c.execute("SELECT status FROM agent_runtimes WHERE id=%s", (x,))

def h(c):
    c.execute("SELECT status FROM agent_runtimes WHERE id=%s", (1,))
'''
    hits = _scan(tmp_path, case)
    assert [h["func"] for h in hits] == ["f"]