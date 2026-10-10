"""Issue-45 方式 A/B Token 认证内核测试（M0）.

覆盖：
- T1  会话签发临时 token：行存在、sha256 落库、明文不落盘
- T2  agent 用会话 token 调业务 API：auth_type=session_token + 作用域上下文
- T3/T5 会话结束 revoke 后 token 失效
- T4 超 TTL token 失效
- T6  用户级 API Key 三通道（Bearer / PRIVATE-TOKEN / access_token）
- T7  JWT 会话经统一入口仍可用（auth_type=jwt）
- T8  session token 作用域互不通用
- T9  明文不落盘（库内只有 sha256）
- 统一入口无效 token / 无 token → 401
"""
import time
import pytest
from fastapi import FastAPI, Depends, Request
from fastapi.testclient import TestClient

from src.api import auth_token
from src.api.deps import get_current_user_or_api_key
from src.api.jwt_utils import create_access_token
from src.user import db as user_db


@pytest.fixture(autouse=True)
def _clean_token_tables():
    """每用例前清空 token 表：Windows 下旧库残留跨运行，防键值冲突。"""
    from src.session_manager import db, db_lock
    with db_lock:
        db.cursor().execute("DELETE FROM session_api_tokens WHERE id LIKE 'test_%' OR id LIKE 'sess_%' OR session_key LIKE 'test_%'")
        db.cursor().execute("DELETE FROM api_tokens WHERE token LIKE 'test_%' OR token LIKE 'tk_test%' OR user_id IN (SELECT id FROM user_accounts WHERE username LIKE 'test_%' OR username LIKE 'tc_%')")
        db.commit()
    yield


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/probe")
    def probe(user: dict = Depends(get_current_user_or_api_key)):
        return {"id": user["id"], "auth_type": user["auth_type"]}

    app.get("/probe_req")(lambda req: req)
    return TestClient(app)


def _make_user(username: str = "tester") -> dict:
    # 唯一用户名：避免 Windows 下测试库文件被占用未能删除导致的跨运行 UNIQUE 冲突
    import uuid
    from src.user import db as _db
    return _db.create_user(f"{username}_{uuid.uuid4().hex[:8]}", "pw")


# ==================== 方式 A：会话级临时 token ====================

def test_t1_issue_session_token_sha256_on_repo():
    uid = _make_user("t1")["id"]
    plain = auth_token.issue_session_token(
        session_key="task-1", repo_key="o/r", agent_type="vod", user_id=0)
    assert plain.startswith("tk_")
    from src.session_manager import db, db_lock
    with db_lock:
        row = db.cursor().execute(
            "SELECT token_sha256, repo_key, agent_type, user_id, expires_at FROM session_api_tokens WHERE session_key='task-1'"
        ).fetchone()
    assert row is not None
    assert row[0] != plain                       # 落库是 sha256
    assert auth_token._sha256(plain) == row[0]   # 且等于明文哈希
    assert row[1] == "o/r" and row[2] == "vod" and row[3] == 0   # T8: devops 无 user_id


def test_t9_plaintext_never_in_db():
    uid = _make_user("t9")["id"]
    plain = auth_token.issue_session_token(
        session_key="task-9", repo_key="o/r", agent_type="chat", user_id=uid)
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        # 全表扫描确认明文不出现在任何列
        rows = c.execute("SELECT * FROM session_api_tokens WHERE session_key='task-9'").fetchall()
    dumped = str(rows)
    assert plain not in dumped


def test_t2_verify_session_token_returns_context(client):
    uid = _make_user("t2")["id"]
    plain = auth_token.issue_session_token(
        session_key="conv-2", repo_key="o/r", agent_type="chat", user_id=uid)
    r = client.get("/probe", headers={"Authorization": f"Bearer {plain}"})
    assert r.status_code == 200
    body = r.json()
    assert body["auth_type"] == "session_token"
    info = auth_token.verify_session_token(plain)
    assert info["repo_key"] == "o/r"
    assert info["agent_type"] == "chat"
    assert info["user_id"] == uid


def test_t3_t5_revoke_session_token_invalid(client):
    _make_user("t3")
    plain = auth_token.issue_session_token(
        session_key="task-3", repo_key="o/r", agent_type="vod")
    assert client.get("/probe", headers={"Authorization": f"Bearer {plain}"}).status_code == 200
    assert auth_token.revoke_session_token("task-3") is True
    assert client.get("/probe", headers={"Authorization": f"Bearer {plain}"}).status_code == 401
    assert auth_token.verify_session_token(plain) is None


