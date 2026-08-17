"""Tasks 路由测试：notify / close / mine"""
import os
import sys
import asyncio
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.api.routes import tasks as tasks_mod
from src.session_manager import db, db_lock
from src.user import db as user_db

client = TestClient(app)

_uid = None
_token = ""
_headers = {}
_BOT_TOKEN = "bot_test_token"


def _bot_headers():
    return {"Authorization": f"Bearer {_BOT_TOKEN}"}


def setup_module():
    global _uid, _token, _headers
    from src.api.db_setup import init_database
    init_database()
    _uid = user_db.create_user("test_tasks_user", "test123")
    if not _uid:
        _uid = user_db.get_user_by_username("test_tasks_user")
    _token = user_db.create_session_token(_uid["id"])
    _headers = {"Authorization": f"Bearer {_token}"}
    # 清理旧映射并重建 gitcode_user -> user_id 映射
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user IN ('younger_eric','bob')")
        c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) VALUES (?,?,?,?,datetime('now'))",
                  (_uid["id"], "younger_eric", "ou_test_123", "吴扬"))
        db.commit()
    # 建一个托管仓库（供 notify 校验）+ bot token（鉴权）
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) VALUES (?,?,?,?,?,?)",
                  ("gitcode:org/test", "org", "test", "org/test", 1, _BOT_TOKEN))
        db.commit()


def teardown_module():
    if _uid:
        user_db.delete_user(_uid["id"])


def _clean_tasks():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM tasks")
        db.commit()


