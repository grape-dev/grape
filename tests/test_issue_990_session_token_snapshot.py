"""Issue #990：会话 token 运行时进程与 shell 环境快照不一致。

覆盖：
- rotate_session_token：旧 token 失效、新 token 生效；失败回滚不产生半状态
- sync_task_session_token：快照 token 有效时原样复用（不重复签发）
- sync_task_session_token：快照 token 失效时轮换并回写 env_extra（快照==返回值）
- sync_task_session_token：并发同步只产生一个有效 token
- sync_task_session_token：快照回写失败时回滚，不返回未落库 token
- session_token_for_execution：direct 路径复用快照；失败不签未回写 token
- runtime_task_env：执行前 env 返回的快照 token 与 agent_tasks.env_extra 一致
- 401 SESSION_TOKEN_INVALID message 带「从运行时进程环境重读 token」恢复指引
"""
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from src.api import auth_token
from src.api.deps import get_current_user_or_api_key
from src.runtime.service import (
    enqueue_task, session_token_for_execution, sync_task_session_token,
)
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

    def test_rotation_failure_rolls_back_and_keeps_old_valid(self, monkeypatch):
        key = "i990-rot-fail-%s" % uuid.uuid4().hex[:8]
        old = auth_token.issue_session_token(
            session_key=key, repo_key="o/r", agent_type="coding")

        def _boom(_n):
            raise RuntimeError("token generation failed")

        monkeypatch.setattr(auth_token.secrets, "token_urlsafe", _boom)
        with pytest.raises(RuntimeError):
            auth_token.rotate_session_token(
                session_key=key, repo_key="o/r", agent_type="coding")
        monkeypatch.undo()
        # 过期旧 token 的 UPDATE 与签发 INSERT 在同一事务内回滚
        assert auth_token.verify_session_token(old) is not None


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

    def test_concurrent_sync_keeps_single_active_token(self):
        tid = _new_task()
        try:
            old = _task_env(tid)["GRAPE_SESSION_TOKEN"]
            _expire_token(old)
            with ThreadPoolExecutor(max_workers=6) as pool:
                tokens = list(pool.map(lambda _: sync_task_session_token(tid), range(8)))
            assert len(set(tokens)) == 1 and tokens[0].startswith("tk_")
            assert tokens[0] == _task_env(tid)["GRAPE_SESSION_TOKEN"]
            active = [r for r in _session_rows(tid) if int(r[1]) > auth_token._now()]
            assert len(active) == 1
        finally:
            _cleanup([tid])

    def test_snapshot_persist_failure_rolls_back(self, monkeypatch):
        tid = _new_task()
        try:
            old = _task_env(tid)["GRAPE_SESSION_TOKEN"]
            _expire_token(old)
            import src.runtime.service as service_mod

            class _FailCommit:
                def __init__(self, real):
                    self._real = real

                def cursor(self):
                    return self._real.cursor()

                def rollback(self):
                    return self._real.rollback()

                def commit(self):
                    raise RuntimeError("snapshot persist failed")

            monkeypatch.setattr(service_mod, "db", _FailCommit(db))
            got = service_mod.sync_task_session_token(tid)
            monkeypatch.undo()
            # 持久化失败 → 不返回未落库 token，轮换 INSERT 一并回滚，快照保持旧值
            assert got == ""
            assert _task_env(tid)["GRAPE_SESSION_TOKEN"] == old
            active = [r for r in _session_rows(tid) if int(r[1]) > auth_token._now()]
            assert active == []
        finally:
            _cleanup([tid])

    def test_missing_task_returns_empty(self):
        assert sync_task_session_token("t_does_not_exist_990") == ""
        assert sync_task_session_token("") == ""


class TestSessionTokenForExecution:
    def test_direct_path_reuses_rotated_snapshot(self):
        tid = _new_task()
        try:
            old = _task_env(tid)["GRAPE_SESSION_TOKEN"]
            _expire_token(old)
            got = session_token_for_execution(
                tid, session_key_fallback="d_990",
                repo_key="gitcode:issue990/r", agent_type="coding")
            assert got.startswith("tk_") and got != old
            assert got == _task_env(tid)["GRAPE_SESSION_TOKEN"]
            assert auth_token.verify_session_token(got) is not None
        finally:
            _cleanup([tid])

    def test_direct_path_skips_unpersisted_token_on_sync_failure(self, monkeypatch):
        tid = _new_task()
        try:
            snapshot = _task_env(tid)["GRAPE_SESSION_TOKEN"]
            before = _session_rows(tid)
            import src.runtime.service as service_mod
            monkeypatch.setattr(service_mod, "sync_task_session_token", lambda _tid: "")
            got = service_mod.session_token_for_execution(
                tid, session_key_fallback="d_990",
                repo_key="gitcode:issue990/r", agent_type="coding")
            assert got == ""
            assert _session_rows(tid) == before
            assert _task_env(tid)["GRAPE_SESSION_TOKEN"] == snapshot
        finally:
            _cleanup([tid])

    def test_direct_path_without_task_row_issues_fallback_token(self):
        key = "i990-direct-%s" % uuid.uuid4().hex[:8]
        try:
            got = session_token_for_execution(
                "", session_key_fallback=key, repo_key="o/r", agent_type="coding")
            info = auth_token.verify_session_token(got)
            assert info and info["session_key"] == key
        finally:
            with db_lock:
                db.cursor().execute(
                    "DELETE FROM session_api_tokens WHERE session_key=?", (key,))
                db.commit()


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