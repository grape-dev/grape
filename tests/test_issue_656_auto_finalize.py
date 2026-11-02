"""Issue #656: 末节点完成后 issue 自动结单（issue 级收尾迁移 + VCS Issue 关闭联动）测试。

覆盖：
- 末节点完成 + 全部节点最新事件 completed → auto_done（status=completed、stage=done、
  completed_at 落库、completed_reason=auto_finalized、VCS Issue 自动关闭）
- 存在 failed 阻断 → 回退 pending_review（保留人工 confirm done 路径），不关 VCS
- 存在 running 事件 → noop（保持 processing，不结单不关 VCS）
- issue 已处 pending_review → noop（显式人工确认场景不自动结单）
- 非末节点（中间节点）完成 → noop:not_last_node
- 幂等：已 completed 的 issue 再次调用 → noop:terminal，不重复关 VCS
"""
import os
import sys
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock
from src.api.db_setup import init_database

_REPO = "gitcode:org/issue656"
_NUM = 656


def setup_module():
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) "
            "VALUES (?,?,?,?,?,?)",
            (_REPO, "org", "issue656", "org/issue656", 1, "bot_issue656_token"))
        db.commit()


def teardown_module():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (_REPO,))
        c.execute("DELETE FROM events WHERE repo_key=?", (_REPO,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (_REPO,))
        db.commit()


def _seed(status="processing", stage="sre"):
    """建档 + 置 stage（stage 写入走 set_issue_stage，与生产同路径）。"""
    from src.api.session_store import set_issue_stage
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (_REPO,))
        c.execute(
            "INSERT OR REPLACE INTO issue (user_id, assignee, repo_key, issue_number, status, completed_at, completed_reason) "
            "VALUES (0, '', ?, ?, ?, NULL, NULL)",
            (_REPO, _NUM, status))
        db.commit()
    set_issue_stage(_REPO, f"#{_NUM}", stage, stage)


def _seed_events(statuses: dict):
    """按 agent_type 造事件（每 agent 一条最新事件）。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (_REPO,))
        for _ag, _st in statuses.items():
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, agent_type, status) VALUES (?,?,?,?,?)",
                (_REPO, f"#{_NUM}", "a2a_event", _ag, _st))
        db.commit()


def _issue_row():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, completed_at, completed_reason, stage FROM issue "
                  "WHERE repo_key=? AND issue_number=?", (_REPO, _NUM))
        return c.fetchone()


_ALL_COMPLETED = {"vod": "completed", "issue": "completed", "coding": "completed",
                  "qa": "completed", "sre": "completed"}


def test_auto_done_when_all_nodes_completed():
    from src.api.issues_center import auto_finalize_issue_on_last_node
    _seed(status="processing", stage="sre")
    _seed_events(_ALL_COMPLETED)
    with patch("src.vcs.api_client.VcsApiClient") as _cli:
        _inst = _cli.return_value
        _inst.get_issue_state.return_value = "open"
        _inst.close_issue.return_value = {"ok": True}
        _res = auto_finalize_issue_on_last_node(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res == "auto_done", _res
        _inst.close_issue.assert_called_once_with(_NUM)
    _st, _at, _reason, _stage = _issue_row()
    assert _st == "completed"
    assert _stage == "done"
    assert _reason == "auto_finalized"
    assert _at, "completed_at must be persisted (#368)"


def test_failed_node_falls_back_pending_review():
    from src.api.issues_center import auto_finalize_issue_on_last_node
    _seed(status="processing", stage="sre")
    _seed_events({**_ALL_COMPLETED, "qa": "failed"})
    with patch("src.vcs.api_client.VcsApiClient") as _cli:
        _res = auto_finalize_issue_on_last_node(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res == "pending_review_fallback", _res
        _cli.return_value.close_issue.assert_not_called()
    _st, _at, _reason, _stage = _issue_row()
    assert _st == "pending_review"
    assert _stage == "pending_review"
    assert not _at


def test_running_event_noop():
    from src.api.issues_center import auto_finalize_issue_on_last_node
    _seed(status="processing", stage="sre")
    _seed_events({**_ALL_COMPLETED, "sre": "running"})
    with patch("src.vcs.api_client.VcsApiClient") as _cli:
        _res = auto_finalize_issue_on_last_node(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res.startswith("noop:node_running"), _res
        _cli.return_value.close_issue.assert_not_called()
    assert _issue_row()[0] == "processing"


def test_pending_review_status_not_auto_finalized():
    from src.api.issues_center import auto_finalize_issue_on_last_node
    _seed(status="pending_review", stage="sre")
    _seed_events(_ALL_COMPLETED)
    with patch("src.vcs.api_client.VcsApiClient") as _cli:
        _res = auto_finalize_issue_on_last_node(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res == "noop:pending_review", _res
        _cli.return_value.close_issue.assert_not_called()
    assert _issue_row()[0] == "pending_review"


def test_middle_node_not_finalized():
    from src.api.issues_center import auto_finalize_issue_on_last_node
    _seed(status="processing", stage="qa")
    _seed_events({**_ALL_COMPLETED, "sre": "pending"})
    _res = auto_finalize_issue_on_last_node(_REPO, f"#{_NUM}", "qa", source="test")
    assert _res == "noop:not_last_node", _res
    assert _issue_row()[0] == "processing"


def test_terminal_idempotent():
    from src.api.issues_center import auto_finalize_issue_on_last_node
    _seed(status="completed", stage="done")
    _seed_events(_ALL_COMPLETED)
    with patch("src.vcs.api_client.VcsApiClient") as _cli:
        _res = auto_finalize_issue_on_last_node(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res == "noop:terminal", _res
        _cli.return_value.close_issue.assert_not_called()
    assert _issue_row()[0] == "completed"


def test_pr_pseudo_issue_id_noop():
    from src.api.issues_center import auto_finalize_issue_on_last_node
    _res = auto_finalize_issue_on_last_node(_REPO, "#pr_123_scene_x", "sre", source="test")
    assert _res == "noop:not_issue", _res
