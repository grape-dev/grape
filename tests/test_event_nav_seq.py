"""Issue #224: 事件详情 prev_seq/next_seq 查询 repo_key 过滤测试。

验证：
- prev_seq/next_seq 仅返回同 repo_key 同 agent_type 的事件
- 跨仓库同 agent_type 事件不出现在导航结果中
- 同仓库不同 agent_type 事件不出现在导航结果中
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.session_manager import db, db_lock


_REPO_A = "gitcode:org/repo-a"
_REPO_B = "gitcode:org/repo-b"
_TEST_USER = {"id": 1, "username": "test", "auth_type": "session_token"}


@pytest.fixture(autouse=True)
def _clean_events():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.commit()


def _insert_event(seq, repo_key, agent_type, status="completed"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, agent_type, title, issue_id, "
            "payload, status, result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (seq, repo_key, "issue_opened", agent_type, "test event", "#1", "{}", status, "done"),
        )
        db.commit()


class TestPrevNextSeqRepoKeyFilter:
    """Issue #224: prev_seq/next_seq 查询必须过滤 repo_key。"""

    def test_prev_seq_same_repo(self):
        """同仓库同 agent_type → prev_seq 指向上一个事件。"""
        _insert_event(100, _REPO_A, "vod")
        _insert_event(101, _REPO_A, "vod")
        _insert_event(102, _REPO_A, "vod")

        from src.api.routes.events import get_event_detail
        result = get_event_detail(seq=101, user=_TEST_USER)
        assert result["prev_seq"] == 100
        assert result["next_seq"] == 102

    def test_prev_seq_excludes_other_repo(self):
        """跨仓库同 agent_type → prev_seq/next_seq 不包含其他仓库事件。"""
        _insert_event(100, _REPO_A, "vod")
        _insert_event(101, _REPO_B, "vod")
        _insert_event(102, _REPO_A, "vod")

        from src.api.routes.events import get_event_detail
        result = get_event_detail(seq=102, user=_TEST_USER)
        assert result["prev_seq"] == 100
        assert result["next_seq"] == 0

    def test_next_seq_excludes_other_repo(self):
        """跨仓库同 agent_type → next_seq 不包含其他仓库事件。"""
        _insert_event(200, _REPO_A, "vod")
        _insert_event(201, _REPO_B, "vod")
        _insert_event(202, _REPO_A, "vod")

        from src.api.routes.events import get_event_detail
        result = get_event_detail(seq=200, user=_TEST_USER)
        assert result["prev_seq"] == 0
        assert result["next_seq"] == 202

    def test_prev_seq_excludes_other_agent_type(self):
        """同仓库不同 agent_type → 不出现在 prev_seq/next_seq。"""
        _insert_event(300, _REPO_A, "vod")
        _insert_event(301, _REPO_A, "coding")
        _insert_event(302, _REPO_A, "vod")

        from src.api.routes.events import get_event_detail
        result = get_event_detail(seq=302, user=_TEST_USER)
        assert result["prev_seq"] == 300
        assert result["next_seq"] == 0

    def test_no_prev_no_next(self):
        """仅一个事件 → prev_seq=0, next_seq=0。"""
        _insert_event(400, _REPO_A, "vod")

        from src.api.routes.events import get_event_detail
        result = get_event_detail(seq=400, user=_TEST_USER)
        assert result["prev_seq"] == 0
        assert result["next_seq"] == 0

    def test_isolated_repos(self):
        """两个仓库各自独立导航，互不干扰。"""
        _insert_event(500, _REPO_A, "vod")
        _insert_event(501, _REPO_B, "vod")
        _insert_event(502, _REPO_A, "vod")
        _insert_event(503, _REPO_B, "vod")

        from src.api.routes.events import get_event_detail

        result_a = get_event_detail(seq=502, user=_TEST_USER)
        assert result_a["prev_seq"] == 500
        assert result_a["next_seq"] == 0

        result_b = get_event_detail(seq=503, user=_TEST_USER)
        assert result_b["prev_seq"] == 501
        assert result_b["next_seq"] == 0