class TestNotify:
    def test_missing_fields(self):
        r = client.post("/api/tasks/notify", headers=_bot_headers(), json={})
        assert r.status_code == 400
        r = client.post("/api/tasks/notify", headers=_bot_headers(), json={"assignee": "x", "repo_key": "org/test"})
        assert r.status_code == 400

    def test_repo_not_managed(self):
        r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
            "assignee": "younger_eric", "repo_key": "nope/missing",
            "issue_number": 1, "task_description": "d"})
        assert r.status_code == 404

    def test_assignee_not_registered(self):
        _clean_tasks()
        r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
            "assignee": "unknown_user", "repo_key": "org/test",
            "issue_number": 5, "task_description": "处理登录超时"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert "未注册" in data["message"]

    def test_normal_path_creates_task_and_conversation(self):
        _clean_tasks()

        def _swallow(coro, *a, **k):
            try:
                coro.close()
            except Exception:
                pass
            return None

        with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
                "assignee": "younger_eric", "repo_key": "org/test",
                "issue_number": 5, "task_description": "处理登录超时"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["task_id"] > 0
        assert data["conversation_id"]
        # 任务落库 active
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT user_id, assignee, repo_key, issue_number, status FROM tasks WHERE id=?", (data["task_id"],)).fetchone()
        assert row and row[0] == _uid["id"]
        assert row and row[1] == "younger_eric"
        assert row and row[4] == "active"

    def test_reassign_closes_other_assignee(self):
        _clean_tasks()
        # 先建 alice 任务（未注册则直接插库模拟）
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "alice", "gitcode:org/test", 7))
            db.commit()
            alice_task = c.lastrowid
        # bob 用独立 user_id 注册（unified_user_mapping.user_id 为主键，避免覆盖 younger_eric）
        bob_uid = user_db.create_user("bob_task_user", "test123")
        if not bob_uid:
            bob_uid = user_db.get_user_by_username("bob_task_user")
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='bob'")
            c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) VALUES (?,?,?,?,datetime('now'))",
                      (bob_uid["id"], "bob", "ou_bob", "鲍勃"))
            db.commit()
        def _swallow(coro, *a, **k):
            try:
                coro.close()
            except Exception:
                pass
            return None

        with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
                "assignee": "bob", "repo_key": "org/test",
                "issue_number": 7, "task_description": "换人处理"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # alice 任务应被关闭（reassigned）
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status, completed_reason FROM tasks WHERE id=?", (alice_task,)).fetchone()
        assert row and row[0] == "completed"
        assert row and row[1] == "reassigned"

    def test_duplicate_notify_reuses_active(self):
        _clean_tasks()

        def _swallow(coro, *a, **k):
            try:
                coro.close()
            except Exception:
                pass
            return None

        with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
            client.post("/api/tasks/notify", headers=_bot_headers(), json={
                "assignee": "younger_eric", "repo_key": "org/test",
                "issue_number": 9, "task_description": "任务A"})
            client.post("/api/tasks/notify", headers=_bot_headers(), json={
                "assignee": "younger_eric", "repo_key": "org/test",
                "issue_number": 9, "task_description": "任务B"})
        # 同 assignee 同 issue 只应有 1 条 active 任务
        with db_lock:
            c = db.cursor()
            rows = c.execute(
                "SELECT id, task_description FROM tasks WHERE repo_key='gitcode:org/test' AND issue_number=9 AND assignee='younger_eric' AND status='active'"
            ).fetchall()
        assert len(rows) == 1, rows

    def test_notify_prewrite_role_is_agent(self):
        """任务分配预写入消息应为 agent（bot 下发），而非 user；任务描述只出现一次。

        回归看护：task-conversation-role-fix.md §7.1
        """
        _clean_tasks()

        def _swallow(coro, *a, **k):
            try:
                coro.close()
            except Exception:
                pass
            return None

        with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
                "assignee": "younger_eric", "repo_key": "org/test",
                "issue_number": 12, "task_description": "处理登录超时"})
        assert r.status_code == 200
        conv_id = r.json()["conversation_id"]
        assert conv_id

        with db_lock:
            c = db.cursor()
            rows = c.execute(
                "SELECT role, content FROM chat_messages WHERE conversation_id=? ORDER BY id",
                (conv_id,)).fetchall()
        assert rows, "会话应有消息"
        first_role, first_content = rows[0]
        assert first_role == "agent", f"预写入消息 role 应为 agent，实际 {first_role}"
        assert "任务分配" in first_content
        # 任务描述在整个会话中只出现一次（无 _run_notify 重复写入）
        desc_count = sum(1 for _, content in rows if "处理登录超时" in content)
        assert desc_count == 1, f"任务描述应只出现一次，实际 {desc_count}"

    def test_notify_message_roles_order(self):
        """会话消息顺序：agent(任务分配) 开头，后续出现用户答复。"""
        _clean_tasks()

        def _swallow(coro, *a, **k):
            try:
                coro.close()
            except Exception:
                pass
            return None

        with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
                "assignee": "younger_eric", "repo_key": "org/test",
                "issue_number": 13, "task_description": "描述B"})
        assert r.status_code == 200
        conv_id = r.json()["conversation_id"]
        assert conv_id

        from src.session_manager import add_message
        add_message(conv_id, "user", "好的，我开始处理")

        with db_lock:
            c = db.cursor()
            roles = [row[0] for row in c.execute(
                "SELECT role FROM chat_messages WHERE conversation_id=? ORDER BY id",
                (conv_id,)).fetchall()]
        assert roles and roles[0] == "agent", f"首条必须是 agent，实际 {roles}"
        assert "user" in roles, "后续应有用户答复"

    def test_build_task_context_message_content(self):
        """任务上下文消息内容：含 Issue 号/仓库/标题/任务描述，结尾为下发询问语气。"""
        msg = tasks_mod._build_task_context_message(
            title="登录超时", task_description="处理登录超时",
            repo_key="org/test", issue_number=5)
        assert "任务分配" in msg
        assert "#5" in msg
        assert "org/test" in msg
        assert "登录超时" in msg
        assert "处理登录超时" in msg
        # 结尾为 bot 下发询问（等待用户确认），而非用户自述
        assert "沟通" in msg or "确认" in msg or "疑问" in msg


class TestClose:
    def test_close_marks_completed(self):
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 11))
            db.commit()
            tid = c.lastrowid
        r = client.post("/api/tasks/close", headers=_bot_headers(), json={"repo_key": "org/test", "issue_number": 11})
        assert r.status_code == 200
        assert r.json()["closed"] >= 1
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status, completed_reason FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row and row[0] == "completed"
        assert row and row[1] == "issue_closed"

    def test_close_missing_fields(self):
        r = client.post("/api/tasks/close", headers=_bot_headers(), json={})
        assert r.status_code == 400


