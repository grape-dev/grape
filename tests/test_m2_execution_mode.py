"""M2 执行模式切换测试（direct / queue_local）：见 acceptance-plan M2-01/02/10"""
import json, os
import pytest

from src.session_manager import db, db_lock
from src.api import event_pipeline
from src.runtime import service
from src.api.event_store import append_event


def _evt_body(issue_id="#5", etype="issue_opened"):
    return {
        "event_type": etype,
        "payload": {"issue": {"number": 5, "title": "登录超时", "body": "x"}, "action": "opened"},
        "issue_id": issue_id,
        "repository": "owner/repo",
    }


class TestExecutionModeSwitch:
    def teardown_method(self):
        # 移除残留执行模式环境变量，避免污染其他测试文件（默认态 = 未设置，走 auto）
        os.environ.pop("DEVOPS_EXECUTION_MODE", None)

    def _clean(self):
        with db_lock:
            db.cursor().execute("DELETE FROM agent_tasks")
            db.cursor().execute("DELETE FROM events")
            # 清掉可能由其他 pipeline 测试残留的会话/阶段行，保证本测试不依赖共享库顺序
            db.cursor().execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%' OR id LIKE 'sess-m2-%'")
            db.cursor().execute("DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:owner/repo%'")
            # 引擎隔离：清掉全部远端/全局引擎（保留本机 credential_id=0 兜底），
            # 避免其他测试残留的全局引擎被 pick_engine 选中导致执行模式不确定（随机顺序 flaky）
            db.cursor().execute("DELETE FROM agent_runtimes WHERE credential_id>0")
            db.cursor().execute("DELETE FROM runtime_credentials")
            db.cursor().execute("DELETE FROM devops_agent_runtime_binding")
            db.commit()

    def test_mode_getter_meta(self, monkeypatch):
        # get_execution_mode 优先读 env；先清掉可能的残留，再验证 meta 落库生效
        monkeypatch.delenv("DEVOPS_EXECUTION_MODE", raising=False)
        self._clean()
        service.set_execution_mode("queue_remote")
        assert service.get_execution_mode() == "queue_remote"
        service.set_execution_mode("direct")
        assert service.get_execution_mode() == "direct"
        # 清理 meta，恢复默认态（避免影响其他文件对 auto 默认的依赖）
        service.set_execution_mode("")

    def test_queue_mode_enqueues_not_executes(self):
        """queue_remote：run_pipeline 应入队 agent_tasks，不 spawn 线程就地执行（queue_local 已废除）。"""
        self._clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_remote"
        import src.api.event_pipeline as ep
        # 构造最小可路由事件（issue_opened → issue agent）
        raw = json.dumps(_evt_body()).encode()
        headers = {"X-GitCode-Event": "Issue Hook"}
        # 确保路由命中
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO event_route_config (repo_key, event_type, skill) VALUES (?,?,?)",
                ("", "issue_opened", "issue"))
            # 远端引擎 + 会话 runtime_id（新模型：派发按引擎集合/会话主引擎选引擎）
            # 注意：Issue Hook 事件归一化后路由到 issue agent，故会话 agent_type=issue
            c.execute(
                "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
                "VALUES ('hm2q','m2-cred','m2-dev','global','tester','active')")
            _cid = c.lastrowid
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
                "VALUES ('opencode','opencode',?, 'online', datetime('now'), 'global', '', 1, 1)", (_cid,))
            _rid = c.lastrowid
            c.execute(
                "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, enable) "
                "VALUES ('sess-m2-q', 'gitcode:owner/repo', 'issue', ?, 'idle', 1)", (_rid,))
            db.commit()
        result = ep.run_pipeline(raw, headers, account="owner/repo")
        with db_lock:
            tasks = db.cursor().execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0]
            events = db.cursor().execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert tasks >= 1, "queue 模式应入队 agent_tasks"
        assert events >= 1, "应写 events 表"

    def test_direct_mode_executes(self):
        """direct：默认模式，run_pipeline 就地执行（不新增 task）。"""
        self._clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "direct"
        raw = json.dumps(_evt_body()).encode()
        headers = {"X-GitCode-Event": "Issue Hook"}
        ep_result = event_pipeline.run_pipeline(raw, headers, account="owner/repo")
        with db_lock:
            tasks = db.cursor().execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0]
        assert tasks == 0, "direct 模式不应入队 agent_tasks"


class TestTaskStatusAPI:
    def test_status_endpoint(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.deps import get_current_user
        import uuid
        # 用测试 token 依赖
        def _fake_user():
            return {"id": 1, "username": "tester", "nickname": "T"}
        app.dependency_overrides[get_current_user] = _fake_user
        client = TestClient(app)
        tid = service.enqueue_task(
            agent_session_id="sess-api", runtime_id=1, prompt="p",
            repo_key="test/repo", agent_type="chat",
        )
        r = client.get(f"/api/runtime/tasks/{tid}/status")
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        r404 = client.get("/api/runtime/tasks/t_nonexistent/status")
        assert r404.status_code == 404
        app.dependency_overrides.clear()
        with db_lock:
            db.cursor().execute("DELETE FROM agent_tasks")
            db.commit()

    def test_cancel_endpoint(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.deps import get_current_user
        def _fake_user():
            return {"id": 1, "username": "tester"}
        app.dependency_overrides[get_current_user] = _fake_user
        client = TestClient(app)
        tid = service.enqueue_task(
            agent_session_id="sess-cancel", runtime_id=1, prompt="p",
            repo_key="test/repo", agent_type="chat",
        )
        r = client.post(f"/api/runtime/tasks/{tid}/cancel")
        assert r.status_code == 200
        t = service.get_task(tid)
        assert t["status"] == "cancelled"
        app.dependency_overrides.clear()
        with db_lock:
            db.cursor().execute("DELETE FROM agent_tasks")
            db.commit()
