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


def test_business_does_not_exist_text_not_treated_as_schema_drift(monkeypatch, caplog):
    """「user does not exist」这类业务文案不得被误判成缺表降级。"""
    caplog.set_level("DEBUG")
    G._degraded_identity_ops.clear()
    G._degraded_identity_warned.clear()

    @contextmanager
    def _boom(*args):
        raise RuntimeError("user does not exist")
        yield  # pragma: no cover

    monkeypatch.setattr(G, "_uow", _boom)
    assert G.get_user_identities("u1") == []
    assert "Get user identities failed" in caplog.text
    assert "identity read degraded" not in caplog.text


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


def test_startup_self_check_reports_mgmt_domain_unavailable(monkeypatch, caplog):
    """QA R1 Optional：三库拓扑下 worker/event 看不到 GRAPE_DSN_MGMT 时必须启动即可诊断。

    `uow("mgmt")` 已固定域路由，缺 DSN 时 `route_of("mgmt")` 抛 DomainUnavailable，
    被 identity 读降级为 `[]`（等同「无身份」）。部署前置靠启动日志暴露：ERROR 含域名与
    DSN 变量名（remediation 指向 GRAPE_DSN_MGMT / GRAPE_3DB_ENV），且不阻断启动。
    """
    from src.api import db_setup
    from src.kernel import db_registry

    def _unavailable(domain):
        raise db_registry.DomainUnavailable(
            domain, "GRAPE_DSN_MGMT missing in a three-database topology")

    monkeypatch.setattr(db_registry, "route_of", _unavailable)

    caplog.set_level("WARNING")
    assert db_setup._check_identity_relations(relation_kind=lambda c, t: "table") == []
    msgs = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert msgs, "mgmt 域不可用必须打 ERROR（否则 worker/event 静默降级为无身份）"
    joined = " ".join(msgs)
    assert "mgmt" in joined
    assert "GRAPE_DSN_MGMT" in joined


def test_identity_uow_sites_use_mgmt_domain():
    """棘轮：grape_account_db 的 identity 读走 mgmt 域，禁止再落回进程主库。"""
    import inspect
    src = inspect.getsource(G)
    assert 'uow("mgmt")' in src or "_uow(_IDENTITY_DOMAIN)" in src
    assert "with _uow() as session" not in src


# ---------------------------------------------------------------- 5. 迁移 202611230013

def test_identity_relation_migration_creates_missing_table(tmp_path):
    """存量库缺基表 → 迁移幂等补建（含 status/unbound_at/token_expires_at 缺列）。"""
    import sqlite3

    from src.api import db_setup
    from src.domains.mgmt.migrations import external_identities_relation as M

    assert any(m.version == "202611230013" for m in db_setup._MIGRATIONS)

    conn = sqlite3.connect(tmp_path / "id.db")
    M.ensure_identity_relation(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(external_identities)").fetchall()]
    for col in ("status", "unbound_at", "token_expires_at", "provider", "external_id"):
        assert col in cols

    M.ensure_identity_relation(conn)          # 幂等重跑
    assert M.verify_identity_relation(conn) is None
    conn.close()


def test_migration_post_check_rejects_table_missing_columns(tmp_path):
    """QA R2 Nit：「表在、列缺」不得盖章——post_check 必须断言三列齐全。

    `_ensure_column` 未传 strict=True 时，MySQL 侧 INSTANT/INPLACE 均失败只登记
    `_DDL_MANUAL_REVIEW` 并正常返回（2026-11-06 issue.vcs_exists 事故同态），
    post_check 若只看「关系存在」会把列缺盖死成已完成 → 线上查询该列 1054 静默失败。
    """
    import sqlite3

    from src.domains.mgmt.migrations import external_identities_relation as M

    conn = sqlite3.connect(tmp_path / "id_cols.db")
    # 存量基表但缺 status/unbound_at/token_expires_at 三列
    conn.execute(
        "CREATE TABLE external_identities "
        "(id INTEGER PRIMARY KEY, provider TEXT, external_id TEXT)")
    with pytest.raises(RuntimeError) as ei:
        M.verify_identity_relation(conn)
    for col in ("status", "unbound_at", "token_expires_at"):
        assert col in str(ei.value), f"缺列 {col} 必须点名（否则无法定位补列失败）"
    conn.close()


def test_migration_column_ensure_is_strict(monkeypatch, tmp_path):
    """QA R2 Nit：补列必须走 `_ensure_column(strict=True)`——失败即抛，不许静默盖章。"""
    import sqlite3

    from src.api import db_setup
    from src.domains.mgmt.migrations import external_identities_relation as M

    conn = sqlite3.connect(tmp_path / "id_strict.db")
    conn.execute("CREATE TABLE external_identities (id INTEGER PRIMARY KEY)")

    seen = {}

    def _fail(c, tbl, col, ddl, **kwargs):
        seen["strict"] = kwargs.get("strict")
        raise RuntimeError("simulated: INSTANT 与 INPLACE,LOCK=NONE 均失败")

    monkeypatch.setattr(db_setup, "_ensure_column", _fail)
    with pytest.raises(RuntimeError):
        M.ensure_identity_relation(conn)      # 抛错=不盖章，下次启动重试
    assert seen.get("strict") is True, "_ensure_column 必须传 strict=True"
    conn.close()


def test_migration_post_check_accepts_view_without_columns(tmp_path):
    """视图形态的列是建视图时的快照、归部署流程管（本迁移不 ALTER 视图）。

    post_check 对 view 只断言关系存在——列断言仅针对基表形态。
    """
    import sqlite3

    from src.domains.mgmt.migrations import external_identities_relation as M

    conn = sqlite3.connect(tmp_path / "id_view.db")
    conn.execute("CREATE TABLE _src_3897 (x INTEGER)")
    conn.execute("CREATE VIEW external_identities AS SELECT x, x AS provider FROM _src_3897")
    assert M.verify_identity_relation(conn) is None
    conn.close()
