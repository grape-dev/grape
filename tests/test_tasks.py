"""Tasks 路由测试：notify / close / mine"""
import os
import sys
import asyncio
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.api.routes import tasks as tasks_mod
from src.session_manager import db, db_lock
from src.user import db as user_db

def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.user.db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))

client = TestClient(app)

_uid = None
_token = ""
_headers = {}
_BOT_TOKEN = "bot_test_token"


def _bot_headers():
    """签发真实 tk_ 会话 token（作用域 gitcode:org/test），通过 get_current_user_or_api_key + 仓库作用域校验。

    notify 进契约后先走契约依赖鉴权：无效 token → 401；旧 `bot_test_token`（非 tk_）会被拦。
    """
    import uuid
    from src.api import auth_token
    tok = auth_token.issue_session_token(
        session_key=f"notify-test-{uuid.uuid4().hex}",
        repo_key="gitcode:org/test", agent_type="issue",
        user_id=(_uid.get("id") if _uid else 0))
    return {"Authorization": f"Bearer {tok}"}


def setup_module():
    global _uid, _token, _headers
    from src.api.db_setup import init_database
    init_database()
    # 用户卫生：先清掉历史残留（user_accounts + 映射），保证本模块用到的测试用户 id 确定、不被跨运行回收复用
    # （unified_user_mapping.user_id 为主键，id 被回收重用会导致 INSERT OR REPLACE 互相覆盖 → 顺序依赖失败）
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username IN ('test_tasks_user','bob_task_user')")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user IN ('younger_eric','bob')")
        db.commit()
    _uid = user_db.create_user("test_tasks_user", "test123")
    if not _uid:
        _uid = user_db.get_user_by_username("test_tasks_user")
    _token = _jwt_access_token(_uid["id"], _uid["username"])
    _headers = {"Authorization": f"Bearer {_token}"}
    # 清理旧映射并重建 gitcode_user -> user_id 映射
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) VALUES (?,?,?,?,datetime('now'))",
                  (_uid["id"], "younger_eric", "ou_test_123", "吴扬"))
        db.commit()
    # 建一个托管仓库（供 notify 校验）+ bot token（鉴权）
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) VALUES (?,?,?,?,?,?)",
                  ("gitcode:org/test", "org", "test", "org/test", 1, _BOT_TOKEN))
        db.commit()
    # 建 user_repos 成员关系（repo_all 走 get_managed_repos 成员校验，防 IDOR 加固后依赖此关系）
    user_db.mark_user_repo_join(_uid["id"], "gitcode:org/test", True, True)


def teardown_module():
    if _uid:
        user_db.delete_user(_uid["id"])
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username IN ('test_tasks_user','bob_task_user','bob_processing_user')")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user IN ('younger_eric','bob')")
        c.execute("DELETE FROM user_repos WHERE user_id IN (SELECT id FROM user_accounts WHERE username IN ('test_tasks_user','bob_task_user','bob_processing_user'))")
        db.commit()


