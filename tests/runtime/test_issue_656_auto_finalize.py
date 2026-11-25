"""末节点收尾（人工闸 2026-11-17）：完成后进入待人工确认，不再自动结单。

覆盖：
- 末节点完成 + 全部节点最新事件 completed → status=stage=pending_review，
  不写 completed/completed_at，**不关 VCS Issue**（关闭只发生在人工 confirm-done）
- 存在 failed 阻断 → 回退 pending_review（保留人工 confirm done 路径）
- 存在 running 事件 → noop（保持 processing，不动状态）
- issue 已处 pending_review → noop（已待确认，不重复迁移）
- 非末节点（中间节点）完成 → noop:not_last_node
- 幂等：已 completed 的 issue 再次调用 → noop:terminal
"""
import json
import os
import sys
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock
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


def _seed_events(statuses: dict, issue_id: str = ""):
    """按 agent_type 造事件（每 agent 一条最新事件）。"""
    _issue_id = issue_id or f"#{_NUM}"
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (_REPO,))
        for _ag, _st in statuses.items():
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, agent_type, status) VALUES (?,?,?,?,?)",
                (_REPO, _issue_id, "a2a_event", _ag, _st))
        db.commit()


def _seed_dedup_leftover(issue_id: str, agent_type: str = "sre"):
    """造一条去重留痕事件（seq 大于真实事件，携带真实 agent_type 供审计）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (repo_key, issue_id, event_type, agent_type, status,"
            " title, summary, payload, result) VALUES (?,?,?,?,?,?,?,?,?)",
            (_REPO, issue_id, "issue_comment", agent_type, "skipped",
             "重复评论已去重", "源评论 id=80001, 原事件 seq=1",
             json.dumps({"_dedup": {"deduped": True,
                                    "reason": "duplicate_comment_content"}}),
             "duplicate_comment_content"))
        db.commit()


def _issue_row():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, completed_at, completed_reason, stage FROM issue "
                  "WHERE repo_key=? AND issue_number=?", (_REPO, _NUM))
        return c.fetchone()


_ALL_COMPLETED = {"vod": "completed", "issue": "completed", "coding": "completed",
                  "qa": "completed", "sre": "completed"}


def test_pending_review_when_all_nodes_completed():
    from src.api.issues_center import finalize_issue_to_pending_review
    _seed(status="processing", stage="sre")
    _seed_events(_ALL_COMPLETED)
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
        _inst = _cli.return_value
        _inst.get_issue_state.return_value = "open"
        _res = finalize_issue_to_pending_review(_REPO, f"#{_NUM}", "sre", source="test")
        _inst.close_issue.assert_not_called()   # 人工闸：自动路径不再关 VCS
    assert _res == "pending_review", _res
    _st, _at, _reason, _stage = _issue_row()
    assert _st == "pending_review", "自动路径只能推进到待确认，不能 completed"
    assert _stage == "pending_review"
    assert not _reason and not _at, "不得写 completed_reason / completed_at"


def test_failed_node_falls_back_pending_review():
    from src.api.issues_center import finalize_issue_to_pending_review
    _seed(status="processing", stage="sre")
    _seed_events({**_ALL_COMPLETED, "qa": "failed"})
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
        _res = finalize_issue_to_pending_review(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res == "pending_review_fallback", _res
        _cli.return_value.close_issue.assert_not_called()
    _st, _at, _reason, _stage = _issue_row()
    assert _st == "pending_review"
    assert _stage == "pending_review"
    assert not _at


def test_running_event_noop():
    from src.api.issues_center import finalize_issue_to_pending_review
    _seed(status="processing", stage="sre")
    _seed_events({**_ALL_COMPLETED, "sre": "running"})
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
        _res = finalize_issue_to_pending_review(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res.startswith("noop:node_running"), _res
        _cli.return_value.close_issue.assert_not_called()
    assert _issue_row()[0] == "processing"


def test_pending_review_status_not_auto_finalized():
    from src.api.issues_center import finalize_issue_to_pending_review
    # Issue #1599：set_issue_stage 推进阶段时会清理 pending_review 漂移，
    # 此用例直接构造「stage=sre + status=pending_review」的显式待审核行，验证自动结单守卫。
    _seed(status="processing", stage="sre")
    with db_lock:
        db.cursor().execute("UPDATE issue SET status='pending_review' "
                            "WHERE repo_key=? AND issue_number=?", (_REPO, _NUM))
        db.commit()
    _seed_events(_ALL_COMPLETED)
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
        _res = finalize_issue_to_pending_review(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res == "noop:pending_review", _res
        _cli.return_value.close_issue.assert_not_called()
    assert _issue_row()[0] == "pending_review"


def test_middle_node_not_finalized():
    from src.api.issues_center import finalize_issue_to_pending_review
    _seed(status="processing", stage="qa")
    _seed_events({**_ALL_COMPLETED, "sre": "pending"})
    _res = finalize_issue_to_pending_review(_REPO, f"#{_NUM}", "qa", source="test")
    assert _res == "noop:not_last_node", _res
    assert _issue_row()[0] == "processing"


def test_terminal_idempotent():
    from src.api.issues_center import finalize_issue_to_pending_review
    _seed(status="completed", stage="done")
    _seed_events(_ALL_COMPLETED)
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
        _res = finalize_issue_to_pending_review(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res == "noop:terminal", _res
        _cli.return_value.close_issue.assert_not_called()
    assert _issue_row()[0] == "completed"


def test_pr_pseudo_issue_id_noop():
    from src.api.issues_center import finalize_issue_to_pending_review
    _res = finalize_issue_to_pending_review(_REPO, "#pr_123_scene_x", "sre", source="test")
    assert _res == "noop:not_issue", _res


# ---- Review feedback fixes (PR #828 re-review) ----

_GH_MIRROR = "github:org/issue656"


def _seed_github_mirror():
    """managed_repos 增加同 full_name 的 GitHub 镜像记录（含 bot_github_token）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_github_token) "
            "VALUES (?,?,?,?,?,?)",
            (_GH_MIRROR, "org", "issue656", "org/issue656", 2, "bot_gh_issue656_token"))
        db.commit()


