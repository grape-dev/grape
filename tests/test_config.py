"""Tests for config module."""
import pytest
import os
import sys
import json

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ["DEVOPS_DB_PATH"] = os.path.join(os.path.dirname(__file__), "..", "data", "test_sessions.db")


class TestEventRouting:
    def _seed(self):
        """确保测试库 agent_type 表有标准的订阅事件映射"""
        from src.session_manager import db, db_lock
        subscribe_map = {
            "vod": ["vod_signal"],
            "issue": ["issue_opened", "issue_updated", "issue_labeled", "issue_comment"],
            "coding": [],
            "qa": ["pull_request_opened", "pull_request_updated", "note_hook_pr"],
            "sre": ["pull_request_merged", "build_failure", "deploy_start", "deploy_failure", "release_created"],
        }
        with db_lock:
            c = db.cursor()
            for at, events in subscribe_map.items():
                c.execute("INSERT OR REPLACE INTO agent_type (agent_type, display_name, order_num, subscribe_events) VALUES (?,?,?,?)",
                          (at, at, {"vod": 1, "issue": 2, "coding": 3, "qa": 4, "sre": 5}[at], json.dumps(events)))
            db.commit()

    def test_default_fallback(self):
        """未知事件类型应返回空（无匹配 Agent）"""
        from src.api.routing import get_domain_agent
        self._seed()
        assert get_domain_agent("unknown_event_type") == ""

    def test_known_routes(self):
        """已知事件类型路由到正确 Agent（静态映射 + 阶段矩阵默认）"""
        from src.api.routing import get_domain_agent
        self._seed()
        assert get_domain_agent("issue_opened") == "vod"
        assert get_domain_agent("issue_comment") == "issue"
        assert get_domain_agent("issue_labeled") == "issue"
        assert get_domain_agent("pull_request_opened") == "qa"
        assert get_domain_agent("pull_request_updated") == "qa"
        assert get_domain_agent("build_failure") == "sre"
        assert get_domain_agent("deploy_start") == "sre"
        assert get_domain_agent("deploy_failure") == "sre"
        assert get_domain_agent("release_created") == "sre"
        assert get_domain_agent("tag_created") == "sre"
        assert get_domain_agent("vod_signal") == "vod"

    def test_removed_events_not_in_routing(self):
        """push / build_success 等未覆盖事件应回落到空"""
        from src.api.routing import get_domain_agent
        self._seed()
        assert get_domain_agent("push") == ""
        assert get_domain_agent("build_success") == ""

    def test_agent_skill_map(self):
        """get_agent_skill_map 应返回 {event_type: agent_type} 映射"""
        from src.api.routing import get_agent_skill_map
        self._seed()
        m = get_agent_skill_map()
        assert m.get("issue_opened") == "vod"
        assert m.get("build_failure") == "sre"
        assert m.get("vod_signal") == "vod"
        assert "push" not in m