def test_t8_session_token_scope_not_shared():
    _make_user("t8a")
    p1 = auth_token.issue_session_token(session_key="sess-a", repo_key="o/r", agent_type="vod")
    p2 = auth_token.issue_session_token(session_key="sess-b", repo_key="other/r", agent_type="vod")
    # 会话 a 的 token 作用域绑定在 o/r；会话 b 独立
    assert auth_token.revoke_session_token("sess-a") is True
    assert auth_token.verify_session_token(p1) is None
    assert auth_token.verify_session_token(p2) is not None   # b 不受 a 失效影响


def test_t4_expired_session_token_invalid():
    _make_user("t4")
    plain = auth_token.issue_session_token(
        session_key="task-4", repo_key="o/r", agent_type="vod", ttl=-60)
    assert auth_token.verify_session_token(plain) is None


def test_purge_expired_session_tokens():
    _make_user("t4p")
    auth_token.issue_session_token(session_key="task-p1", repo_key="o/r", agent_type="vod", ttl=-60)
    auth_token.issue_session_token(session_key="task-p2", repo_key="o/r", agent_type="vod", ttl=3600)
    n = auth_token.purge_expired_session_tokens()
    assert n >= 1
    from src.session_manager import db, db_lock
    with db_lock:
        left = db.cursor().execute("SELECT session_key FROM session_api_tokens WHERE session_key='task-p1'").fetchall()
    assert left == []


# ==================== 方式 B：用户级 API Key ====================

def test_generate_and_verify_api_key_three_channels(client):
    uid = _make_user("t6")["id"]
    got = auth_token.generate_api_token(uid, name="ci-token-01")
    assert got and got["token"].startswith("grape_")

    info = auth_token.verify_api_token(got["token"])
    assert info["user_id"] == uid and info["name"] == "ci-token-01"

    heads = [
        {"Authorization": f"Bearer {got['token']}"},
        {"PRIVATE-TOKEN": got["token"]},
        {},  # access_token 走 query
    ]
    for i, h in enumerate(heads):
        url = "/probe"
        if i == 2:
            url += f"?access_token={got['token']}"
        r = client.get(url, headers=h)
        assert r.status_code == 200, f"channel {i} failed"
        assert r.json()["auth_type"] == "api_key"


def test_api_key_secret_hash_not_plain(client):
    uid = _make_user("t6h")["id"]
    got = auth_token.generate_api_token(uid, name="ci")
    assert got
    from src.session_manager import db, db_lock
    with db_lock:
        row = db.cursor().execute("SELECT token_sha256 FROM api_tokens WHERE id=?", (got["id"],)).fetchone()
    assert row[0] == auth_token._sha256(got["token"])
    assert row[0] != got["token"]


def test_revoke_api_key(client):
    uid = _make_user("t6r")["id"]
    got = auth_token.generate_api_token(uid, name="ci")
    assert client.get("/probe", headers={"Authorization": f"Bearer {got['token']}"}).status_code == 200
    assert auth_token.revoke_api_token(uid, got["id"]) is True
    assert client.get("/probe", headers={"Authorization": f"Bearer {got['token']}"}).status_code == 401
    assert auth_token.verify_api_token(got["token"]) is None
    # 已吊销 token 不出现在列表
    assert all(t["id"] != got["id"] for t in auth_token.list_api_tokens(uid))


def test_list_api_tokens_masks_secret(client):
    uid = _make_user("t6l")["id"]
    got = auth_token.generate_api_token(uid, name="mask-me")
    items = auth_token.list_api_tokens(uid)
    assert any(t["id"] == got["id"] for t in items)
    dumped = str(items)
    assert got["token"] not in dumped
    assert "token_sha256" not in dumped


# ==================== JWT 会话与统一入口 ====================

def test_t7_jwt_via_unified_entry(client):
    user = _make_user("t7")
    jwt = create_access_token(user["id"], "t7")
    r = client.get("/probe", headers={"Authorization": f"Bearer {jwt}"})
    assert r.status_code == 200
    assert r.json()["auth_type"] == "jwt"


def test_unified_entry_401_without_token(client):
    assert client.get("/probe").status_code == 401


def test_unified_entry_401_invalid_token(client):
    assert client.get("/probe", headers={"Authorization": "Bearer <invalid>"}).status_code == 401
    assert client.get("/probe", headers={"PRIVATE-TOKEN": "grape_invalid"}).status_code == 401
    assert client.get("/probe?access_token=tk_invalid").status_code == 401