class TestMine:
    def test_mine_requires_auth(self):
        r = client.get("/api/tasks/mine")
        assert r.status_code == 401

    def test_mine_returns_only_current_user(self):
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 13))
            db.commit()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (9999,?,?,?, 'active')",
                      ("other", "gitcode:org/test", 14))
            db.commit()
        r = client.get("/api/tasks/mine", headers=_headers)
        assert r.status_code == 200
        tasks = r.json()["tasks"]
        assert all(t["assignee"] in ("younger_eric",) for t in tasks)
        assert all(t["issue_number"] != 14 for t in tasks)

    def test_mine_status_filter(self):
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 15))
            db.commit()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status, completed_reason, completed_at) VALUES (?,?,?,?, 'completed', 'issue_closed', datetime('now'))",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 16))
            db.commit()
        r_active = client.get("/api/tasks/mine?status=active", headers=_headers)
        r_done = client.get("/api/tasks/mine?status=completed", headers=_headers)
        assert all(t["status"] == "active" for t in r_active.json()["tasks"])
        assert all(t["status"] == "completed" for t in r_done.json()["tasks"])

    def test_mine_repo_filter(self):
        """按 repo_key 过滤：传 repo 只返回该 repo 任务；不传返回全部"""
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/repoA", 20))
            db.commit()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/repoB", 21))
            db.commit()
        # 按 repoA 过滤
        r = client.get("/api/tasks/mine?repo_key=" + "org/repoA", headers=_headers)
        assert r.status_code == 200
        tasks = r.json()["tasks"]
        assert all(t["repo_key"] == "org/repoA" for t in tasks)
        assert len(tasks) == 1
        # 不传 repo → 全部
        r_all = client.get("/api/tasks/mine", headers=_headers)
        assert all(t["repo_key"] in ("org/repoA", "org/repoB") for t in r_all.json()["tasks"])
        assert len(r_all.json()["tasks"]) == 2


class TestUserMappingBackfill:
    """方案 B：GitCode OAuth 登录写映射 + 存量回填（notify 的 assignee→user_id 解析依赖 unified_user_mapping）"""

    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user IN ('backfill_user_1','oauth_user_1')")
            c.execute("DELETE FROM user_accounts WHERE username IN ('backfill_user_1','oauth_user_1')")
            c.execute("DELETE FROM tasks WHERE repo_key='gitcode:org/test' AND issue_number IN (91, 92)")
            db.commit()

    def _run_backfill(self):
        """与 session_manager 启动回填同 SQL（幂等）"""
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR IGNORE INTO unified_user_mapping (user_id, gitcode_user, gitcode_nickname) "
                "SELECT id, username, nickname FROM user_accounts WHERE id>0"
            )
            db.commit()

    def test_backfill_populates_mapping_for_existing_account(self):
        """存量用户（仅 user_accounts、无映射）经回填后 notify 可解析 user_id"""
        self._clean()
        created = user_db.create_user("backfill_user_1", "pwd")
        uid = created["id"]
        try:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='backfill_user_1'")
                db.commit()
            self._run_backfill()
            assert tasks_mod._get_user_id_by_gitcode("backfill_user_1") == uid
        finally:
            user_db.delete_user(uid)
            self._clean()

    def test_notify_creates_task_after_backfill(self):
        """回填后 /api/tasks/notify 能成功建任务（此前因无映射返回 ok:false）"""
        self._clean()
        created = user_db.create_user("backfill_user_1", "pwd")
        try:
            self._run_backfill()

            def _swallow(coro, *a, **k):
                try:
                    coro.close()
                except Exception:
                    pass
                return None

            with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
                r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
                    "assignee": "backfill_user_1", "repo_key": "org/test",
                    "issue_number": 91, "task_description": "回填后建任务"})
            assert r.status_code == 200
            assert r.json().get("ok") is True, r.json()
            with db_lock:
                row = db.cursor().execute(
                    "SELECT COUNT(*) FROM tasks WHERE repo_key='gitcode:org/test' AND issue_number=91 AND assignee='backfill_user_1'"
                ).fetchone()
            assert row[0] == 1, "回填后 notify 应成功创建任务"
        finally:
            user_db.delete_user(created["id"])
            self._clean()


