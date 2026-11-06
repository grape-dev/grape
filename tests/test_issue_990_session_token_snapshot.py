"""Issue #990：会话 token 运行时进程与 shell 环境快照不一致。

覆盖：
- rotate_session_token：旧 token 失效、新 token 生效（token 轮换）
- sync_task_session_token：快照 token 有效时原样复用（不重复签发）
- sync_task_session_token：快照 token 失效时轮换并回写 env_extra（快照==返回值）
- force_rotate：强制轮换后快照同步刷新
- runtime_task_env：执行前 env 返回的快照 token 与 agent_tasks.env_extra 一致
- 401 SESSION_TOKEN_INVALID message 带「从运行时进程环境重读 token」恢复指引
"""
import json
import os
import sys
import uuid

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from src.api import auth_token
from src.api.deps import get_current_user_or_api_key
from src.runtime.service import enqueue_task, sync_task_session_token
from src.session_manager import db, db_lock


def _seed_session_and_runtime(repo_key="gitcode:issue990/r", agent_type="coding"):
    with db_lock:
        c = db.cursor()
        sid = "i990sess-%s" % uuid.uuid4().hex[:8]
        c.execute(
            "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, status, skill) "
            "VALUES (?,?,?,?,'i990skill')",
            (sid, repo_key, agent_type, "idle"))
        name = "i990rt-%s" % uuid.uuid4().hex[:6]
        c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, installed, healthy, status, scope, owner_id) "
                  "VALUES (?,?,'0',1,1,'online','global','')", (name, name))
        rid = c.lastrowid
        db.commit()
    return sid, rid


def _new_task(repo_key="gitcode:issue990/r", agent_type="coding", user_id=0, env_extra=None):
    sid, rid = _seed_session_and_runtime(repo_key, agent_type)
    return enqueue_task(
        agent_session_id=sid, runtime_id=rid, prompt="p", cwd=".",
        event_type="test", account="", user_id=user_id,
        repo_key=repo_key, agent_type=agent_type, skill="i990skill",
        env_extra=dict(env_extra or {}),
    )


