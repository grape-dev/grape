"""Issue #89: tasks/events 数据一致性修复测试。

覆盖：
1. append_skipped_event 写入含 issue_id
2. pipeline 接口返回 tasks_without_events 差异 flag
3. create_task_via_issue 后立即有事件
4. resolve_issue_id 子任务 payload（含 issues 键）不再返回空 → 无 #0
5. _is_pr_payload 修复：Issue Hook 含 issues 交叉引用键不误判为 PR
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from unittest.mock import patch
import pytest

from src.session_manager import db, db_lock

REPO = "gitcode:owner/issue89-test"


def _cleanup():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM event_skipped WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        try:
            c.execute("DELETE FROM issue_state WHERE repo_key=?", (REPO,))
        except Exception:
            pass
        db.commit()


def setup_function():
    _cleanup()


def teardown_function():
    _cleanup()


# ===== 1. append_skipped_event 含 issue_id =====

class TestAppendSkippedEventWithIssueId:
    def test_skipped_event_has_issue_id(self):
        """append_skipped_event 写入 event_skipped 表含 issue_id 列"""
        from src.api.event_store import append_skipped_event
        append_skipped_event(REPO, "issue_opened", "agent_disabled", "vod", issue_id="#42")
        with db_lock:
            c = db.cursor()
            c.execute("SELECT issue_id, reason, detail FROM event_skipped WHERE repo_key=? ORDER BY id DESC LIMIT 1", (REPO,))
            row = c.fetchone()
        assert row is not None
        assert row[0] == "#42"
        assert row[1] == "agent_disabled"
        assert row[2] == "vod"

    def test_skipped_event_empty_issue_id_default(self):
        """append_skipped_event 不传 issue_id 时默认空字符串"""
        from src.api.event_store import append_skipped_event
        append_skipped_event(REPO, "push", "no_route")
        with db_lock:
            c = db.cursor()
            c.execute("SELECT issue_id FROM event_skipped WHERE repo_key=? ORDER BY id DESC LIMIT 1", (REPO,))
            row = c.fetchone()
        assert row is not None
        assert row[0] == ""


# ===== 2. pipeline 接口返回 tasks_without_events =====

class TestPipelineTasksWithoutEvents:
    def test_tasks_without_events_flag(self):
        """tasks 有记录但 events 无记录 → has_tasks_without_events=True"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.deps import get_current_user, get_current_user_or_api_key
        from src.api.routes import tasks as tasks_mod

        # 在 tasks 表插入一条任务，但不创建 events
        with patch.object(tasks_mod, "_get_user_id_by_gitcode", return_value=1), \
             patch.object(tasks_mod, "_get_issue_title", return_value="Test Task"):
            tasks_mod.ensure_issue_task(REPO, 100, title="Test Task", assignee="alice",
                                        event_type="issue_opened")

        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "tester", "auth_type": "api_key"}
        app.dependency_overrides[get_current_user_or_api_key] = lambda: {"id": 1, "username": "tester", "auth_type": "api_key"}
        try:
            client = TestClient(app)
            r = client.get("/api/pipeline/owner/issue89-test")
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["has_tasks_without_events"] is True
            twe = data["tasks_without_events"]
            assert any(t["issue_id"] == "#100" for t in twe), f"expected #100 in {twe}"
        finally:
            app.dependency_overrides.clear()

    def test_no_tasks_without_events_when_events_exist(self):
        """tasks 有记录且 events 也有记录 → has_tasks_without_events=False"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.deps import get_current_user, get_current_user_or_api_key
        from src.api.event_store import append_event
        from src.api.routes import tasks as tasks_mod

        with patch.object(tasks_mod, "_get_user_id_by_gitcode", return_value=1), \
             patch.object(tasks_mod, "_get_issue_title", return_value="Test Task 2"):
            tasks_mod.ensure_issue_task(REPO, 101, title="Test Task 2", assignee="bob",
                                        event_type="issue_opened")

        # 创建对应的 event
        append_event(repo_key=REPO, event_type="issue_opened", title="Test Task 2",
                     issue_id="#101", agent_type="vod", status="completed")

        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "tester", "auth_type": "api_key"}
        app.dependency_overrides[get_current_user_or_api_key] = lambda: {"id": 1, "username": "tester", "auth_type": "api_key"}
        try:
            client = TestClient(app)
            r = client.get("/api/pipeline/owner/issue89-test")
            assert r.status_code == 200
            data = r.json()
            # #101 有 events → 不在 tasks_without_events 中
            twe = data.get("tasks_without_events", [])
            assert not any(t["issue_id"] == "#101" for t in twe), f"#101 should not be in {twe}"
        finally:
            app.dependency_overrides.clear()


# ===== 3. create_task_via_issue 后立即有事件 =====

class TestCreateTaskTriggersEvent:
    def test_create_task_creates_event(self):
        """create_task_via_issue 成功后 events 表立即有 issue_opened 事件"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.deps import get_current_user, get_current_user_or_api_key

        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "tester", "auth_type": "session_token"}
        app.dependency_overrides[get_current_user_or_api_key] = lambda: {"id": 1, "username": "tester"}
        try:
            with patch("src.api.routes.tasks.get_user_token", return_value="fake_token"), \
                 patch("src.api.deps.get_user_token_with_refresh", return_value="fake_token"), \
                 patch("src.api.routes.tasks._find_open_issue_by_title", return_value=None), \
                 patch("src.api.routes.tasks._post_gitcode_issue", return_value={"number": 200}), \
                 patch("src.api.routes.tasks._get_user_id_by_gitcode", return_value=1), \
                 patch("src.api.routes.tasks._get_issue_title", return_value=""):
                client = TestClient(app)
                r = client.post("/api/issues/owner/issue89-test", json={
                    "repo_key": REPO,
                    "title": "Issue89 Test Task",
                    "description": "Test description",
                })
                assert r.status_code == 200, r.text
                assert r.json()["created"] is True

            # 验证 events 表有 issue_opened 事件
            with db_lock:
                c = db.cursor()
                c.execute("SELECT event_type, issue_id, agent_type, status FROM events WHERE repo_key=? AND issue_id='#200' ORDER BY seq DESC LIMIT 1", (REPO,))
                row = c.fetchone()
            assert row is not None, "create_task_via_issue 应立即创建事件"
            assert row[0] == "issue_opened"
            assert row[1] == "#200"
            assert row[2] == "vod"
            assert row[3] == "running"
        finally:
            app.dependency_overrides.clear()