def test_no_vcs_close_on_last_node_with_github_mirror():
    """人工闸：末节点收尾不再关 VCS Issue（GitCode 与 GitHub 镜像都不动）。"""
    from src.api.issues_center import finalize_issue_to_pending_review
    _seed_github_mirror()
    try:
        _seed(status="processing", stage="sre")
        _seed_events(_ALL_COMPLETED)
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
            _inst = _cli.return_value
            _inst.get_issue_state.return_value = "open"
            _inst.close_issue.return_value = {"ok": True}
            _res = finalize_issue_to_pending_review(_REPO, f"#{_NUM}", "sre", source="test")
            assert _res == "pending_review", _res
            _inst.close_issue.assert_not_called()
        assert _issue_row()[3] == "pending_review"
    finally:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (_GH_MIRROR,))
            db.commit()


def test_no_vcs_close_single_repo():
    """无镜像时也不关 GitCode 本仓（关闭动作只归人工 confirm-done）。"""
    from src.api.issues_center import finalize_issue_to_pending_review
    _seed(status="processing", stage="sre")
    _seed_events(_ALL_COMPLETED)
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
        _inst = _cli.return_value
        _inst.get_issue_state.return_value = "open"
        _inst.close_issue.return_value = {"ok": True}
        _res = finalize_issue_to_pending_review(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res == "pending_review", _res
        _inst.close_issue.assert_not_called()


def test_misconfigured_next_not_treated_as_last_node():
    """Suggestion: next 指向不存在节点（配置笔误）→ 判非末节点，不触发自动结单。"""
    from src.api.issues_center import finalize_issue_to_pending_review
    _seed(status="processing", stage="sre")
    _seed_events(_ALL_COMPLETED)
    _bad_cfg = {"nodes": [
        {"id": "issue", "next": "vod"}, {"id": "vod", "next": "coding"},
        {"id": "coding", "next": "qa"}, {"id": "qa", "next": "sre"},
        {"id": "sre", "next": "dn"},   # 笔误：指向不存在节点
    ]}
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=_bad_cfg):
        _res = finalize_issue_to_pending_review(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res == "noop:not_last_node", _res
    assert _issue_row()[0] == "processing"


def test_terminal_compensates_stage_alignment():
    """Suggestion: status=completed 但 stage 未对齐 → 后续事件读 status 反向对齐 stage=done。"""
    from src.api.issues_center import finalize_issue_to_pending_review
    _seed(status="completed", stage="sre")   # 模拟 set_issue_stage 历史失败残留
    _seed_events(_ALL_COMPLETED)
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
        _res = finalize_issue_to_pending_review(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res == "noop:terminal", _res
        _cli.return_value.close_issue.assert_not_called()
    assert _issue_row()[3] == "done", "stage must be compensated to done"


# ---- Issue #3444 re-review: dedup leftover must not block auto-finalize ----

import pytest  # noqa: E402  (kept next to the regression cases it serves)


@pytest.mark.parametrize("real_form,leftover_form", [
    (f"#{_NUM}", f"{_NUM}"),
    (f"{_NUM}", f"#{_NUM}"),
])
def test_dedup_leftover_after_all_completed_still_finalizes(real_form, leftover_form):
    """末节点（sre）真实 completed + 后到 skipped 去重留痕 → 仍推进 pending_review。

    Issue #3444 复审阻塞项：留痕 seq 更大且 agent_type=sre，若按 MAX(seq) 选为
    「最新事件」会把 sre 覆盖成 skipped → noop:node_pending:sre，末节点完成也无法
    进入待人工确认。兼容 events.issue_id 带/不带 `#` 两种历史形态。
    """
    from src.api.issues_center import finalize_issue_to_pending_review
    _seed(status="processing", stage="sre")
    _seed_events(_ALL_COMPLETED, issue_id=real_form)
    _seed_dedup_leftover(leftover_form, agent_type="sre")
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
        _res = finalize_issue_to_pending_review(_REPO, f"#{_NUM}", "sre", source="test")
        _cli.return_value.close_issue.assert_not_called()
    assert _res == "pending_review", _res
    _st, _at, _reason, _stage = _issue_row()
    assert _st == "pending_review", "自动路径只能推进到待确认，不能停在 processing"
    assert _stage == "pending_review"
    assert not _reason and not _at


def test_dedup_leftover_alone_does_not_count_as_completed():
    """对照：节点只有 skipped 留痕（无真实完成事件）时仍视为未完成 → noop。"""
    from src.api.issues_center import finalize_issue_to_pending_review
    _seed(status="processing", stage="sre")
    _events = {k: v for k, v in _ALL_COMPLETED.items() if k != "sre"}
    _seed_events(_events)
    _seed_dedup_leftover(f"#{_NUM}", agent_type="sre")
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
        _res = finalize_issue_to_pending_review(_REPO, f"#{_NUM}", "sre", source="test")
        _cli.return_value.close_issue.assert_not_called()
    assert _res == "noop:node_pending:sre", _res
    assert _issue_row()[0] == "processing"
