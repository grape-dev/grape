"""Pipeline 单元测试：_get_issue_pipeline 的 latest_event + current_owner 逻辑"""
import sys, os

# 强制使用独立测试库
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

from unittest.mock import patch
import pytest
from src.api.routes.pipeline import _get_issue_pipeline, _running_issue_ids, AGENT_ORDER, STAGE_STATUS_MAP
from src.session_manager import db, db_lock


def _ev(seq, issue_id, event_type, agent_type, title, status, actor, created_at="2026-08-04 10:00:00", payload=None):
    return {"seq": seq, "issue_id": issue_id, "event_type": event_type,
            "agent_type": agent_type, "title": title, "status": status,
            "created_at": created_at, "actor": actor, "payload": payload}


def _mock_bots():
    """mock get_all_bot_usernames + get_agent_display_name（函数内 import，用 patch）"""
    return patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
           patch("src.api.event_pipeline.get_agent_display_name", side_effect=lambda at: {
               "issue": "Issue Agent", "coding": "Coding Agent", "qa": "QA Agent",
               "sre": "SRE Agent", "vod": "VOD Agent"}.get(at, at))


class TestPipeline:
    def test_single_agent_completed(self):
        events = [_ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot")]
        with _mock_bots()[0], _mock_bots()[1]:
            p = _get_issue_pipeline("owner/repo", "#1", events)
        assert p is not None
        assert p["statuses"] == [0, 2, 0, 0, 0], p["statuses"]  # issue 是第 2 个 agent
        assert p["stages"][1]["latest_event"]["title"] == "issue_opened"
        assert p["current_owner"] == "Issue Agent"

    def test_bot_actor_returns_agent_name(self):
        events = [_ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot")]
        with _mock_bots()[0], _mock_bots()[1]:
            p = _get_issue_pipeline("owner/repo", "#1", events)
        assert p["current_owner"] == "Issue Agent"

    def test_user_actor_returns_username(self):
        events = [_ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "Jason7602")]
        with _mock_bots()[0], _mock_bots()[1]:
            p = _get_issue_pipeline("owner/repo", "#1", events)
        assert p["current_owner"] == "Jason7602"

    def test_multiple_agents_chain(self):
        events = [
            _ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot"),
            _ev(2, "#1", "a2a_event", "coding", "A2A: coding", "completed", "grape_bot"),
            _ev(3, "#1", "pull_request_opened", "qa", "PR #1", "running", "grape_bot"),
        ]
        with _mock_bots()[0], _mock_bots()[1]:
            p = _get_issue_pipeline("owner/repo", "#1", events)
        assert p["statuses"] == [0, 2, 2, 1, 0], p["statuses"]
        assert p["result"] == "Processing"
        assert p["current_owner"] == "QA Agent"  # 最后一个 running 阶段

    def test_running_stage_is_current_owner(self):
        events = [
            _ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot"),
            _ev(2, "#1", "pull_request_opened", "qa", "PR #1", "running", "grape_bot"),
        ]
        with _mock_bots()[0], _mock_bots()[1]:
            p = _get_issue_pipeline("owner/repo", "#1", events)
        assert p["current_owner"] == "QA Agent"

    def test_no_events_returns_none(self):
        p = _get_issue_pipeline("owner/repo", "#9", [])
        assert p is None

    def test_latest_event_is_last(self):
        events = [
            _ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot"),
            _ev(2, "#1", "issue_comment", "issue", "评论处理", "completed", "Jason7602"),
        ]
        with _mock_bots()[0], _mock_bots()[1]:
            p = _get_issue_pipeline("owner/repo", "#1", events)
        assert p["stages"][1]["latest_event"]["seq"] == 2

    def test_failed_stage(self):
        events = [
            _ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot"),
            _ev(2, "#1", "pull_request_opened", "qa", "PR #1", "failed", "grape_bot"),
        ]
        with _mock_bots()[0], _mock_bots()[1]:
            p = _get_issue_pipeline("owner/repo", "#1", events)
        assert p["result"] == "Failed"
        assert p["statuses"] == [0, 2, 0, 3, 0], p["statuses"]

    def test_all_done_released(self):
        events = []
        seq = 1
        for at, et in [("vod", "vod_signal"), ("issue", "issue_opened"), ("coding", "a2a_event"),
                       ("qa", "pull_request_opened"), ("sre", "pull_request_merged")]:
            events.append(_ev(seq, "#1", et, at, et, "completed", "grape_bot"))
            seq += 1
        with _mock_bots()[0], _mock_bots()[1]:
            p = _get_issue_pipeline("owner/repo", "#1", events)
        assert p["result"] == "Released"

    def test_missing_agent_stage(self):
        events = [_ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot")]
        with _mock_bots()[0], _mock_bots()[1]:
            p = _get_issue_pipeline("owner/repo", "#1", events)
        # SRE 无事件 → statuses 为 0，latest_event 为 None
        assert p["statuses"][4] == 0
        assert p["stages"][4]["latest_event"] is None

    # ===== Issue assignee 责任人 =====

    def test_assignee_returns_feishu_name(self):
        """有 assignee 且有飞书映射 → 责任人 = 飞书昵称"""
        events = [_ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot",
                      payload={"object_attributes": {"assignee": "younger_eric"}})]
        with _mock_bots()[0], _mock_bots()[1], \
             patch("src.api.routes.pipeline._resolve_feishu_name", return_value="吴扬"):
            p = _get_issue_pipeline("owner/repo", "#1", events)
        assert p["current_owner"] == "吴扬"

    def test_assignee_no_feishu_mapping_returns_username(self):
        """有 assignee 无飞书映射 → 责任人 = GitCode 用户名"""
        events = [_ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot",
                      payload={"object_attributes": {"assignee": "younger_eric"}})]
        with _mock_bots()[0], _mock_bots()[1], \
             patch("src.api.routes.pipeline._resolve_feishu_name", side_effect=lambda u: u):
            p = _get_issue_pipeline("owner/repo", "#1", events)
        assert p["current_owner"] == "younger_eric"

    def test_assignee_dict_username(self):
        """assignee 为 dict（含 username 字段）也能解析"""
        events = [_ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot",
                      payload={"issue": {"assignee": {"username": "alice"}}})]
        with _mock_bots()[0], _mock_bots()[1], \
             patch("src.api.routes.pipeline._resolve_feishu_name", side_effect=lambda u: u):
            p = _get_issue_pipeline("owner/repo", "#1", events)
        assert p["current_owner"] == "alice"

    def test_assignee_change_takes_latest(self):
        """assignee 变更 → 取最新事件的 assignee"""
        events = [
            _ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot",
                payload={"object_attributes": {"assignee": "old_owner"}}),
            _ev(2, "#1", "issue_updated", "issue", "issue_updated", "completed", "grape_bot",
                payload={"object_attributes": {"assignee": "new_owner"}}),
        ]
        with _mock_bots()[0], _mock_bots()[1], \
             patch("src.api.routes.pipeline._resolve_feishu_name", side_effect=lambda u: u):
            p = _get_issue_pipeline("owner/repo", "#1", events)
        assert p["current_owner"] == "new_owner"

    def test_assignee_json_string_payload(self):
        """payload 为 JSON 字符串（DB 存储形态）也能解析"""
        import json as _json
        events = [_ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot",
                      payload=_json.dumps({"object_attributes": {"assignee": "bob"}}))]
        with _mock_bots()[0], _mock_bots()[1], \
             patch("src.api.routes.pipeline._resolve_feishu_name", side_effect=lambda u: u):
            p = _get_issue_pipeline("owner/repo", "#1", events)
        assert p["current_owner"] == "bob"

    def test_no_assignee_falls_back_to_actor(self):
        """无 assignee → 回退现有 actor 推断（bot → Agent 名）"""
        events = [_ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot")]
        with _mock_bots()[0], _mock_bots()[1]:
            p = _get_issue_pipeline("owner/repo", "#1", events)
        assert p["current_owner"] == "Issue Agent"


class TestExecutingDerivation:
    """前端 B：executing 由 agent_tasks 实时派生（见 multi-issue-isolation-design.md §4.4）"""

    @pytest.fixture(autouse=True)
    def _clean_tasks(self):
        with db_lock:
            db.cursor().execute("DELETE FROM agent_tasks WHERE repo_key='gitcode:org/pipeline-test'")
            db.commit()
        yield
        with db_lock:
            db.cursor().execute("DELETE FROM agent_tasks WHERE repo_key='gitcode:org/pipeline-test'")
            db.commit()

    def test_running_issue_detected(self):
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, issue_id, repo_key, agent_type, status, prompt) "
                      "VALUES ('t1', 's1', 1, '#5', 'gitcode:org/pipeline-test', 'coding', 'running', 'p')")
            db.commit()
        assert "#5" in _running_issue_ids("gitcode:org/pipeline-test")

    def test_completed_issue_not_running(self):
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, issue_id, repo_key, agent_type, status, prompt) "
                      "VALUES ('t2', 's1', 1, '#6', 'gitcode:org/pipeline-test', 'coding', 'completed', 'p')")
            db.commit()
        assert "#6" not in _running_issue_ids("gitcode:org/pipeline-test")

    def test_empty_repo(self):
        assert _running_issue_ids("gitcode:org/pipeline-test") == set()

    def test_pipeline_issues_sorted_latest_first(self):
        """最新活动的 issue 排最前（原为 issue 首次出现顺序，最早 issue 恒在前）。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.deps import get_current_user

        def _fake_user():
            return {"id": 1, "username": "tester", "nickname": "T"}
        app.dependency_overrides[get_current_user] = _fake_user

        # 清空该 repo 的 events
        with db_lock:
            db.cursor().execute("DELETE FROM events WHERE repo_key='gitcode:org/pipeline-test'")
            db.commit()

        # 插入两个 issue 的事件：issue A 先出现（seq 1-2），issue B 后出现（seq 3）
        # B 最新活动 seq=3 > A 最新活动 seq=2 → 排序后 B 应在前
        rows = [
            (1, "#A", "issue_opened", "vod", "tA", "completed", "alice"),
            (2, "#A", "issue_comment", "vod", "tA", "completed", "alice"),
            (3, "#B", "issue_opened", "vod", "tB", "completed", "alice"),
        ]
        with db_lock:
            c = db.cursor()
            for seq, iid, et, ag, title, status, actor in rows:
                c.execute(
                    "INSERT INTO events (seq, repo_key, issue_id, event_type, agent_type, title, summary, status, actor, source, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                    (seq, "gitcode:org/pipeline-test", iid, et, ag, title, title, status, actor, "gitcode"))
            db.commit()

        client = TestClient(app)
        r = client.get("/api/pipeline/org/pipeline-test")
        assert r.status_code == 200, r.text
        data = r.json()
        # active 应为最新活动的 issue（B，seq=3 最新）
        assert data["active"]["issue_id"] == "#B", \
            f"最新活动的 issue 应排最前(active), got {data['active']['issue_id']}"
        # history 第一条应为 A（次新）
        assert data["history"][0]["issue_id"] == "#A", \
            f"history 第一条应为 A, got {[h['issue_id'] for h in data['history']]}"
        app.dependency_overrides.clear()
        with db_lock:
            db.cursor().execute("DELETE FROM events WHERE repo_key='gitcode:org/pipeline-test'")
            db.commit()
