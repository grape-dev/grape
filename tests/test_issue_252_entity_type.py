"""Issue #252: 实体类型区分测试——issue 与 PR 编号相同时不混淆。

覆盖：
1. _extract_entity_type_from_text 区分 issue/pull_request URL
2. resolve_pr_issue_id 跳过 PR 自引用（编号相同 + URL 为 /pulls/）
3. _num_from_issue_id 排除 #pr_ 前缀
4. _group_events_by_issue 按 entity_type 过滤（PR 事件不混入 issue 分组）
5. append_event 存储 entity_type
6. 向后兼容：旧事件无 entity_type 仍正常分组
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest
from src.session_manager import db, db_lock

REPO = "gitcode:owner/issue252-test"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        try:
            c.execute("DELETE FROM issue_state WHERE repo_key=?", (REPO,))
        except Exception:
            pass
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        try:
            c.execute("DELETE FROM issue_state WHERE repo_key=?", (REPO,))
        except Exception:
            pass
        db.commit()


# ===== 1. _extract_entity_type_from_text =====

class TestExtractEntityType:
    def test_issue_url(self):
        from src.api.event_store import _extract_entity_type_from_text
        assert _extract_entity_type_from_text("see https://gitcode.com/owner/repo/issues/224") == "issue"

    def test_pull_url(self):
        from src.api.event_store import _extract_entity_type_from_text
        assert _extract_entity_type_from_text("see https://gitcode.com/owner/repo/pulls/224") == "pull_request"

    def test_merge_request_url(self):
        from src.api.event_store import _extract_entity_type_from_text
        assert _extract_entity_type_from_text("see /merge_requests/224") == "pull_request"

    def test_hash_only_no_type(self):
        from src.api.event_store import _extract_entity_type_from_text
        assert _extract_entity_type_from_text("fixes #224") == ""

    def test_empty(self):
        from src.api.event_store import _extract_entity_type_from_text
        assert _extract_entity_type_from_text("") == ""


# ===== 2. resolve_pr_issue_id 跳过 PR 自引用 =====

class TestResolvePrIssueIdSelfRef:
    def test_pr_title_with_issue_url_resolves(self):
        """PR title 含 /issues/224 → 解析为 #224（关联 Issue）"""
        from src.api.event_pipeline import resolve_pr_issue_id
        payload = {
            "object_attributes": {
                "iid": 224,
                "title": "fix: 修复 /issues/224 的问题",
                "source_branch": "feat/fix",
            },
        }
        result = resolve_pr_issue_id(payload)
        assert result == "#224"

    def test_pr_title_with_pull_url_self_ref_skipped(self):
        """PR title 含 /pulls/224（自身编号）→ 跳过自引用，返回空"""
        from src.api.event_pipeline import resolve_pr_issue_id
        payload = {
            "object_attributes": {
                "iid": 224,
                "title": "PR /pulls/224 描述",
                "source_branch": "feat/fix",
            },
        }
        result = resolve_pr_issue_id(payload)
        assert result == ""

    def test_pr_title_with_hash_not_self_ref(self):
        """PR title 含 #224（自身编号）但无 URL 路径 → 无法判定类型，仍采用（向后兼容）"""
        from src.api.event_pipeline import resolve_pr_issue_id
        payload = {
            "object_attributes": {
                "iid": 224,
                "title": "fixes #224",
                "source_branch": "feat/fix",
            },
        }
        result = resolve_pr_issue_id(payload)
        assert result == "#224"

    def test_pr_title_with_pull_url_different_num_resolves(self):
        """PR title 含 /pulls/300（非自身编号）→ 不跳过（可能关联其他 PR）"""
        from src.api.event_pipeline import resolve_pr_issue_id
        payload = {
            "object_attributes": {
                "iid": 224,
                "title": "rebase from /pulls/300",
                "source_branch": "feat/fix",
            },
        }
        result = resolve_pr_issue_id(payload)
        assert result == "#300"

    def test_pr_with_linked_issues_list(self):
        """PR payload.issues 列表 → 正常提取关联 Issue 编号"""
        from src.api.event_pipeline import resolve_pr_issue_id
        payload = {
            "object_attributes": {
                "iid": 224,
                "title": "some title",
                "source_branch": "feat/fix",
            },
            "issues": [{"number": 1762}],
        }
        result = resolve_pr_issue_id(payload)
        assert result == "#1762"

    def test_pr_branch_issue_n(self):
        """PR 分支名 issue-224 → 解析为 #224"""
        from src.api.event_pipeline import resolve_pr_issue_id
        payload = {
            "object_attributes": {
                "iid": 300,
                "title": "",
                "source_branch": "issue-224",
            },
        }
        result = resolve_pr_issue_id(payload)
        assert result == "#224"


# ===== 3. _num_from_issue_id 排除 #pr_ 前缀 =====

