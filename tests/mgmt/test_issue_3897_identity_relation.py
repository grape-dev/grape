"""Issue #3897 B 组：external_identities 缺表（1146）的域路由 / 降级 / 启动自检。

现场（生产 Loki，30min 抽样 200 条 warning 命中 113 条）：

    Get user identities with tokens failed: (pymysql.err.ProgrammingError)
    (1146, "Table 'grape_prod_runtime.external_identities' doesn't exist")

代码级根因：`src/kernel/persistence/uow.py::uow()` 固定绑 `get_engine()`（进程主库），
绕过了 `db_registry.route_of("mgmt")` 的域路由。worker/event 进程主库是 runtime 库，
identity 表（owner=mgmt）在 runtime 只是跨域视图，而该视图从未被补建
（不在 `scripts/view_create_missing_allowlist.txt`）→ 每次身份解析 1146 + 一条 WARNING。

本文件锁四条契约：
  1. `uow("mgmt")` 走域引擎（`get_domain_engine`），默认 `uow()` 保持进程主库（零回归）；
  2. 缺表/域不可用 → 每次调用返回 []（非致命），同一操作只 WARN 一次（其余 DEBUG），
     且 WARN 带 remediation；
  3. `schema_probe.relation_kind` 双后端可用（sqlite 表/缺失；MySQL 走 information_schema）；
  4. 启动自检 `db_setup._check_identity_relations` 缺失时 ERROR 且不抛异常。
"""
import os
import sys

import pytest
from contextlib import contextmanager
from sqlalchemy.exc import ProgrammingError

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from src.kernel.persistence import uow as uow_mod
from src.domains.mgmt.auth import grape_account_db as G
from src.domains.runtime import schema_probe


# ---------------------------------------------------------------- 1. 域路由

class _FakeSession:
    def __init__(self):
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _patch_sessionmaker(monkeypatch, seen: dict):
    class _Factory:
        def __call__(self):
            return _FakeSession()

    def _sessionmaker(bind, **kwargs):
        seen["bind"] = bind
        return _Factory()

    monkeypatch.setattr(uow_mod, "sessionmaker", _sessionmaker, raising=True)


def test_uow_routes_identity_domain_to_domain_engine(monkeypatch):
    """`uow("mgmt")` 必须绑 mgmt 域引擎——否则三库拓扑下会打到进程库（1146 根因）。"""
    seen = {}
    _patch_sessionmaker(monkeypatch, seen)
    monkeypatch.setattr(uow_mod, "get_engine", lambda: "MAIN")
    monkeypatch.setattr(uow_mod, "get_domain_engine", lambda d: f"DOMAIN:{d}")

    with uow_mod.uow("mgmt") as s:
        assert isinstance(s, _FakeSession)

    assert seen["bind"] == "DOMAIN:mgmt"
    assert s.committed and s.closed


def test_uow_default_keeps_process_db(monkeypatch):
    """默认 `uow()` 行为零变化（单库/本域进程不新开连接）。"""
    seen = {}
    _patch_sessionmaker(monkeypatch, seen)
    monkeypatch.setattr(uow_mod, "get_engine", lambda: "MAIN")
    monkeypatch.setattr(uow_mod, "get_domain_engine", lambda d: f"DOMAIN:{d}")

    with uow_mod.uow():
        pass

    assert seen["bind"] == "MAIN"


# ---------------------------------------------------------------- 2. 缺表降级

def _missing_table_error():
    return ProgrammingError(
        "SELECT ...",
        {},
        Exception(1146, "Table 'grape_prod_runtime.external_identities' doesn't exist"))


@contextmanager
def _raising_uow(*args):
    raise _missing_table_error()
    yield  # pragma: no cover


def test_missing_relation_degrades_to_empty_and_warns_once(monkeypatch, caplog):
    """1146 → 返回 []（链路不中断），同操作只 1 条 WARNING（其余 DEBUG），含 remediation。"""
    caplog.set_level("DEBUG")
    G._degraded_identity_ops.clear()
    G._degraded_identity_warned.clear()
    monkeypatch.setattr(G, "_uow", _raising_uow)

    for _ in range(5):
        assert G.get_user_identities_with_tokens("u1") == []
        assert G.get_user_identities("u1") == []

    warns = [r for r in caplog.records
             if r.levelname == "WARNING" and "identity read degraded" in r.getMessage()]
    assert len(warns) == 2          # 两个不同的 op 各 1 条
    assert any("get_user_identities_with_tokens" in r.getMessage() for r in warns)
    assert all("domain_view_freshness.py" in r.getMessage() for r in warns)
    # 后续调用降为 DEBUG（不再刷 warning）
    assert len([r for r in caplog.records
                if r.levelname == "DEBUG" and "identity read degraded" in r.getMessage()]) >= 3


def test_non_relation_errors_keep_existing_warning(monkeypatch, caplog):
    """非缺表类错误（如解密/连接业务异常）维持既有 WARNING 语义，不被降级吞掉。"""
    caplog.set_level("DEBUG")
    G._degraded_identity_ops.clear()
    G._degraded_identity_warned.clear()

    @contextmanager
    def _boom(*args):
        raise RuntimeError("decrypt failed")
        yield  # pragma: no cover

    monkeypatch.setattr(G, "_uow", _boom)
    assert G.get_user_identities_with_tokens("u1") == []
    text = caplog.text
    assert "Get user identities with tokens failed" in text
    assert "identity read degraded" not in text


# ---------------------------------------------------------------- 3. relation 探针

def test_relation_kind_detects_existing_and_missing_sqlite():
    from src.kernel.session_manager import db

    c = db.cursor()
    assert schema_probe.relation_kind(c, "external_identities") == "table"
    assert schema_probe.relation_kind(c, "no_such_relation_3897") is None


# ---------------------------------------------------------------- 4. 启动自检

def test_startup_self_check_reports_missing_relation(caplog):
    from src.api import db_setup

    caplog.set_level("WARNING")
    missing = db_setup._check_identity_relations(
        relation_kind=lambda c, t: "table" if t == "grape_accounts" else None)
    assert missing == ["external_identities"]
    errs = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errs, "缺关系必须打 ERROR（启动期可见）"
    assert "external_identities" in errs[0].getMessage()
    assert "domain_view_freshness.py" in errs[0].getMessage()


def test_startup_self_check_never_raises(caplog):
    from src.api import db_setup

    def _boom(c, t):
        raise RuntimeError("probe exploded")

    assert db_setup._check_identity_relations(relation_kind=_boom) == []


def test_identity_uow_sites_use_mgmt_domain():
    """棘轮：grape_account_db 的 identity 读走 mgmt 域，禁止再落回进程主库。"""
    import inspect
    src = inspect.getsource(G)
    assert 'uow("mgmt")' in src or "_uow(_IDENTITY_DOMAIN)" in src
    assert "with _uow() as session" not in src