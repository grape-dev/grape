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
        # 恢复 direct，避免污染其他测试
        os.environ["DEVOPS_EXECUTION_MODE"] = "direct"

    def _clean(self):
        with db_lock:
            db.cursor().execute("DELETE FROM agent_tasks")
            db.cursor().execute("DELETE FROM events")
            # 清掉可能由其他 pipeline 测试残留的会话/阶段行，保证本测试不依赖共享库顺序
            db.cursor().execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
            db.cursor().execute("DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:owner/repo%'")
            db.commit()

    def test_mode_getter_meta(self):
        self._clean()
        service.set_execution_mode("queue_local")
        assert service.get_execution_mode() == "queue_local"
        service.set_execution_mode("direct")
        assert service.get_execution_mode() == "direct"

    def test_queue_mode_enqueues_not_executes(self):
        """queue_local：run_pipeline 应入队 agent_tasks，不 spawn 线程就地执行。"""
        self._clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_local"
        import src.api.event_pipeline as ep
        # 构造最小可路由事件（issue_opened → issue agent）
        raw = json.dumps(_evt_body()).encode()
        headers = {"X-GitCode-Event": "Issue Hook"}
        # 确保路由命中
        with db_lock:
            db.cursor().execute(
                "INSERT OR REPLACE INTO event_route_config (repo_key, event_type, skill) VALUES (?,?,?)",
                ("", "issue_opened", "issue"))
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
