"""Issue #3396: Chat 引擎可选面 = 角色过滤 + 四档 scope；PUT 保存必须过 chat 角色校验

2026-11-20 用户裁定：
1. Chat 的可用引擎与其它角色**同口径**——复用 query_engines 四档（本机/个人/仓库/全局）
   并按 chat 角色过滤，不再走"仅本机直连+个人档"的旧口径；
2. 健康过低仍要能看到（标记 status）但不可选（disabled）；
3. PUT /chat/config 指定引擎时必须校验 chat 角色，堵住绕过口。

本用例守四条：
- 有 chat 角色的本机引擎出现在列表（scope=local, disabled=False）；
- 只有 coding 标签的本机引擎**不**出现；
- 有 chat 角色的 global 引擎出现（scope=global）——旧口径拿不到；
- PUT 指定无 chat 角色的引擎 → 400 ENGINE_ROLE_MISMATCH。
"""
import os

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

import pytest
from fastapi.testclient import TestClient
from src.api.main import app
from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.auth import grape_account_db

client = TestClient(app)
_uid = None
_headers = {}
_CREATED = []   # 本模块创建的 rid（本机档必须用已登记引擎名，无法按名字前缀清）
REPO = "org/test3396c"


def _jwt(user_id, username="test3396c"):
    from src.api.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mk_cred(name, scope="global"):
    # 凭据名带模块前缀：runtime_credentials.name 有唯一约束，两个 3396 用例模块
    # 曾共用 "remote_sre" 之类同名凭据 → 跨模块 UNIQUE 冲突（实测）。
    name = "test3396c_" + name
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h_test3396c_" + name, name, "dev-" + name, scope, "", "test3396c"))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, *, cid=0, tags=("all",), healthy=1):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, "
            "last_seen_at, scope, owner_id) VALUES (?,?,?,?,?,1,?,?,'')",
            (name, name, cid, "online" if healthy else "offline", healthy, _now(),
             "global" if cid else "local"))
        rid = c.lastrowid
        _CREATED.append(rid)
        for t in tags:
            c.execute("INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)",
                      (rid, t))
        db.commit()
        return rid


def setup_module():
    global _uid, _headers
    _uid = grape_account_db.create_grape_account("test3396c", email=None, password="test123")  # secrets-check: allow (test fixture password)
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("test3396c")
    _headers = {"Authorization": f"Bearer {_jwt(_uid['id'], _uid['username'])}"}
    with db_lock:
        c = db.cursor()
        for at, dn, n in [("vod", "VOD Agent", 1), ("issue", "Issue Agent", 2), ("coding", "Coding Agent", 3),
                          ("qa", "QA Agent", 4), ("sre", "SRE Agent", 5), ("chat", "Chat Agent", 6)]:
            c.execute(
                "INSERT OR REPLACE INTO agent_type (agent_type, display_name, description, icon, order_num, "
                "subscribe_events) VALUES (?,?,?,?,?,'[]')", (at, dn, f"Desc {at}", "●", n))
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                  "VALUES (?,?,?,?)", ("gitcode:" + REPO, "org", "test3396c", REPO))
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, is_join, is_admin) VALUES (?,?,1,1)",
                  (_uid["id"], "gitcode:" + REPO))
        db.commit()


def teardown_module():
    if _uid:
        grape_account_db.soft_delete_account(_uid["id"])


@pytest.fixture(autouse=True)
def _purge():
    def _p():
        with db_lock:
            c = db.cursor()
            if _CREATED:
                ph = ",".join("?" * len(_CREATED))
                c.execute(f"DELETE FROM engine_capabilities WHERE runtime_id IN ({ph})", tuple(_CREATED))
                c.execute(f"DELETE FROM agent_runtimes WHERE id IN ({ph})", tuple(_CREATED))
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'test3396c_%'")
            db.commit()
        _CREATED.clear()
    _p()
    yield
    _p()


def _backends():
    r = client.get(f"/api/chat/config?repo={REPO}", headers=_headers)
    assert r.status_code == 200, r.text[:300]
    return r.json().get("_backends") or []


def _find(rid, name=None):
    """只按 runtime_id 匹配（本机档名是已知引擎名，按名字会撞到别的同类型引擎）。"""
    for b in _backends():
        if int(b.get("runtime_id") or 0) == int(rid or 0):
            return b
    return None


class TestChatPickerScopeAndRole:
    def test_local_chat_engine_listed(self):
        name = "opencode"; rid = _mk_engine(name, tags=["all"], cid=0)
        row = _find(rid, name)
        assert row is not None, "local engine with 'all' tag missing from chat picker"
        assert row["scope"] == "local"
        assert row["disabled"] is False

    def test_local_engine_without_chat_role_excluded(self):
        name = "hermes"; rid = _mk_engine(name, tags=["coding"], cid=0)
        assert _find(rid, name) is None, "engine tagged only 'coding' must not be a chat candidate"

    def test_global_chat_engine_listed(self):
        cid = _mk_cred("remote_chat")
        name = "test3396c_global_chat"
        rid = _mk_engine(name, cid=cid, tags=["chat"])
        row = _find(rid, name)
        assert row is not None, "global engine with 'chat' tag missing (old scope-limited logic)"
        assert row["scope"] == "global"

    def test_global_engine_without_chat_role_excluded(self):
        cid = _mk_cred("remote_sre")
        name = "test3396c_global_sre"; rid = _mk_engine(name, cid=cid, tags=["sre"])
        assert _find(rid, name) is None

    def test_unhealthy_listed_but_disabled(self):
        name = "dsh"
        rid = _mk_engine(name, tags=["all"], healthy=0, cid=0)
        row = _find(rid, name)
        assert row is not None, "unhealthy engine should still be visible (用户裁定：标记健康值)"
        assert row["disabled"] is True
        assert row["status"] != "online"


class TestChatConfigWriteGuard:
    def test_put_rejects_engine_without_chat_role(self):
        rid = _mk_engine("test3396c_local_norole", tags=["coding"])
        r = client.put(f"/api/chat/config?repo={REPO}",
                       json={"backend": "test3396c_local_norole", "runtime_id": rid},
                       headers=_headers)
        assert r.status_code == 400, r.text[:300]
        detail = r.json().get("detail") or {}
        assert detail.get("error_code") == "ENGINE_ROLE_MISMATCH"

    def test_put_accepts_engine_with_chat_role(self):
        cid = _mk_cred("remote_ok")
        rid = _mk_engine("test3396c_global_ok", cid=cid, tags=["chat"])
        r = client.put(f"/api/chat/config?repo={REPO}",
                       json={"backend": "test3396c_global_ok", "runtime_id": rid},
                       headers=_headers)
        assert r.status_code == 200, r.text[:300]