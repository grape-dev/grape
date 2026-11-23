"""事件详情 prev_seq/next_seq 导航口径测试。

历史：#224 的口径是「同 repo_key + 同 agent_type」。真实仓库里这等于跨需求乱跳——
同一 agent 在多个需求之间来回串，用户在 EVENT 详情页点左右箭头根本不知道会去哪。

2026-11-20 主人口径（本文件的新契约）：
- 事件有 issue_id 时，前后导航 = 同 repo_key + 同 issue_id 内的事件，**跨 agent_type 一起串**
  （一个需求的事件本来就是 vod→issue→coding→qa→sre 一条链）；
- issue_id 为空 / '#0'（非需求事件，如手工、管理类事件）时回退 #224 的旧口径，避免串成无意义链；
- issue_id 既有 '#123' 也有历史裸 '123' 形态，两种都算同一需求；
- 跨仓库永不相串（#224 的 repo_key 过滤保留）。
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.kernel.session_manager import db, db_lock

_REPO_A = "gitcode:org/repo-a"
_REPO_B = "gitcode:org/repo-b"
_TEST_USER = {"id": 1, "username": "test", "auth_type": "session_token"}

_CLEAN = (
    "DELETE FROM events WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' "
    "OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' "
    "OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'"
)


@pytest.fixture(autouse=True)
def _clean_events():
    with db_lock:
        c = db.cursor()
        c.execute(_CLEAN)
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute(_CLEAN)
        db.commit()


def _insert_event(seq, repo_key, agent_type, issue_id="#1", status="completed"):
    with db_lock:
        c = db.cursor()
        # 测试库与手工数据共用一份 sessions.db：seq 是全局主键，先占位清理，免得撞既有行
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, agent_type, title, issue_id, "
            "payload, status, result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (seq, repo_key, "issue_opened", agent_type, "test event", issue_id, "{}", status, "done"),
        )
        db.commit()


def _detail(seq):
    from src.domains.runtime.routes.events import get_event_detail
    return get_event_detail(seq=seq, user=_TEST_USER)


class TestSameIssueNavigation:
    """主口径：箭头只在同需求内前后跳。"""

    def test_cross_agent_type_within_same_issue_is_navigable(self):
        """同需求、不同 agent_type（vod→coding→qa）必须能串起来——这是需求的主诉求。"""
        _insert_event(300, _REPO_A, "vod", "#7")
        _insert_event(301, _REPO_A, "coding", "#7")
        _insert_event(302, _REPO_A, "qa", "#7")

        mid = _detail(301)
        assert mid["prev_seq"] == 300 and mid["prev_agent"] == "vod"
        assert mid["next_seq"] == 302 and mid["next_agent"] == "qa"
        assert mid["nav_scope"] == "issue" and mid["nav_issue_id"] == "#7"

    def test_other_issue_same_repo_is_excluded(self):
        """同仓库不同需求的事件不得出现在 arrows 里（旧口径的真实故障面）。"""
        _insert_event(400, _REPO_A, "coding", "#10")
        _insert_event(401, _REPO_A, "coding", "#11")
        _insert_event(402, _REPO_A, "coding", "#10")

        r = _detail(402)
        assert r["prev_seq"] == 400, "同 #10 的上一条"
        assert r["next_seq"] == 0, "#11 不属于同一需求，不得成为下一条"

    def test_other_issue_does_not_leak_into_next(self):
        _insert_event(500, _REPO_A, "coding", "#10")
        _insert_event(501, _REPO_A, "qa", "#11")

        r = _detail(500)
        assert r["next_seq"] == 0

    def test_bare_and_hash_issue_id_are_same_issue(self):
        """历史行有的存 '#9'、有的存裸 '9'，必须视为同一需求。"""
        _insert_event(600, _REPO_A, "vod", "#9")
        _insert_event(601, _REPO_A, "coding", "9")
        _insert_event(602, _REPO_A, "qa", "#9")

        r = _detail(601)
        assert r["prev_seq"] == 600 and r["next_seq"] == 602

    def test_cross_repo_never_links(self):
        _insert_event(700, _REPO_A, "vod", "#3")
        _insert_event(701, _REPO_B, "coding", "#3")
        _insert_event(702, _REPO_A, "qa", "#3")

        r = _detail(702)
        assert r["prev_seq"] == 700, "#3@repo-a 的上一条是 700，跨仓 701 不算"
        assert r["next_seq"] == 0

    def test_single_event_has_no_neighbours(self):
        _insert_event(800, _REPO_A, "vod", "#4")
        r = _detail(800)
        assert r["prev_seq"] == 0 and r["next_seq"] == 0
        assert r["nav_scope"] == "issue"


class TestNonIssueFallback:
    """非需求事件（issue_id 为空 / '#0'）回退 #224 口径：同 repo + 同 agent_type。"""

    @pytest.mark.parametrize("empty", ["", "#0", "0"])
    def test_empty_issue_id_falls_back_to_agent_scope(self, empty):
        _insert_event(900, _REPO_A, "coding", empty)
        _insert_event(901, _REPO_A, "qa", empty)      # 同 repo 不同 agent，不该串
        _insert_event(902, _REPO_A, "coding", empty)
        _insert_event(903, _REPO_B, "coding", empty)  # 跨仓，不该串

        r = _detail(902)
        assert r["nav_scope"] == "agent", "非需求事件必须回退旧口径"
        assert r["prev_seq"] == 900
        assert r["next_seq"] == 0, "903 在另一个仓库"
        assert r["nav_issue_id"] == ""