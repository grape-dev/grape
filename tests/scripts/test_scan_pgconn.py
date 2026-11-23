# -*- coding: utf-8 -*-
"""scan_pgconn 判定口径的看护用例（不依赖数据库）。"""
import importlib.util
import json
import os

import pytest

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


# ---- --check 退出码路径（rc=1 上升 / rc=2 fail-closed），隔离的 tmp 仓库 ----

CASE_PG_ONE = '''
from src.kernel.session_manager import db, db_lock

def f(x):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM agent_runtimes WHERE id=%s", (x,))
'''

CASE_PG_TWO = CASE_PG_ONE + '''

def g(x):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT name FROM agent_runtimes WHERE id=%s", (x,))
'''


def _make_repo(tmp_path, src, baseline_counts):
    (tmp_path / "src" / "domains" / "runtime").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "configs").mkdir()
    (tmp_path / "src" / "domains" / "runtime" / "case.py").write_text(src, encoding="utf-8")
    (tmp_path / "configs" / "table_ownership.json").write_text(
        json.dumps({"tables": {"agent_runtimes": "runtime"}}), encoding="utf-8")
    (tmp_path / "scripts" / "pgconn_baseline.json").write_text(
        json.dumps({"risk_sites": sum(baseline_counts.values()),
                    "risk_domains": {"runtime": sum(baseline_counts.values()), "insight": 0},
                    "files": baseline_counts}), encoding="utf-8")


def _run_check(tmp_path, monkeypatch):
    mod = _load_scanner()
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(mod, "MANIFEST", os.path.join(str(tmp_path), "configs", "table_ownership.json"))
    monkeypatch.setattr(mod, "BASELINE", os.path.join(str(tmp_path), "scripts", "pgconn_baseline.json"))
    return mod.main(["--check"])


def test_check_returns_1_when_grown(tmp_path, monkeypatch):
    # 基线 1 处 → 现 2 处：上升，退出码 1
    _make_repo(tmp_path, CASE_PG_TWO, {"src/domains/runtime/case.py": 1})
    assert _run_check(tmp_path, monkeypatch) == 1


def test_check_returns_0_when_not_grown(tmp_path, monkeypatch):
    _make_repo(tmp_path, CASE_PG_ONE, {"src/domains/runtime/case.py": 1})
    assert _run_check(tmp_path, monkeypatch) == 0


def test_check_fail_closed_when_manifest_missing(tmp_path, monkeypatch):
    # 清单缺失：fail-closed，退出码 2
    _make_repo(tmp_path, CASE_PG_ONE, {"src/domains/runtime/case.py": 1})
    os.remove(os.path.join(str(tmp_path), "configs", "table_ownership.json"))
    with pytest.raises(SystemExit) as ei:
        _run_check(tmp_path, monkeypatch)
    assert ei.value.code == 2


def test_check_fail_closed_when_baseline_missing(tmp_path, monkeypatch):
    # 基线缺失：fail-closed，退出码 2
    _make_repo(tmp_path, CASE_PG_ONE, {"src/domains/runtime/case.py": 1})
    os.remove(os.path.join(str(tmp_path), "scripts", "pgconn_baseline.json"))
    assert _run_check(tmp_path, monkeypatch) == 2


# ---- Issue #3857：门禁接线 + 真实读点回归锁 ----

CASE_CURSOR_FOR = '''
from src.kernel.db_registry import cursor_for

def f(x):
    with cursor_for("runtime") as c:
        c.execute("SELECT status FROM agent_runtimes WHERE id=%s", (x,))
'''


def test_cursor_for_is_recognized_as_domain_source(tmp_path):
    """`cursor_for("<域>")` 是内核唯一路由原语（三库缺 DSN 即 DomainUnavailable），
    属域连接来源，不得被当作进程库读。"""
    assert _scan(tmp_path, CASE_CURSOR_FOR) == []


def test_check_all_wires_pgconn_ratchet():
    """门禁必须真正接入 check-all。

    Issue #3857 复盘点：脚本与 `--check` 都对，但只在 `rds_gate.sh`（三库收口门禁）里
    接线，CI 的 check-all 完全不跑——engine-onboarding 这类新读点因此可静默合入。
    """
    path = os.path.join(ROOT, "scripts", "check-all.sh")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert "scan_pgconn.py --check" in text, "check-all 未接入进程库连接棘轮"


def test_engine_onboarding_not_flagged():
    """Issue #3857 回归锁：engine-onboarding 读 agent_runtimes 不得再被判为进程库读。

    该端点三库下曾用 mgmt 主库游标查 runtime 域表（1146 → remote_engines 恒空、
    needs_worker 恒 true）。单测 `--check` 只能拦「相对基线上升」，这里锁死具体读点。
    """
    mod = _load_scanner()
    owned = mod.load_owned()["runtime"]
    path = os.path.join(ROOT, "src", "domains", "runtime", "routes", "runtime.py")
    hits = mod.scan_file(path, "src/domains/runtime/routes/runtime.py", "runtime", owned)
    flagged = [h for h in hits if h["func"] == "runtime_engine_onboarding"]
    assert flagged == [], f"engine-onboarding 仍用进程库连接读 runtime 域表: {flagged}"