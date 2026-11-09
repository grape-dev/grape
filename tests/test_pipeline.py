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


# ===== Issue #542：_events_by_issue 索引路径 + history 截断 =====

class TestEventsByIssueIndex:
    """Issue #542：_get_issue_pipeline 走 events_by_issue 索引（O(issues×events) → O(events)）。

    索引路径与线性过滤路径结果必须完全一致（行为兼容）。
    """

    def _build_index(self, events):
        idx = {}
        for e in events:
            idx.setdefault(e.get("issue_id"), []).append(e)
        return idx

    def test_index_path_equals_linear_path(self):
        events = [
            _ev(1, "#1", "issue_opened", "issue", "Fix login bug", "completed", "grape_bot"),
            _ev(2, "#1", "a2a_event", "coding", "A2A: coding", "completed", "grape_bot"),
            _ev(3, "#2", "issue_opened", "issue", "Add gzip support", "completed", "grape_bot"),
            _ev(4, "#3", "issue_opened", "issue", "Cache branches", "completed", "grape_bot"),
            _ev(5, "#3", "pull_request_opened", "qa", "PR #3", "running", "grape_bot"),
        ]
        with _mock_bots()[0], _mock_bots()[1]:
            for iid in ("#1", "#2", "#3"):
                p_linear = _get_issue_pipeline("owner/repo", iid, events)
                p_index = _get_issue_pipeline("owner/repo", iid, events,
                                              events_by_issue=self._build_index(events))
                assert p_linear == p_index, f"index path diverges for {iid}"

    def test_index_missing_issue_returns_none(self):
        events = [_ev(1, "#1", "issue_opened", "issue", "Fix login bug", "completed", "grape_bot")]
        with _mock_bots()[0], _mock_bots()[1]:
            p = _get_issue_pipeline("owner/repo", "#99", events,
                                    events_by_issue=self._build_index(events))
        assert p is None

    def test_index_empty_list_returns_none(self):
        with _mock_bots()[0], _mock_bots()[1]:
            p = _get_issue_pipeline("owner/repo", "#1", [], events_by_issue={})
        assert p is None

    def test_pr_title_fallback_via_index(self):
        """PR 订阅伪 ID 标题兜底：索引路径按索引键匹配同 PR 号事件（不遍历全量 events）。"""
        events = [
            # 目标伪 ID：标题为占位符（事件类型名）
            _ev(1, "#pr_7_scene-a", "note_hook_pr", "qa", "note_hook_pr", "completed", "grape_bot"),
            # 同 PR 号另一伪 ID：带真实标题 → 兜底应命中
            _ev(2, "#pr_7", "pull_request_opened", "qa", "Fix CI pipeline", "completed", "grape_bot"),
            # 无关 issue：不参与兜底
            _ev(3, "#9", "issue_opened", "issue", "Other issue", "completed", "grape_bot"),
        ]
        with _mock_bots()[0], _mock_bots()[1]:
            p_index = _get_issue_pipeline("owner/repo", "#pr_7_scene-a", events,
                                          events_by_issue=self._build_index(events))
            p_linear = _get_issue_pipeline("owner/repo", "#pr_7_scene-a", events)
        assert p_index is not None and p_linear is not None
        assert p_index["title"] == "Fix CI pipeline"
        assert p_index == p_linear


class TestHistoryLimit:
    """Issue #542：pipeline 接口 history 截断（默认最近 history_limit 条，history_total 保留全量数）。"""

    OWNER = "test542p"
    REPO = "pipe"
    REPO_KEY = "gitcode:test542p/pipe"

    def _jwt_headers(self):
        from src.auth import grape_account_db
        from src.api.jwt_utils import create_access_token
        from src.auth.grape_account_db import get_token_version
        uid = grape_account_db.create_grape_account("test_542_pipe", email=None, password="test123")
        if not uid:
            uid = grape_account_db.get_grape_account_by_username("test_542_pipe")
        tok = create_access_token(uid["id"], uid["username"], get_token_version(uid["id"]))
        return uid, {"Authorization": f"Bearer {tok}"}

    def _seed(self, n_issues):
        """n 个 issue 各 1 条事件（seq 自动递增，按插入顺序 = 活跃顺序）。"""
        with db_lock:
            c = db.cursor()
            for i in range(1, n_issues + 1):
                c.execute(
                    "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, summary, status, actor, source, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
                    (self.REPO_KEY, f"#{i}", "issue_opened", "issue",
                     f"Real title {i}", f"Summary {i}", "completed", "alice", "gitcode"))
            db.commit()

    def _clean(self):
        with db_lock:
            db.cursor().execute("DELETE FROM events WHERE repo_key=?", (self.REPO_KEY,))
            db.cursor().execute("DELETE FROM issue WHERE repo_key=?", (self.REPO_KEY,))
            db.cursor().execute("DELETE FROM issue_state WHERE repo_key=?", (self.REPO_KEY,))
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (self.REPO_KEY,))
            db.cursor().execute("DELETE FROM user_repos WHERE repo_key=?", (self.REPO_KEY,))
            db.commit()

    def setup_method(self):
        self._clean()
        self._uid, self._headers = self._jwt_headers()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id) "
                      "VALUES (?,?,?,?,1)", (self.REPO_KEY, self.OWNER, self.REPO, f"{self.OWNER}/{self.REPO}"))
            c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, is_join, is_admin) VALUES (?,?,1,1)",
                      (self._uid["id"], self.REPO_KEY))
            db.commit()

    def teardown_method(self):
        self._clean()

    def test_history_truncated_with_total(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._seed(6)  # active=最新 1 条，history=5 条
        client = TestClient(app)
        r = client.get(f"/api/pipeline/{self.OWNER}/{self.REPO}?history_limit=3", headers=self._headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert len(d["history"]) == 3
        assert d["history_total"] == 5
        assert d["history_limit"] == 3
        # 截断保留最近优先：history[0] 应为次新 issue（#5）
        assert d["history"][0]["issue_id"] == "#5"
        # 统计口径不变（全量）：today_count 覆盖全部 6 条
        assert d["today_count"] == 6

    def test_history_limit_default_no_overtruncate(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._seed(4)  # history=3 < 默认 50 → 不截断
        client = TestClient(app)
        r = client.get(f"/api/pipeline/{self.OWNER}/{self.REPO}", headers=self._headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert len(d["history"]) == 3
        assert d["history_total"] == 3
        assert d["history_limit"] == 50