def _clean_tasks():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
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
            row = c.execute("SELECT user_id, assignee, repo_key, issue_number, status FROM issue WHERE id=?", (data["task_id"],)).fetchone()
        assert row and row[0] == _uid["id"]
        assert row and row[1] == "younger_eric"
        assert row and row[4] == "active"

    def test_reassign_closes_other_assignee(self):
        _clean_tasks()
        # 先建 alice 任务（未注册则直接插库模拟）
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
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
            row = c.execute("SELECT status, completed_reason FROM issue WHERE id=?", (alice_task,)).fetchone()
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
                "SELECT id, task_description FROM issue WHERE repo_key='gitcode:org/test' AND issue_number=9 AND assignee='younger_eric' AND status='active'"
            ).fetchall()
        assert len(rows) == 1, rows

    def test_notify_no_background_chat_agent(self):
        """§十：任务分配 notify 不得触发后台 ChatAgent / 不注入用户 token 执行 opencode。

        回归看护：agent-tasks-direct-create-binding-fix-design.md §10.4。
        """
        _clean_tasks()

        import asyncio as _aio

        async def _run():
            await tasks_mod._run_notify(
                ctx=None, api=None, user_id=_uid["id"], conv_id="conv-no-agent",
                open_id="", task_description="处理登录超时", repo_key="gitcode:org/test",
                issue_number=5, cfg={"backend": "opencode"})

        # 断言：不调用 consume_stream_async、不产生 agent LLM 回复（静态模板消息落会话）
        with patch("src.runtime.executor.consume_stream_async") as m_evict, \
             patch("src.api.issues_center.add_message") as m_add:
            _aio.run(_run())
        m_evict.assert_not_called(), "notify 不得触发 LLM/opencode 执行"
        m_add.assert_called_once(), "应写入一条静态模板 agent 消息"
        _args = m_add.call_args[0]
        assert _args[0] == "conv-no-agent" and _args[1] == "agent"
        assert "任务已分配" in _args[2]
        assert "处理登录超时" not in _args[2], "模板不应重复完整任务描述（已在会话上下文）"

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
        """Issue #85: Issue closed → 任务进入 pending_review（不再自动 completed）"""
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 11))
            db.commit()
            tid = c.lastrowid
        r = client.post("/api/tasks/close", headers=_bot_headers(), json={"repo_key": "org/test", "issue_number": 11})
        assert r.status_code == 200
        assert r.json()["closed"] >= 1
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status, completed_reason FROM issue WHERE id=?", (tid,)).fetchone()
        assert row and row[0] == "pending_review"

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
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 13))
            db.commit()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (9999,?,?,?, 'active')",
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
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 15))
            db.commit()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, completed_reason, completed_at) VALUES (?,?,?,?, 'completed', 'issue_closed', datetime('now'))",
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
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/repoA", 20))
            db.commit()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/repoB", 21))
            db.commit()
        # 按 repoA 过滤
        r = client.get("/api/tasks/mine?repo_key=" + "org/repoA", headers=_headers)
        assert r.status_code == 200
        tasks = r.json()["tasks"]
        assert all(t["repo_key"].endswith("org/repoA") for t in tasks)
        assert len(tasks) == 1
        # 不传 repo → 全部
        r_all = client.get("/api/tasks/mine", headers=_headers)
        assert all(t["repo_key"].endswith(("org/repoA", "org/repoB")) for t in r_all.json()["tasks"])
        assert len(r_all.json()["tasks"]) == 2

    def test_mine_sort_order(self):
        """Issue #42：sort_order desc/asc 控制创建时间排序方向；mode=time 走纯时间序。"""
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            for i, st in enumerate(("active", "processing", "completed", "active")):
                c.execute(
                    "INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, created_at) "
                    "VALUES (?,?,?,?,?, datetime('now', ?))",
                    (_uid["id"], "younger_eric", "gitcode:org/sort", 30 + i, st,
                     f"-{i} minutes"))
            db.commit()
        r = client.get("/api/tasks/mine?mode=time&sort_order=desc", headers=_headers)
        assert r.status_code == 200
        got = [t["issue_number"] for t in r.json()["tasks"]]
        # 4 个任务 issue=30,31,32,33，created_at 依次-0,-1,-2,-3 分钟 → desc 最新(30)在前
        assert got == [30, 31, 32, 33], f"纯时间序 desc 应为 30,31,32,33，实际 {got}"
        r2 = client.get("/api/tasks/mine?mode=time&sort_order=asc", headers=_headers)
        got2 = [t["issue_number"] for t in r2.json()["tasks"]]
        assert got2 == [33, 32, 31, 30], f"纯时间序 asc 应为 33,32,31,30，实际 {got2}"

    def test_mine_sort_order_whitelist(self):
        """sort_order 白名单校验：非法值 → 400（FastAPI pattern）。"""
        r = client.get("/api/tasks/mine?sort_order=evil", headers=_headers)
        assert r.status_code == 422  # Query pattern 校验失败

    def test_mine_mode_default_priority_group(self):
        """mode=default：状态优先（active→processing→completed），组内按 created_at desc。"""
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            # 故意乱序插入：completed 最晚建，active 最早建
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, created_at) VALUES (?,?,?,?,'completed', datetime('now','-1 minutes'))", (_uid["id"], "younger_eric", "gitcode:org/md", 101))
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, created_at) VALUES (?,?,?,?,'active', datetime('now','-3 minutes'))", (_uid["id"], "younger_eric", "gitcode:org/md", 102))
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, created_at) VALUES (?,?,?,?,'processing', datetime('now','-2 minutes'))", (_uid["id"], "younger_eric", "gitcode:org/md", 103))
            db.commit()
        r = client.get("/api/tasks/mine?mode=default&sort_order=desc", headers=_headers)
        got = [t["issue_number"] for t in r.json()["tasks"]]
        # 状态优先：active(102) → processing(103) → completed(101)
        assert got == [102, 103, 101], f"default 模式应按状态分组，实际 {got}"


