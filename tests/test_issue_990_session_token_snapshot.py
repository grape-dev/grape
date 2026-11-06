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
import subprocess
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


class _FakeLockCursor:
    """模拟池外 MySQL 专用连接上的 GET_LOCK / RELEASE_LOCK。"""

    def __init__(self, state):
        self._state = state
        self._row = None

    def execute(self, sql, params=None):
        upper = sql.strip().upper()
        if "GET_LOCK" in upper:
            self._state["events"].append(("get", params[0]))
            self._row = (1 if self._state.get("acquired", True) else 0,)
        elif "RELEASE_LOCK" in upper:
            self._state["events"].append(("release", params[0]))
            self._row = (1,)
        else:
            raise AssertionError(f"unexpected lock SQL: {sql}")
        return self

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _FakeLockConn:
    def __init__(self, state):
        self._state = state

    def cursor(self):
        return _FakeLockCursor(self._state)

    def close(self):
        self._state["events"].append(("close", ""))


class _DBWithDedicated:
    """包装真实 db，仅补充 dedicated_connection（MySQL 命名锁通道）。"""

    def __init__(self, real, state):
        self._real = real
        self._state = state

    def __getattr__(self, name):
        return getattr(self._real, name)

    def dedicated_connection(self):
        return _FakeLockConn(self._state)


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
        # 生成阶段失败：UPDATE 尚未执行，旧 token 自然保持有效
        assert auth_token.verify_session_token(old) is not None

    def test_rotation_rollback_after_update_keeps_old_valid(self, monkeypatch):
        """在 UPDATE 已执行、INSERT 之前注入失败，验证事务真正回滚旧 token 过期。"""
        key = "i990-rot-rollback-%s" % uuid.uuid4().hex[:8]
        old = auth_token.issue_session_token(
            session_key=key, repo_key="o/r", agent_type="coding")

        def _boom(_token):
            raise RuntimeError("insert failed after update")

        # _sha256 在 INSERT 参数求值时调用，此时过期旧 token 的 UPDATE 已执行
        monkeypatch.setattr(auth_token, "_sha256", _boom)
        with pytest.raises(RuntimeError):
            auth_token.rotate_session_token(
                session_key=key, repo_key="o/r", agent_type="coding")
        monkeypatch.undo()
        assert auth_token.verify_session_token(old) is not None
        rows = _session_rows(key)
        assert len(rows) == 1  # 只保留原始旧 token 行，未产生半状态新行


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

    def test_mysql_path_uses_named_lock_and_rotates(self, monkeypatch):
        """MySQL 后端走专用连接 GET_LOCK 串行化，且锁内轮换/回写成功。"""
        import src.runtime.service as service_mod
        import src.session_manager as sm

        tid = _new_task()
        try:
            old = _task_env(tid)["GRAPE_SESSION_TOKEN"]
            _expire_token(old)
            state = {"acquired": True, "events": []}
            monkeypatch.setattr(sm, "_is_mysql_backend", lambda: True)
            monkeypatch.setattr(service_mod, "db", _DBWithDedicated(db, state))
            got = service_mod.sync_task_session_token(tid)
            assert got.startswith("tk_") and got != old
            assert got == _task_env(tid)["GRAPE_SESSION_TOKEN"]
            kinds = [e[0] for e in state["events"]]
            assert kinds.count("get") == 1
            assert kinds.count("release") == 1
            assert kinds.count("close") == 1
            # GET_LOCK 先于临界区，RELEASE_LOCK 在临界区之后
            assert kinds[0] == "get" and kinds[-2:] == ["release", "close"]
        finally:
            _cleanup([tid])

    def test_mysql_lock_not_acquired_fails_closed(self, monkeypatch):
        """MySQL 命名锁拿不到 → 不轮换、不改快照（fail-closed）。"""
        import src.runtime.service as service_mod
        import src.session_manager as sm

        tid = _new_task()
        try:
            old = _task_env(tid)["GRAPE_SESSION_TOKEN"]
            _expire_token(old)
            state = {"acquired": False, "events": []}
            monkeypatch.setattr(sm, "_is_mysql_backend", lambda: True)
            monkeypatch.setattr(service_mod, "db", _DBWithDedicated(db, state))
            assert service_mod.sync_task_session_token(tid) == ""
            assert _task_env(tid)["GRAPE_SESSION_TOKEN"] == old
            assert [r for r in _session_rows(tid)
                    if int(r[1]) > auth_token._now()] == []
        finally:
            _cleanup([tid])

    def test_mysql_dedicated_connection_unavailable_fails_closed(self, monkeypatch):
        """MySQL 后端没有专用连接通道 → 不无锁轮换（fail-closed）。"""
        import src.runtime.service as service_mod
        import src.session_manager as sm

        tid = _new_task()
        try:
            old = _task_env(tid)["GRAPE_SESSION_TOKEN"]
            _expire_token(old)

            class _NoDedicated:
                def __getattr__(self, name):
                    return getattr(db, name)

            monkeypatch.setattr(sm, "_is_mysql_backend", lambda: True)
            monkeypatch.setattr(service_mod, "db", _NoDedicated())
            assert service_mod.sync_task_session_token(tid) == ""
            assert _task_env(tid)["GRAPE_SESSION_TOKEN"] == old
        finally:
            _cleanup([tid])

    def test_multiprocess_sqlite_rotation_keeps_single_token(self, tmp_path):
        """真跨进程（4 个独立解释器）并发同步：只产生一个有效 token，且与快照一致。

        使用独立的临时 SQLite 文件（pytest 会话的测试库在 session fixture 中被 unlink，
        子进程无法通过路径共享），子进程各自 import 时绑定该文件。
        """
        import sqlite3

        tid = "t_i990_mp_%s" % uuid.uuid4().hex[:8]
        repo_key = "gitcode:issue990/mp"
        old_token = "tk_old_%s" % uuid.uuid4().hex
        db_file = str(tmp_path / "sessions.db")
        conn = sqlite3.connect(db_file)
        conn.execute(
            "CREATE TABLE agent_tasks (id TEXT PRIMARY KEY, repo_key TEXT, agent_type TEXT, "
            "user_id INTEGER, env_extra TEXT)")
        conn.execute(
            "CREATE TABLE session_api_tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_key TEXT, repo_key TEXT, agent_type TEXT, user_id INTEGER, "
            "token_sha256 TEXT, expires_at INTEGER)")
        conn.execute(
            "INSERT INTO agent_tasks (id, repo_key, agent_type, user_id, env_extra) VALUES (?,?,?,?,?)",
            (tid, repo_key, "coding", 0,
             json.dumps({"GRAPE_SESSION_TOKEN": old_token})))
        conn.execute(
            "INSERT INTO session_api_tokens (session_key, repo_key, agent_type, user_id, "
            "token_sha256, expires_at) VALUES (?,?,?,?,?,?)",
            (tid, repo_key, "coding", 0, auth_token._sha256(old_token), 0))
        conn.commit()
        conn.close()

        script = (
            "import os,sys\n"
            "sys.path.insert(0, os.environ['I990_ROOT'])\n"
            "from src.runtime.service import sync_task_session_token\n"
            "open(sys.argv[2], 'w').write(sync_task_session_token(sys.argv[1]))\n"
        )
        env = dict(os.environ)
        env["I990_ROOT"] = os.path.abspath(ROOT)
        env["DEVOPS_DB_PATH"] = db_file
        out_files = [str(tmp_path / ("out%d.txt" % i)) for i in range(4)]
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, tid, out_files[i]],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
            for i in range(4)
        ]
        for p in procs:
            _, err = p.communicate(timeout=180)
            assert p.returncode == 0, err.decode()[-800:]
        outs = [open(f).read().strip() for f in out_files]
        assert all(t.startswith("tk_") for t in outs), outs
        assert len(set(outs)) == 1
        token = outs[0]

        check = sqlite3.connect(db_file)
        snapshot = json.loads(
            check.execute("SELECT env_extra FROM agent_tasks WHERE id=?", (tid,)).fetchone()[0])
        active = check.execute(
            "SELECT token_sha256 FROM session_api_tokens WHERE session_key=? AND expires_at > ?",
            (tid, auth_token._now())).fetchall()
        check.close()
        assert snapshot["GRAPE_SESSION_TOKEN"] == token
        assert [r[0] for r in active] == [auth_token._sha256(token)]

    def test_sqlite_open_transaction_fails_closed(self):
        """SQLite 已有未结束事务、无法取 BEGIN IMMEDIATE → fail-closed 且不回滚调用方事务。"""
        tid = _new_task()
        try:
            old = _task_env(tid)["GRAPE_SESSION_TOKEN"]
            _expire_token(old)
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE agent_tasks SET status=? WHERE id=?", ("queued", tid))
                got = sync_task_session_token(tid)
                db.rollback()
            assert got == ""
            assert _task_env(tid)["GRAPE_SESSION_TOKEN"] == old
        finally:
            _cleanup([tid])


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
            # Issue #990 只同步 token，不向 runtime env 强制/覆盖 GRAPE_CLI_MODE
            assert "GRAPE_CLI_MODE" not in got["env"]
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