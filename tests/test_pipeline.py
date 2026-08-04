"""Pipeline 单元测试：_get_issue_pipeline 的 latest_event + current_owner 逻辑"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 强制使用独立测试库
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

from unittest.mock import patch
from src.api.routes.pipeline import _get_issue_pipeline, AGENT_ORDER, STAGE_STATUS_MAP


def _ev(seq, issue_id, event_type, agent_type, title, status, actor, created_at="2026-08-04 10:00:00"):
    return {"seq": seq, "issue_id": issue_id, "event_type": event_type,
            "agent_type": agent_type, "title": title, "status": status,
            "created_at": created_at, "actor": actor}


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