class TestNumFromIssueId:
    def test_normal_issue_id(self):
        from src.api.routes.issues import _num_from_issue_id
        assert _num_from_issue_id("#224") == "224"

    def test_pr_subscription_id_excluded(self):
        from src.api.routes.issues import _num_from_issue_id
        assert _num_from_issue_id("#pr_224_scene123") == ""

    def test_pr_subscription_id_no_scene(self):
        from src.api.routes.issues import _num_from_issue_id
        assert _num_from_issue_id("#pr_224") == ""

    def test_empty(self):
        from src.api.routes.issues import _num_from_issue_id
        assert _num_from_issue_id("") == ""

    def test_zero(self):
        from src.api.routes.issues import _num_from_issue_id
        assert _num_from_issue_id("#0") == "0"


# ===== 4. _group_events_by_issue 按 entity_type 过滤 =====

class TestGroupEventsByEntityType:
    def test_pr_events_excluded_from_issue_group(self):
        """entity_type='pull_request' 的事件不混入 issue 分组"""
        from src.api.event_store import append_event
        from src.api.routes.issues import _group_events_by_issue

        # 写入 issue 事件
        append_event(repo_key=REPO, event_type="issue_opened", title="Issue #224",
                     issue_id="#224", agent_type="vod", entity_type="issue")
        # 写入 PR 事件（同编号 224）
        append_event(repo_key=REPO, event_type="pull_request_opened", title="PR #224",
                     issue_id="#224", agent_type="coding", entity_type="pull_request")

        groups = _group_events_by_issue([REPO])
        events_224 = groups.get("224", [])
        assert len(events_224) == 1, f"expected 1 issue event, got {len(events_224)}"
        assert events_224[0]["event_type"] == "issue_opened"

    def test_old_events_without_entity_type_included(self):
        """无 entity_type 的旧事件仍正常分组（向后兼容）"""
        from src.api.event_store import append_event
        from src.api.routes.issues import _group_events_by_issue

        # 模拟旧事件（不传 entity_type，默认空串）
        append_event(repo_key=REPO, event_type="issue_opened", title="Old Issue #224",
                     issue_id="#224", agent_type="vod")

        groups = _group_events_by_issue([REPO])
        events_224 = groups.get("224", [])
        assert len(events_224) == 1
        assert events_224[0]["event_type"] == "issue_opened"

    def test_pr_subscription_events_excluded(self):
        """#pr_ 前缀的 PR 订阅事件不混入 issue 分组"""
        from src.api.event_store import append_event
        from src.api.routes.issues import _group_events_by_issue

        # issue 事件
        append_event(repo_key=REPO, event_type="issue_opened", title="Issue #224",
                     issue_id="#224", agent_type="vod", entity_type="issue")
        # PR 订阅事件（伪 issue_id）
        append_event(repo_key=REPO, event_type="a2a_event", title="PR sub QA",
                     issue_id="#pr_224_scene1", agent_type="qa", entity_type="pull_request",
                     scene_id="scene1")

        groups = _group_events_by_issue([REPO])
        events_224 = groups.get("224", [])
        assert len(events_224) == 1, f"PR subscription event should not be in issue group, got {len(events_224)}"


# ===== 5. append_event 存储 entity_type =====

class TestAppendEventEntityType:
    def test_entity_type_stored(self):
        from src.api.event_store import append_event
        seq = append_event(repo_key=REPO, event_type="pull_request_opened",
                           title="PR #224", issue_id="#224", agent_type="coding",
                           entity_type="pull_request")
        assert seq > 0
        with db_lock:
            c = db.cursor()
            c.execute("SELECT entity_type FROM events WHERE seq=?", (seq,))
            row = c.fetchone()
        assert row is not None
        assert row[0] == "pull_request"

    def test_entity_type_default_empty(self):
        from src.api.event_store import append_event
        seq = append_event(repo_key=REPO, event_type="issue_opened",
                           title="Issue #224", issue_id="#224", agent_type="vod")
        assert seq > 0
        with db_lock:
            c = db.cursor()
            c.execute("SELECT entity_type FROM events WHERE seq=?", (seq,))
            row = c.fetchone()
        assert row is not None
        assert row[0] == ""


# ===== 6. 编号相同场景端到端验证 =====

class TestSameNumberE2E:
    def test_issue_and_pr_same_number_separated(self):
        """issue #224 和 PR #224 同时存在时，事件分组不混淆"""
        from src.api.event_store import append_event
        from src.api.routes.issues import _group_events_by_issue, _query_pr_subscription_events

        # issue 事件
        append_event(repo_key=REPO, event_type="issue_opened", title="Issue #224",
                     issue_id="#224", agent_type="vod", entity_type="issue")
        append_event(repo_key=REPO, event_type="issue_comment", title="Comment on #224",
                     issue_id="#224", agent_type="issue", entity_type="issue")
        # PR 事件（同编号）
        append_event(repo_key=REPO, event_type="pull_request_opened", title="PR #224",
                     issue_id="#224", agent_type="coding", entity_type="pull_request")

        groups = _group_events_by_issue([REPO])
        events_224 = groups.get("224", [])

        # issue 分组只含 2 个 issue 事件，PR 事件被排除
        assert len(events_224) == 2
        for ev in events_224:
            assert ev["event_type"] in ("issue_opened", "issue_comment")
