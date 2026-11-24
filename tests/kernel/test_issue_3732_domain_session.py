"""Issue #3732：mgmt 域 ORM 读路径按域取连接（域感知 engine/session 工厂）。

三库拓扑下 worker/event 进程的全局 engine 指向 runtime 库，mgmt 域表
（external_identities 等，归属见 configs/table_ownership.json）的 ORM 读必须
走 mgmt 域 DSN；读失败必须可分辨（strict），禁止空列表代表读失败。

测试通道说明：生产 DSN 仅 mysql（db_registry.parse_dsn 只接受 mysql scheme）；
sqlite scheme 在 get_domain_engine 里直通，仅用于双库 fixture 表达「另一个库」。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import pytest


UID = "route-user-3732"


@pytest.fixture(autouse=True)
def _reset_db_registry_state():
    """db_registry 的退避/拉黑是模块级状态，测试间必须复位（sqlite DSN 会让
    _new_conn 在裸 SQL 域路径上探活失败并进入 5s 退避，不影响 ORM 域路径）。"""
    from src.kernel import db_registry
    db_registry._retry_after.clear()
    db_registry._forbidden.clear()
    yield
    db_registry._retry_after.clear()
    db_registry._forbidden.clear()


@pytest.fixture()
def mgmt_engine(monkeypatch, tmp_path):
    """双 DSN fixture：GRAPE_DSN_MGMT 指向独立 sqlite 文件库（mgmt 域）。"""
    from src.kernel.persistence import engine as engine_mod
    mgmt_db = tmp_path / "mgmt.db"
    monkeypatch.setenv("GRAPE_DSN_MGMT", f"sqlite:///{mgmt_db}")
    eng = engine_mod.get_domain_engine("mgmt")
    yield eng
    eng.dispose()


def _seed_mgmt_identity(eng, user_id=UID, provider="gitcode", status=1,
                        access_token=""):
    """在 mgmt 域库（独立 sqlite 文件）落一条绑定行。"""
    from sqlalchemy.orm import sessionmaker
    from src.kernel.persistence.models import Base, ExternalIdentityModel
    Base.metadata.create_all(eng, tables=[ExternalIdentityModel.__table__])
    s = sessionmaker(bind=eng)()
    s.add(ExternalIdentityModel(id=f"id-{provider}", grape_user_id=user_id,
                                provider=provider, external_id="ext-1",
                                external_login=f"login-{provider}",
                                external_name="", external_avatar="",
                                access_token=access_token, refresh_token="",
                                token_expires_at="", metadata_json="{}",
                                status=status, created_at="2026-11-22"))
    s.commit()
    s.close()


# ---------------------------------------------------------------- 回落（单库零回归）

def test_domain_engine_falls_back_without_dsn(monkeypatch):
    """无域 DSN（单库/测试）→ get_domain_engine 回落进程全局 engine。"""
    monkeypatch.delenv("GRAPE_DSN_MGMT", raising=False)
    monkeypatch.delenv("GRAPE_DSN_RUNTIME", raising=False)
    monkeypatch.delenv("GRAPE_DSN_INSIGHT", raising=False)
    monkeypatch.delenv("GRAPE_DOMAIN_LIB", raising=False)
    from src.kernel.persistence.engine import get_domain_engine, get_engine
    assert get_domain_engine("mgmt") is get_engine()


def test_uow_noarg_unchanged():
    """uow() 无参语义不变：仍绑进程全局 engine。"""
    from src.kernel.persistence.engine import get_engine
    from src.kernel.persistence.uow import uow
    with uow() as session:
        assert session.bind is get_engine()


# ---------------------------------------------------------------- 路由（mysql DSN）

def test_domain_engine_routes_to_dsn_db(monkeypatch):
    """有 mgmt DSN → engine URL 指向 DSN 库（create_engine 不连库，无需真实例）。

    密码含 URL 保留字符（'@' / ':' / '/' / '#'）：DSN 由 quote_plus(password) 生成，
    断言与生成源同一变量——验的是「parse_dsn(unquote) → quote_plus → SQLAlchemy
    URL 还原」整条链（59 实锤教训），而不是某个写死的字面量，用例自身不存在
    「字面量与断言不一致」的空间。
    """
    from urllib.parse import quote_plus
    from src.kernel.persistence import engine as engine_mod

    password = "pa@ss:w/rd#1"        # 全部是 URL 保留字符
    dsn = (f"mysql://u:{quote_plus(password)}"
           "@db-host:3307/grape_prod_mgmt?charset=utf8mb4")
    monkeypatch.setenv("GRAPE_DSN_MGMT", dsn)

    from src.kernel import db_registry
    assert db_registry.parse_dsn(dsn)["password"] == password

    eng = engine_mod.get_domain_engine("mgmt")
    try:
        assert eng.url.drivername == "mysql+pymysql"
        assert eng.url.database == "grape_prod_mgmt"
        assert eng.url.host == "db-host"
        assert eng.url.port == 3307
        assert eng.url.password == password
        # 域 engine 按域缓存：二次取同一实例
        assert engine_mod.get_domain_engine("mgmt") is eng
    finally:
        eng.dispose()
        engine_mod.cache_clear()


# ---------------------------------------------------------------- 双 DSN fixture（核心）

def test_identity_reads_hit_mgmt_db_not_process_db(mgmt_engine):
    """worker/event 场景复现：进程主库无该用户数据（三库下是 runtime 库，必然
    1146 被吞成空列表），mgmt 域 DSN 库有真实绑定 → 三个读函数都应读到 mgmt 行。

    修复前（红）：ORM 读复用进程全局 engine → 查进程库 → 返回 []。
    """
    from src.domains.mgmt.auth.grape_account_db import (
        get_user_identities,
        get_user_identities_with_tokens,
        list_platform_links,
    )
    from src.kernel.session_manager import db, db_lock
    _seed_mgmt_identity(mgmt_engine)

    # 前置：进程主库确实没有该用户的绑定（模拟 runtime 库查无此表/无数据）
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM external_identities WHERE grape_user_id=?",
                  (UID,))
        assert c.fetchone()[0] == 0

    idents = get_user_identities_with_tokens(UID)
    assert [i["provider"] for i in idents] == ["gitcode"], \
        "ORM 读未路由到 mgmt 域库（仍打进程主库，1146 会被吞成空列表）"
    assert idents[0]["external_login"] == "login-gitcode"

    idents2 = get_user_identities(UID)
    assert [i["provider"] for i in idents2] == ["gitcode"]

    links = list_platform_links(UID)
    routed = [l for l in links if l.get("provider") == "gitcode"]
    assert routed and routed[0]["source"] == "identity"


# ---------------------------------------------------------------- strict 失败通道

def test_with_tokens_lenient_vs_strict(monkeypatch, tmp_path):
    """mgmt 侧缺表（复现 1146 场景）：默认宽容返回 []；strict=True 必须重抛——
    调用方据此区分「确实没绑定」与「读不出来」，禁止空列表代表读失败。"""
    monkeypatch.setenv("GRAPE_DSN_MGMT",
                       f"sqlite:///{tmp_path / 'mgmt_empty.db'}")
    from src.domains.mgmt.auth.grape_account_db import get_user_identities_with_tokens
    assert get_user_identities_with_tokens(UID) == []
    with pytest.raises(Exception):
        get_user_identities_with_tokens(UID, strict=True)


def test_platform_links_strict_reports_token_scan_degraded(monkeypatch, tmp_path):
    """复审 Optional-1 验收：token 扫描失败必须能被 strict=True 的调用方看到
    （degraded: token_scan）。此前 token 扫描是宽容调用，_degraded 里的 token_scan
    分支不可达——域库读不出来时 _tokens 为空集，身份行被判定 token_usable=False，
    把「读不出来」渲染成「授权失效」。"""
    monkeypatch.setenv("GRAPE_DSN_MGMT",
                       f"sqlite:///{tmp_path / 'mgmt_empty_links.db'}")
    from src.domains.mgmt.auth.grape_account_db import list_platform_links
    # 默认宽容：不抛错（现有语义不变）
    assert list_platform_links(UID) == []
    with pytest.raises(RuntimeError) as ei:
        list_platform_links(UID, strict=True)
    assert "token_scan" in str(ei.value)


def test_caller_degrade_reason_visible(monkeypatch, tmp_path, caplog):
    """repo_roles 读失败 fail-closed 返回 ""，但降级原因必须显式可见
    （mgmt_read_failed），不得静默把读失败当「未绑定」。"""
    import logging
    monkeypatch.setenv("GRAPE_DSN_MGMT",
                       f"sqlite:///{tmp_path / 'mgmt_empty.db'}")
    from src.domains.mgmt.auth.repo_roles import _get_platform_login
    with caplog.at_level(logging.WARNING):
        assert _get_platform_login({"id": UID}, "gitcode") == ""
    assert any("mgmt_read_failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- 启动自检

def _fake_conn_factory(has_table=True):
    class _FakeCursor:
        def __init__(self):
            self.queries = []
        def execute(self, sql, params=None):
            self.queries.append((sql, params))
            return self
        def fetchone(self):
            return ("yes",) if has_table else None
        def close(self):
            pass
    class _FakeConn:
        def __init__(self):
            self.cur = _FakeCursor()
        def cursor(self):
            return self.cur
        def close(self):
            pass
    return _FakeConn()


def test_startup_check_flags_table_residency_mismatch(monkeypatch):
    """负向用例：DSN 指向的库缺本域关键表（external_identities）→ 启动告警。"""
    from src.kernel import db_registry
    monkeypatch.setenv("GRAPE_DSN_MGMT",
                       "mysql://u:p@db-host:3307/grape_prod_mgmt")
    monkeypatch.setattr(db_registry, "new_connection",
                        lambda dom, autocommit=True: _fake_conn_factory(False))
    problems = db_registry.warn_domain_table_residency("mgmt")
    assert any("external_identities" in p for p in problems)


def test_startup_check_flags_worker_missing_mgmt_dsn(monkeypatch):
    """负向用例：多库拓扑下 worker 缺 GRAPE_DSN_MGMT → 告警（1146 复发前兆）。"""
    from src.kernel import db_registry
    monkeypatch.setenv("GRAPE_DSN_RUNTIME",
                       "mysql://u:p@db-host:3307/grape_prod_runtime")
    monkeypatch.delenv("GRAPE_DSN_MGMT", raising=False)
    monkeypatch.setattr(db_registry, "new_connection",
                        lambda dom, autocommit=True: _fake_conn_factory(True))
    problems = db_registry.warn_domain_table_residency("worker")
    assert any("GRAPE_DSN_MGMT" in p for p in problems)


def test_startup_check_silent_in_single_db(monkeypatch):
    """单库（无任何 DSN）→ 自检 no-op，不告警不阻断。"""
    from src.kernel import db_registry
    for k in ("GRAPE_DSN_MGMT", "GRAPE_DSN_RUNTIME", "GRAPE_DSN_INSIGHT"):
        monkeypatch.delenv(k, raising=False)
    assert db_registry.warn_domain_table_residency("worker") == []