class TestUserMappingBackfill:
    """方案 B：GitCode OAuth 登录写映射 + 存量回填（notify 的 assignee→user_id 解析依赖 unified_user_mapping）"""

    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user IN ('backfill_user_1','oauth_user_1')")
            c.execute("DELETE FROM user_accounts WHERE username IN ('backfill_user_1','oauth_user_1')")
            c.execute("DELETE FROM issue WHERE repo_key='gitcode:org/test' AND issue_number IN (91, 92)")
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
                    "SELECT COUNT(*) FROM issue WHERE repo_key='gitcode:org/test' AND issue_number=91 AND assignee='backfill_user_1'"
                ).fetchone()
            assert row[0] == 1, "回填后 notify 应成功创建任务"
        finally:
            user_db.delete_user(created["id"])
            self._clean()


class TestTaskLifecycle:
    """任务生命周期：processing 状态 + bot 接管（task-lifecycle-design.md）"""

    def _clean(self):
        _clean_tasks()

    def test_notify_bot_takeover_does_not_complete(self):
        """assignee=grape_bot 时只做接管，不把需求置 completed（reassign-takeover-no-complete-design.md）"""
        self._clean()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
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
                "SELECT status FROM issue WHERE repo_key='gitcode:org/test' AND issue_number=50").fetchall()
        assert rows
        for (status,) in rows:
            assert status != "completed", f"bot 接管不应完成需求，实际 {status}"

    def test_notify_bot_takeover_no_tasks(self):
        """同 issue 无 active/processing 任务时 bot 接管返回 ok 且不抛错"""
        self._clean()
        r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
            "assignee": "grape_bot", "repo_key": "org/test", "issue_number": 51,
            "task_description": "接管"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_start_task_no_longer_flips_status(self):
        """点「去对话」不再翻转状态：active 保持；真实对话消息才置 processing（task-status-processing-trigger-fix.md）"""
        self._clean()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 52))
            db.commit()
            tid = c.lastrowid
        r = client.post(f"/api/tasks/{tid}/start", headers=_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "active"
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status FROM issue WHERE id=?", (tid,)).fetchone()
        assert row and row[0] == "active"

    def test_start_task_not_owner_rejected(self):
        """非本人任务调 start 返回 403"""
        self._clean()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
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
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'processing')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 54))
            db.commit()
            tid = c.lastrowid
        r = client.post(f"/api/tasks/{tid}/start", headers=_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "processing"
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status FROM issue WHERE id=?", (tid,)).fetchone()
        assert row and row[0] == "processing"

    def test_close_marks_processing_completed(self):
        """Issue #85: Issue closed → processing 任务进入 pending_review（不再自动 completed）"""
        self._clean()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'processing')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 55))
            db.commit()
        r = client.post("/api/tasks/close", headers=_bot_headers(), json={"repo_key": "org/test", "issue_number": 55})
        assert r.status_code == 200
        assert r.json()["closed"] >= 1
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status, completed_reason FROM issue WHERE repo_key='gitcode:org/test' AND issue_number=55").fetchone()
        assert row and row[0] == "pending_review"

    def test_reassign_does_not_complete_old_assignee(self):
        """换人不再把需求置 completed（reassign-takeover-no-complete-design.md）"""
        self._clean()
        # bob 用独立 user_id（unified_user_mapping.user_id 为主键，直接复用 _uid["id"] 会覆盖 younger_eric 映射）
        bob = user_db.create_user("bob_processing_user", "test123")
        if not bob:
            bob = user_db.get_user_by_username("bob_processing_user")
        try:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='bob'")
                c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) VALUES (?,?,?,?,datetime('now'))",
                          (bob["id"], "bob", "ou_bob", "鲍勃"))
                db.commit()
                c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'processing')",
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
                row = c.execute("SELECT status, completed_reason FROM issue WHERE id=?", (old_tid,)).fetchone()
            # 换人只转移责任，不完成需求
            assert row and row[0] != "completed", f"换人不应完成需求，实际 {row}"
            assert row and row[1] is None
        finally:
            if bob:
                user_db.delete_user(bob["id"])
            with db_lock:
                db.cursor().execute("DELETE FROM unified_user_mapping WHERE gitcode_user='bob'")
                db.commit()

    def test_mine_returns_processing(self):
        """/tasks/mine 返回 processing 状态任务"""
        self._clean()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'processing')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 57))
            db.commit()
        r = client.get("/api/tasks/mine", headers=_headers)
        assert r.status_code == 200
        statuses = [t["status"] for t in r.json()["tasks"]]
        assert "processing" in statuses