def _task_env(task_id):
    with db_lock:
        row = db.cursor().execute("SELECT env_extra FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def _expire_token(token):
    with db_lock:
        db.cursor().execute(
            "UPDATE session_api_tokens SET expires_at='0' WHERE token_sha256=?",
            (auth_token._sha256(token),))
        db.commit()


def _session_rows(task_id):
    with db_lock:
        return db.cursor().execute(
            "SELECT token_sha256, expires_at FROM session_api_tokens WHERE session_key=?",
            (task_id,)).fetchall()


def _cleanup(task_ids):
    with db_lock:
        c = db.cursor()
        for tid in task_ids:
            c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
            c.execute("DELETE FROM session_api_tokens WHERE session_key=?", (tid,))
        c.execute("DELETE FROM devops_agent_session WHERE id LIKE 'i990sess-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'i990rt-%'")
        db.commit()


class TestRotateSessionToken:
    def test_rotation_invalidates_old_and_issues_new(self):
        key = "i990-rot-%s" % uuid.uuid4().hex[:8]
        old = auth_token.issue_session_token(
            session_key=key, repo_key="o/r", agent_type="coding")
        new = auth_token.rotate_session_token(
            session_key=key, repo_key="o/r", agent_type="coding")
        assert new != old and new.startswith("tk_")
        assert auth_token.verify_session_token(old) is None
        info = auth_token.verify_session_token(new)
        assert info and info["session_key"] == key and info["repo_key"] == "o/r"

    def test_rotation_keeps_single_active_token(self):
        key = "i990-rot-single-%s" % uuid.uuid4().hex[:8]
        auth_token.issue_session_token(session_key=key, repo_key="o/r", agent_type="coding")
        new = auth_token.rotate_session_token(session_key=key, repo_key="o/r", agent_type="coding")
        with db_lock:
            rows = db.cursor().execute(
                "SELECT token_sha256 FROM session_api_tokens "
                "WHERE session_key=? AND CAST(expires_at AS INTEGER) > ?",
                (key, auth_token._now())).fetchall()
        assert [r[0] for r in rows] == [auth_token._sha256(new)]


class TestSyncTaskSessionToken:
    def test_reuses_valid_snapshot_token(self):
        tid = _new_task()
        try:
            snapshot = _task_env(tid)["GRAPE_SESSION_TOKEN"]
            before = len(_session_rows(tid))
            got = sync_task_session_token(tid)
            assert got == snapshot
            assert _task_env(tid)["GRAPE_SESSION_TOKEN"] == snapshot
            assert len(_session_rows(tid)) == before  # 未重复签发
        finally:
            _cleanup([tid])

    def test_rotates_expired_snapshot_and_persists(self):
        tid = _new_task()
        try:
            old = _task_env(tid)["GRAPE_SESSION_TOKEN"]
            _expire_token(old)
            assert auth_token.verify_session_token(old) is None

            new = sync_task_session_token(tid)
            assert new.startswith("tk_") and new != old
            # 运行时注入值（返回值）与 shell 环境快照一致
            assert _task_env(tid)["GRAPE_SESSION_TOKEN"] == new
            assert auth_token.verify_session_token(new) is not None
            info = auth_token.verify_session_token(new)
            assert info["session_key"] == tid and info["repo_key"] == "gitcode:issue990/r"
        finally:
            _cleanup([tid])

    def test_force_rotate_refreshes_snapshot(self):
        tid = _new_task()
        try:
            old = _task_env(tid)["GRAPE_SESSION_TOKEN"]
            assert auth_token.verify_session_token(old) is not None
            new = sync_task_session_token(tid, force_rotate=True)
            assert new != old
            assert _task_env(tid)["GRAPE_SESSION_TOKEN"] == new
            assert auth_token.verify_session_token(old) is None
            assert auth_token.verify_session_token(new) is not None
        finally:
            _cleanup([tid])

    def test_missing_task_returns_empty(self):
        assert sync_task_session_token("t_does_not_exist_990") == ""
        assert sync_task_session_token("") == ""


class TestRuntimeTaskEnvSync:
    def test_exec_env_matches_snapshot(self):
        from src.api.routes.runtime import runtime_task_env
        tid = _new_task()
        try:
            old = _task_env(tid)["GRAPE_SESSION_TOKEN"]
            _expire_token(old)
            got = runtime_task_env(tid, credential_id=1)
            token = got["env"]["GRAPE_SESSION_TOKEN"]
            assert token.startswith("tk_")
            assert token == _task_env(tid)["GRAPE_SESSION_TOKEN"]
            assert got["env"]["GRAPE_CLI_MODE"] == "WORKER"
            assert auth_token.verify_session_token(token) is not None
        finally:
            _cleanup([tid])

    def test_exec_env_reuses_valid_snapshot(self):
        from src.api.routes.runtime import runtime_task_env
        tid = _new_task()
        try:
            snapshot = _task_env(tid)["GRAPE_SESSION_TOKEN"]
            got = runtime_task_env(tid, credential_id=1)
            assert got["env"]["GRAPE_SESSION_TOKEN"] == snapshot
        finally:
            _cleanup([tid])


class TestSessionToken401RecoveryHint:
    def test_401_message_guides_reading_runtime_env(self):
        app = FastAPI()

        @app.get("/probe")
        def probe(user: dict = Depends(get_current_user_or_api_key)):
            return {"ok": True}

        client = TestClient(app)
        r = client.get("/probe", headers={"Authorization": "Bearer tk_invalid"})
        assert r.status_code == 401
        detail = r.json()["detail"]
        assert detail["error_code"] == "SESSION_TOKEN_INVALID"
        assert "re-read GRAPE_SESSION_TOKEN" in detail["message"]
        assert "runtime process environment" in detail["message"]