class TestTaskLifecycle:
    """任务生命周期：processing 状态 + bot 接管（task-lifecycle-design.md）"""

    def _clean(self):
        _clean_tasks()

    def test_notify_bot_takeover_closes_tasks(self):
        """assignee=grape_bot 时，同 issue 的 active/processing 任务置 completed（bot_takeover）"""
        self._clean()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 50))
            db.commit()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'processing')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 50))
            db.commit()
        r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
            "assignee": "grape_bot", "repo_key": "org/test", "issue_number": 50,
            "task_description": "接管"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        with db_lock:
            c = db.cursor()
            rows = c.execute(
                "SELECT status, completed_reason FROM tasks WHERE repo_key='gitcode:org/test' AND issue_number=50").fetchall()
        assert rows, "应有任务"
        for status, reason in rows:
            assert status == "completed", f"bot 接管后应为 completed，实际 {status}"
            assert reason == "bot_takeover", f"completed_reason 应为 bot_takeover，实际 {reason}"

    def test_notify_bot_takeover_no_tasks(self):
        """同 issue 无 active/processing 任务时 bot 接管返回 ok 且不抛错"""
        self._clean()
        r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
            "assignee": "grape_bot", "repo_key": "org/test", "issue_number": 51,
            "task_description": "接管"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_start_task_active_to_processing(self):
        """用户点「去对话」：active → processing"""
        self._clean()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 52))
            db.commit()
            tid = c.lastrowid
        r = client.post(f"/api/tasks/{tid}/start", headers=_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "processing"
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row and row[0] == "processing"

    def test_start_task_not_owner_rejected(self):
        """非本人任务调 start 返回 403"""
        self._clean()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (9999, "other", "gitcode:org/test", 53))
            db.commit()
            tid = c.lastrowid
        r = client.post(f"/api/tasks/{tid}/start", headers=_headers)
        assert r.status_code == 403

    def test_start_task_idempotent(self):
        """processing 任务重复 start 返回当前状态，不改回 active"""
        self._clean()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'processing')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 54))
            db.commit()
            tid = c.lastrowid
        r = client.post(f"/api/tasks/{tid}/start", headers=_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "processing"
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row and row[0] == "processing"

    def test_close_marks_processing_completed(self):
        """Issue 关闭应关闭 processing 任务（不只 active）"""
        self._clean()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'processing')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 55))
            db.commit()
        r = client.post("/api/tasks/close", headers=_bot_headers(), json={"repo_key": "org/test", "issue_number": 55})
        assert r.status_code == 200
        assert r.json()["closed"] >= 1
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status, completed_reason FROM tasks WHERE repo_key='gitcode:org/test' AND issue_number=55").fetchone()
        assert row and row[0] == "completed"
        assert row and row[1] == "issue_closed"

    def test_reassign_closes_processing_old_assignee(self):
        """换人时原 assignee 的 processing 任务置 completed（reassigned）"""
        self._clean()
        # 建 bob 映射（换人目标 assignee）
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) VALUES (?,?,?,?,datetime('now'))",
                      (_uid["id"], "bob", "ou_bob", "鲍勃"))
            db.commit()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'processing')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 56))
            db.commit()
            old_tid = c.lastrowid

        def _swallow(coro, *a, **k):
            try:
                coro.close()
            except Exception:
                pass
            return None

        with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
                "assignee": "bob", "repo_key": "org/test", "issue_number": 56,
                "task_description": "换人处理"})
        assert r.status_code == 200
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status, completed_reason FROM tasks WHERE id=?", (old_tid,)).fetchone()
        # 原 assignee（younger_eric）的 processing 任务应被换人关闭
        assert row and row[0] == "completed", f"原 assignee processing 任务应 completed，实际 {row}"
        assert row and row[1] == "reassigned"

    def test_mine_returns_processing(self):
        """/tasks/mine 返回 processing 状态任务"""
        self._clean()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'processing')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 57))
            db.commit()
        r = client.get("/api/tasks/mine", headers=_headers)
        assert r.status_code == 200
        statuses = [t["status"] for t in r.json()["tasks"]]
        assert "processing" in statuses
