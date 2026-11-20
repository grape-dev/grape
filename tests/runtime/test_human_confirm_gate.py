"""人工闸（2026-11-17）：需求的「确认完成」只能由人做。

覆盖四条契约：
1. 末节点全绿 → 只推进到 pending_review，不写 completed、不关 VCS Issue；
2. agent 调 review-pass target=done → 一律拒绝（内部码 + 路由 400）；
3. 末节点 taget=processing → 归一 pending_review（不是 done）；
4. close_issue_tasks 只认「人推进的 done」，agent/pipeline 推进的 done 降级为待确认。
"""
import os
import sys
from unittest.mock import patch

import pytest
from fastapi import HTTPException

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock          # noqa: E402
from src.api.db_setup import init_database                  # noqa: E402

_REPO = "gitcode:org/humangate"
_NUM = 9001


def setup_module():
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos "
            "(repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) "
            "VALUES (?,?,?,?,?,?)",
            (_REPO, "org", "humangate", "org/humangate", 1, "bot_humangate_token"))
        db.commit()


def teardown_module():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (_REPO,))
        c.execute("DELETE FROM events WHERE repo_key=?", (_REPO,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (_REPO,))
        db.commit()


def _seed(status="processing", stage="sre"):
    from src.api.session_store import set_issue_stage
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (_REPO,))
        c.execute(
            "INSERT OR REPLACE INTO issue (user_id, assignee, repo_key, issue_number, status, "
            "completed_at, completed_reason) VALUES (0, '', ?, ?, ?, NULL, NULL)",
            (_REPO, _NUM, status))
        db.commit()
    set_issue_stage(_REPO, f"#{_NUM}", stage, stage)


def _seed_events(statuses: dict):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (_REPO,))
        for _ag, _st in statuses.items():
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, agent_type, status) "
                "VALUES (?,?,?,?,?)", (_REPO, f"#{_NUM}", "a2a_event", _ag, _st))
        db.commit()


def _row():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, completed_at, completed_reason, stage FROM issue "
                  "WHERE repo_key=? AND issue_number=?", (_REPO, _NUM))
        return c.fetchone()


def _stage_owner():
    from src.api.session_store import get_issue_stage
    return (get_issue_stage(_REPO, f"#{_NUM}") or {}).get("owner_agent") or ""


_ALL_COMPLETED = {"vod": "completed", "issue": "completed", "coding": "completed",
                  "qa": "completed", "sre": "completed"}

_LAST_CFG = {"nodes": [
    {"id": "vod", "next": "issue"}, {"id": "issue", "next": "coding"},
    {"id": "coding", "next": "qa"}, {"id": "qa", "next": "sre"},
    {"id": "sre", "next": "done"},
]}


# ───────── 1. 末节点收尾只推进到待确认 ─────────
def test_last_node_pending_review_without_vcs_close():
    from src.api.issues_center import finalize_issue_to_pending_review
    _seed()
    _seed_events(_ALL_COMPLETED)
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=_LAST_CFG), \
            patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
        _res = finalize_issue_to_pending_review(_REPO, f"#{_NUM}", "sre", source="test")
        _cli.return_value.close_issue.assert_not_called()
    assert _res == "pending_review"
    _st, _at, _reason, _stage = _row()
    assert _st == "pending_review" and _stage == "pending_review"
    assert not _at and not _reason


# ───────── 2. agent 不能终态 ─────────
def test_review_pass_done_forbidden_internal():
    from src.api.issues_center import _review_pass
    _seed(status="pending_review", stage="sre")
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=_LAST_CFG):
        _res = _review_pass(_REPO, _NUM, target="done", evidence="agent wants done")
    assert _res == "noop:agent_finalize_forbidden"
    assert _row()[0] == "pending_review"


def test_route_review_pass_done_returns_400(monkeypatch):
    from src.domains.runtime.routes import issues as issues_routes
    monkeypatch.setattr(issues_routes, "_session_devops_issue",
                        lambda issue_id, user: {"id": issue_id, "repo_key": _REPO,
                                                "issue_number": _NUM, "status": "pending_review"})

    class _Body:
        target = "done"
        evidence = ""

    import asyncio
    with pytest.raises(HTTPException) as _exc:
        asyncio.run(issues_routes.issue_review_pass(1, _Body(), user={"id": 1}))
    assert _exc.value.status_code == 400
    assert "agent_finalize_forbidden" in str(_exc.value.detail)


# ───────── 3. 末节点 processing → pending_review（不是 done） ─────────
def test_review_pass_last_node_processing_normalized_to_pending_review():
    from src.api.issues_center import _review_pass
    _seed(status="processing", stage="sre")
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=_LAST_CFG):
        _res = _review_pass(_REPO, _NUM, target="processing", evidence="pipeline finished")
    assert _res == "pending_review", _res
    _st, _at, _reason, _stage = _row()
    assert _st == "pending_review" and _stage == "pending_review"


# ───────── 4. close_issue_tasks：只认人推进的 done ─────────
def test_close_issue_tasks_completes_human_done():
    from src.api.session_store import set_issue_stage
    from src.api.issues_center import close_issue_tasks
    _seed(status="pending_review", stage="pending_review")
    set_issue_stage(_REPO, f"#{_NUM}", "done", "user")   # 人在界面确认
    close_issue_tasks(_REPO, _NUM)
    assert _row()[0] == "completed", "人推进的 done 应当结单"


def test_close_issue_tasks_downgrades_agent_done():
    from src.api.session_store import set_issue_stage
    from src.api.issues_center import close_issue_tasks
    _seed(status="pending_review", stage="pending_review")
    set_issue_stage(_REPO, f"#{_NUM}", "done", "agent")  # 自动路径残留
    close_issue_tasks(_REPO, _NUM)
    _st, _at, _reason, _stage = _row()
    assert _st == "pending_review", "非人工 done 不得结单"
    assert _stage == "pending_review", "应降级回待确认"
    assert _stage_owner() == "pipeline"