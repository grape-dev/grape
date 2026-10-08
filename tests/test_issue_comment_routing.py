"""issue_comment 按 stage owner 路由测试（issue-comment-stage-owner-routing-design.md）。

- coding/qa/sre 阶段评论 → 同名 owner agent（不再绕 issue）
- pending_review → owner_agent（如 QA 设审核则由 QA 收口）
- done/cancelled → 不分发
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api.db_setup import init_database
from src.session_manager import db, db_lock

init_database()

REPO = "gitcode:org/cmt"
NUM = 9


def _base_issue():
    # 先删再经 set_issue_stage 建档（含最小 active 行）
    with db_lock:
        db.cursor().execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (REPO, NUM))
        db.commit()
    from src.api.session_store import set_issue_stage
    set_issue_stage(REPO, f"#{NUM}", "vod", "vod")


def _route(stage="vod", owner="", cancelled=False):
    _base_issue()
    if stage:
        from src.api.session_store import set_issue_stage
        set_issue_stage(REPO, f"#{NUM}", stage, owner or (stage if stage in ("vod", "issue", "coding", "qa", "sre") else ""))
    if cancelled:
        with db_lock:
            db.cursor().execute(
                "UPDATE issue SET status='cancelled' WHERE repo_key=? AND issue_number=?",
                (REPO, NUM))
            db.commit()
    from src.api.event_pipeline import _resolve_issue_stage_agent
    return _resolve_issue_stage_agent(REPO, "issue_comment",
                                      {"issue": {"number": NUM}}, "issue")


def test_route_vod_issue_self():
    assert _route(stage="vod") == "vod"
    assert _route(stage="issue", owner="issue") == "issue"


def test_route_coding_qa_sre_to_self():
    assert _route(stage="coding", owner="coding") == "coding"
    assert _route(stage="qa", owner="qa") == "qa"
    assert _route(stage="sre", owner="sre") == "sre"


def test_route_pending_review_to_owner_agent():
    assert _route(stage="pending_review", owner="qa") == "qa"
    assert _route(stage="pending_review", owner="coding") == "coding"


def test_route_pending_review_fallback_when_owner_unknown():
    # owner 缺失 → 回落表内 pending_review 默认（issue）
    assert _route(stage="pending_review", owner="") == "issue"


def test_route_done_not_dispatched():
    assert _route(stage="done", owner="done") == ""


def test_route_cancelled_not_dispatched():
    assert _route(stage="coding", owner="coding", cancelled=True) == ""