# ===== 4. resolve_issue_id 子任务 payload 不再返回空 =====

class TestResolveIssueIdSubtask:
    def test_issue_hook_with_issues_key_still_resolves(self):
        """GitCode Issue Hook payload 含 issues 交叉引用键 → resolve_issue_id 仍能解析"""
        from src.api.event_pipeline import resolve_issue_id
        # 模拟 GitCode Issue Hook payload（子任务 Issue 创建时）
        payload = {
            "object_attributes": {
                "iid": 55,
                "action": "open",
                "title": "子任务 #55",
                "description": "## 父任务 #50",
            },
            "issues": [{"number": 50}],  # 交叉引用（父任务），旧逻辑会误判为 PR
            "project": {"path_with_namespace": "owner/repo"},
        }
        result = resolve_issue_id(payload)
        assert result == "#55", f"expected #55, got {result}"

    def test_issue_hook_noteable_type_issue(self):
        """Note Hook noteable_type=Issue → resolve_issue_id 正确解析"""
        from src.api.event_pipeline import resolve_issue_id
        payload = {
            "object_attributes": {
                "noteable_type": "Issue",
                "noteable_iid": 77,
                "note": "评论内容",
            },
        }
        result = resolve_issue_id(payload)
        assert result == "#77", f"expected #77, got {result}"


# ===== 5. _is_pr_payload 修复 =====

class TestIsPrPayloadFix:
    def test_issue_hook_with_issues_key_not_pr(self):
        """Issue Hook 含 issues 键但无 PR 特有字段 → 不是 PR payload"""
        from src.api.event_pipeline import _is_pr_payload
        payload = {
            "object_attributes": {"iid": 55, "action": "open"},
            "issues": [{"number": 50}],
        }
        assert _is_pr_payload(payload) is False

    def test_pr_with_merge_request_key_is_pr(self):
        """PR 事件含 merge_request 键 → 是 PR payload"""
        from src.api.event_pipeline import _is_pr_payload
        payload = {
            "object_attributes": {"iid": 10, "source_branch": "feat/x"},
            "merge_request": {"iid": 10},
        }
        assert _is_pr_payload(payload) is True

    def test_pr_with_source_branch_is_pr(self):
        """object_attributes 含 source_branch → 是 PR payload"""
        from src.api.event_pipeline import _is_pr_payload
        payload = {
            "object_attributes": {"iid": 10, "source_branch": "feat/x", "target_branch": "main"},
        }
        assert _is_pr_payload(payload) is True

    def test_note_hook_issue_not_pr(self):
        """Note Hook noteable_type=Issue → 不是 PR payload"""
        from src.api.event_pipeline import _is_pr_payload
        payload = {
            "object_attributes": {"noteable_type": "Issue", "noteable_iid": 77},
        }
        assert _is_pr_payload(payload) is False