class TestRepoAllTasks:
    """Issue #72: GET /api/tasks/repo/{owner}/{repo} — 仓库全部任务（不限 user_id）"""

    def test_repo_all_requires_auth(self):
        r = client.get("/api/tasks/repo/org/test")
        assert r.status_code == 401

    def test_repo_all_returns_all_users_tasks(self):
        """返回该仓库所有任务，不限 user_id"""
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 70))
            db.commit()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (9999, "other_user", "gitcode:org/test", 71))
            db.commit()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (0, "bot_user", "gitcode:org/test", 72))
            db.commit()
        r = client.get("/api/tasks/repo/org/test", headers=_headers)
        assert r.status_code == 200
        tasks = r.json()["tasks"]
        issue_nums = [t["issue_number"] for t in tasks]
        assert 70 in issue_nums, "应包含当前用户任务"
        assert 71 in issue_nums, "应包含其他用户任务"
        assert 72 in issue_nums, "应包含 user_id=0 的任务"
        # 所有任务都是 org/test 仓库的
        assert all(t["repo_key"] == "org/test" for t in tasks)

    def test_repo_all_status_filter(self):
        """status 筛选正常"""
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 80))
            db.commit()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, completed_reason, completed_at) VALUES (?,?,?,?, 'completed', 'issue_closed', datetime('now'))",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 81))
            db.commit()
        r_active = client.get("/api/tasks/repo/org/test?status=active", headers=_headers)
        r_done = client.get("/api/tasks/repo/org/test?status=completed", headers=_headers)
        assert all(t["status"] == "active" for t in r_active.json()["tasks"])
        assert all(t["status"] == "completed" for t in r_done.json()["tasks"])
        assert 80 in [t["issue_number"] for t in r_active.json()["tasks"]]
        assert 81 in [t["issue_number"] for t in r_done.json()["tasks"]]

    def test_repo_all_includes_assignee(self):
        """返回的任务包含 assignee 字段"""
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 85))
            db.commit()
        r = client.get("/api/tasks/repo/org/test", headers=_headers)
        assert r.status_code == 200
        tasks = r.json()["tasks"]
        assert len(tasks) > 0
        assert all("assignee" in t for t in tasks), "每个任务应包含 assignee 字段"
        assert tasks[0]["assignee"] == "younger_eric"

    def test_repo_all_only_returns_target_repo(self):
        """只返回指定仓库的任务，不包含其他仓库"""
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 90))
            db.commit()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/other", 91))
            db.commit()
        r = client.get("/api/tasks/repo/org/test", headers=_headers)
        assert r.status_code == 200
        tasks = r.json()["tasks"]
        assert all(t["repo_key"] == "org/test" for t in tasks)
        assert 91 not in [t["issue_number"] for t in tasks]

    def test_repo_all_sort_order(self):
        """sort_order desc/asc 排序正常"""
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            for i in range(3):
                c.execute(
                    "INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, created_at) "
                    "VALUES (?,?,?,?,?, datetime('now', ?))",
                    (_uid["id"], "younger_eric", "gitcode:org/test", 100 + i, "active",
                     f"-{i} minutes"))
            db.commit()
        r_desc = client.get("/api/tasks/repo/org/test?mode=time&sort_order=desc", headers=_headers)
        got_desc = [t["issue_number"] for t in r_desc.json()["tasks"]]
        assert got_desc == [100, 101, 102], f"desc 应最新在前，实际 {got_desc}"
        r_asc = client.get("/api/tasks/repo/org/test?mode=time&sort_order=asc", headers=_headers)
        got_asc = [t["issue_number"] for t in r_asc.json()["tasks"]]
        assert got_asc == [102, 101, 100], f"asc 应最早在前，实际 {got_asc}"

    def test_repo_all_empty_repo(self):
        """有权限但无任务的仓库返回空列表"""
        _clean_tasks()
        r = client.get("/api/tasks/repo/org/test", headers=_headers)
        assert r.status_code == 200
        assert r.json()["tasks"] == []

    def test_repo_all_non_member_forbidden(self):
        """未托管/未加入该仓库的用户访问「仓库全部」应 403（防 IDOR）"""
        _clean_tasks()
        outsider = user_db.create_user("outsider_task_user", "test123")
        if not outsider:
            outsider = user_db.get_user_by_username("outsider_task_user")
        try:
            with db_lock:
                db.cursor().execute("DELETE FROM user_repos WHERE user_id=?", (outsider["id"],))
                db.commit()
            tok = _jwt_access_token(outsider["id"], outsider["username"])
            r = client.get("/api/tasks/repo/org/test", headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 403, f"非成员应 403，实际 {r.status_code}"
        finally:
            user_db.delete_user(outsider["id"])
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM user_repos WHERE user_id=?", (outsider["id"],))
                c.execute("DELETE FROM user_accounts WHERE username='outsider_task_user'")
                db.commit()

    def test_repo_all_assignees_filter(self):
        """assignees 筛选（单值 / 多值逗号分隔）"""
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            for _assignee, _num in (("alice", 120), ("bob", 121), ("carol", 122)):
                c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                          (_uid["id"], _assignee, "gitcode:org/test", _num))
            db.commit()
        r1 = client.get("/api/tasks/repo/org/test?assignees=alice", headers=_headers)
        assert [t["issue_number"] for t in r1.json()["tasks"]] == [120]
        r2 = client.get("/api/tasks/repo/org/test?assignees=alice,bob", headers=_headers)
        got = sorted(t["issue_number"] for t in r2.json()["tasks"])
        assert got == [120, 121], f"多值筛选应返回 alice+bob 任务，实际 {got}"

    def test_mine_assignees_filter(self):
        """mine 视图 assignees 筛选（共享 _build_assignee_filter）"""
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "gitcode:org/test", 130))
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "someone_else", "gitcode:org/test", 131))
            db.commit()
        r = client.get("/api/tasks/mine?assignees=younger_eric", headers=_headers)
        got = [t["issue_number"] for t in r.json()["tasks"]]
        assert got == [130], f"assignees 筛选应只返回 younger_eric，实际 {got}"
