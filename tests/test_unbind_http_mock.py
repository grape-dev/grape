"""解绑 GitCode 全流程场景用例 —— mock 版（不连任何真实数据库，不起服务进程）。

对应 docs/weekly-08/mysql-dialect-compat-requirements.md §五 S1：
- S1-U1 confirm=false → 409 + warehouses（路径 A + 路径 B 合并）
- S1-U2 confirm=true → 200 + 绑定软删 + user_repos 清理 + 审计
- S1-U3 affected_repos 为真实数量（查询失败 → -1 不静默归零）
- S1-U4 无密码单绑定 → 400 need_set_password
- S1-U5 未知 provider → 400 白名单

mock 策略：
- FastAPI TestClient + dependency_overrides(require_jwt_only) 免登录；
- MockDB（tests/mock_db.py）按 SQL 子串注册结果集，验证"发出的 SQL"是
  标准参数化 LIKE（无 ESCAPE 反斜杠）——方言行为在 mock 层即被锁定；
- 数据访问层函数（delete_external_identity 等）monkeypatch 为可观测桩。
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mock_db import MockDB, install_mock_db


@pytest.fixture()
def app_and_client(monkeypatch):
    from src.api.routes.grape_auth import router, unbind_provider
    from src.api.deps import require_jwt_only

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_jwt_only] = lambda: {"id": 33, "username": "mock_user"}
    return app, TestClient(app)


def _mock_unbind_db(monkeypatch, *, path_a_rows=(), path_b_rows=(), count_rows=((0,),),
                    ident_deleted=True, has_pwd=True, ident_count=2):
    """装配解绑链路的 MockDB 与数据访问桩，返回 (mock_db, calls)。"""
    db = MockDB()
    # 注册顺序即匹配优先级：COUNT 查询也含 INNER JOIN 子串，须先注册
    db.register("SELECT COUNT(*) FROM managed_repos", lambda sql, p: [tuple(r) for r in count_rows])
    db.register("INNER JOIN user_repos", lambda sql, p: [tuple(r) for r in path_a_rows])
    db.register("WHERE host_user_id=?", lambda sql, p: [tuple(r) for r in path_b_rows])
    db.register("DELETE FROM user_repos", [])
    db.register("UPDATE unified_user_mapping", [])
    install_mock_db(monkeypatch, db)

    calls = {}
    import src.auth.grape_account_db as gad
    monkeypatch.setattr(gad, "delete_external_identity",
                        lambda uid, p, **kw: calls.setdefault("deleted", []).append((uid, p)) or ident_deleted)
    monkeypatch.setattr(gad, "count_user_identities", lambda uid: ident_count)
    monkeypatch.setattr(gad, "has_password", lambda uid: has_pwd)
    monkeypatch.setattr(gad, "get_user_identities", lambda uid: [])
    monkeypatch.setattr(gad, "clear_placeholder_email", lambda uid, p: True)
    # 路由内 from src.user import db as user_db 后调用 delete_credential
    import src.user.db as udb
    monkeypatch.setattr(udb, "delete_credential", lambda uid, key: None)
    import src.api.routes.grape_auth as ga
    monkeypatch.setattr(ga, "delete_external_identity", gad.delete_external_identity)
    monkeypatch.setattr(ga, "count_user_identities", gad.count_user_identities)
    monkeypatch.setattr(ga, "has_password", gad.has_password)
    monkeypatch.setattr(ga, "get_user_identities", gad.get_user_identities)
    # 审计桩
    import src.auth.audit as audit
    monkeypatch.setattr(audit, "log_audit", lambda *a, **kw: calls.setdefault("audit", []).append((a, kw)))
    return db, calls


# ---------------------------------------------------------------- S1-U1

def test_s1_u1_unbind_confirm_false_returns_409_with_warehouses(app_and_client, monkeypatch):
    app, client = app_and_client
    _mock_unbind_db(monkeypatch,
                    path_a_rows=[("gitcode:mock-user/repo-a",)],
                    path_b_rows=[("gitcode:mock-user/repo-b",)])
    r = client.delete("/api/auth/bind/gitcode?confirm=false")
    assert r.status_code == 409
    body = r.json()["detail"]
    assert set(body["warehouses"]) == {"gitcode:mock-user/repo-a", "gitcode:mock-user/repo-b"}
    assert body["need_confirm"] is True


def test_s1_u1_sql_is_standard_parameterized_like(app_and_client, monkeypatch):
    """方言锁定：发出的 SQL 必须是纯参数化 LIKE（无 ESCAPE 反斜杠）。"""
    app, client = app_and_client
    db, _ = _mock_unbind_db(monkeypatch, path_a_rows=[("gitcode:x/y",)])
    client.delete("/api/auth/bind/gitcode?confirm=false")
    joins = [sql for sql, _ in db.cursor().queries if "INNER JOIN user_repos" in sql]
    assert joins, db.cursor().queries
    for sql in joins:
        assert "ESCAPE" not in sql and "\\" not in sql, sql
        assert "LIKE ?" in sql, sql
    # 参数是常量前缀，无转义包装
    like_params = [p for sql, p in db.cursor().queries if "LIKE ?" in sql]
    assert ("gitcode:%",) in like_params or any(p[-1] == "gitcode:%" for p in like_params if p)


# ---------------------------------------------------------------- S1-U2

def test_s1_u2_unbind_confirm_true_full_flow(app_and_client, monkeypatch):
    app, client = app_and_client
    db, calls = _mock_unbind_db(monkeypatch, count_rows=((2,),))
    r = client.delete("/api/auth/bind/gitcode?confirm=true")
    assert r.status_code == 200
    body = r.json()
    assert body["message"] == "解绑成功"
    assert body["affected_repos"] == 2            # S1-U3
    assert body["partial"] is False
    assert calls.get("deleted") == [(33, "gitcode")]  # 绑定软删
    assert calls.get("audit"), "审计缺失"           # 审计记录
    # user_repos 清理已执行且是标准 LIKE
    dels = [sql for sql, _ in db.cursor().queries if sql.startswith("DELETE FROM user_repos")]
    assert dels and "ESCAPE" not in dels[0] and "LIKE ?" in dels[0]
    # um 列清理已执行
    assert any("UPDATE unified_user_mapping" in sql for sql, _ in db.cursor().queries)


def test_s1_u2_user_repos_failure_partial_visible(app_and_client, monkeypatch):
    """F3：user_repos 清理失败 → 200 但 partial=True（不静默半途状态）。"""
    app, client = app_and_client
    db, calls = _mock_unbind_db(monkeypatch)

    class FailingCursor(db.cursor().__class__):
        def execute(self, sql, params=()):
            if sql.startswith("DELETE FROM user_repos"):
                raise RuntimeError("mock db failure")
            return super().execute(sql, params)

    monkeypatch.setattr(db, "cursor", lambda: FailingCursor(db), raising=False)
    # 重新安装使路由使用同一个 db 对象
    r = client.delete("/api/auth/bind/gitcode?confirm=true")
    assert r.status_code == 200
    assert r.json()["partial"] is True


# ---------------------------------------------------------------- S1-U3（失败不归零）

def test_s1_u3_affected_count_failure_returns_unknown(app_and_client, monkeypatch):
    app, client = app_and_client
    db, _ = _mock_unbind_db(monkeypatch)

    class FailingCursor(db.cursor().__class__):
        def execute(self, sql, params=()):
            if sql.startswith("SELECT COUNT(*) FROM managed_repos"):
                raise RuntimeError("mock count failure")
            return super().execute(sql, params)

    monkeypatch.setattr(db, "cursor", lambda: FailingCursor(db), raising=False)
    r = client.delete("/api/auth/bind/gitcode?confirm=true")
    assert r.status_code == 200
    assert r.json()["affected_repos"] == -1  # 未知，不是 0


# ---------------------------------------------------------------- S1-U4

def test_s1_u4_last_login_method_guard(app_and_client, monkeypatch):
    app, client = app_and_client
    _mock_unbind_db(monkeypatch, has_pwd=False, ident_count=1)
    r = client.delete("/api/auth/bind/gitcode?confirm=true")
    assert r.status_code == 400
    body = r.json()["detail"]
    assert body["need_set_password"] is True


# ---------------------------------------------------------------- S1-U5

def test_s1_u5_unknown_provider_rejected(app_and_client, monkeypatch):
    app, client = app_and_client
    db, _ = _mock_unbind_db(monkeypatch)
    r = client.delete("/api/auth/bind/foo?confirm=true")
    assert r.status_code == 400
    # 白名单在最前面——不应发出任何 SQL、不触碰 um 列
    assert db.cursor().queries == []
