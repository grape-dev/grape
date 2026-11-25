"""Issue #4063：审核点（pending_review）状态机稳定驻留与读数一致性回归。

覆盖：
- AC1 审核点在位连续多次读 task_status 稳定为 pending_review；
- AC2 发布 issue_comment（含 Agent 自身评论）后仍读 pending_review；
- AC3 重复置位 changed:false、内容变更 changed:true；
- AC4/AC5 detail 只读 review_point.status/references 与 status get 同源；
- 置位→评论→读回 / 重复置位 回归。
"""
import json
import sqlite3
import sys
import threading
from unittest.mock import patch

import pytest

_PATCH_DB_MODULES = (
    "src.kernel.session_manager", "src.api.event_store", "src.api.event_pipeline",
    "src.domains.mgmt.user.db", "src.api.session_store", "src.api.issues_center",
    "src.api.db_setup", "src.domains.runtime.routes.events", "src.api.token_resolver",
    "src.domains.runtime.routes.issues",
)

RK = "gitcode:test/i4063"
NUM = 4063
BOT = "grape_bot"


@pytest.fixture(autouse=True)
def _mock_db(monkeypatch):
    import src.kernel.session_manager as _sm
    import src.api.db_setup as _dbs
    _conn = sqlite3.connect(":memory:", check_same_thread=False)
    _lock = threading.RLock()
    for _name in _PATCH_DB_MODULES:
        _mod = sys.modules.get(_name)
        if _mod is None:
            continue
        monkeypatch.setattr(_mod, "db", _conn, raising=False)
        monkeypatch.setattr(_mod, "db_lock", _lock, raising=False)
    # Issue #4063（QA S1）：db_setup 只建 runtime 域表，mgmt 域的 managed_repos 由 mgmt
    # 启动链路建。ensure_issue_task（event_pipeline 3.2）会读 managed_repos.org_id，缺表
    # 会被 3.2 的 except 吞成假绿。这里先跑 mgmt init_db 补建托管仓库表，再跑 db_setup，
    # 使 events 等核心表的 org_id 迁移与真实启动链路一致。
    import src.domains.mgmt.user.db as _mg_db
    _mg_db.init_db()
    _dbs.init_database()
    yield
    try:
        from src.domains.runtime.pipeline.event_pipeline import wait_pi_refresh_drain
        wait_pi_refresh_drain(timeout=35.0)
    except Exception:
        pass
    _conn.close()


from src.api import event_pipeline as ep  # noqa: E402
from src.api import issues_center as ic  # noqa: E402
from src.domains.runtime.routes import issues as issues_mod  # noqa: E402


class _DbProxy:
    def __getattr__(self, attr):
        import src.kernel.session_manager as _sm
        return getattr(_sm.db, attr)


class _DbLockProxy:
    def __enter__(self):
        import src.kernel.session_manager as _sm
        self._real = _sm.db_lock
        return self._real.__enter__()

    def __exit__(self, *args):
        return self._real.__exit__(*args)


db = _DbProxy()
db_lock = _DbLockProxy()


def _seed(status="processing", stage="qa", owner_agent="qa", num=NUM):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (RK, num))
        c.execute("DELETE FROM events WHERE repo_key=? AND issue_id=?", (RK, f"#{num}"))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (1, "realuser", RK, num, "审核点驻留需求", "desc", status))
        db.commit()
    if stage:
        from src.api.session_store import set_issue_stage
        set_issue_stage(RK, f"#{num}", stage, owner_agent)


def _row(num=NUM):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, stage, review_points, review_refs FROM issue "
            "WHERE repo_key=? AND issue_number=?", (RK, num)).fetchone()


def _comment_payload(username="realuser", note="补充说明", num=NUM, cid=94063):
    return {
        "object_kind": "note",
        "project": {"path_with_namespace": "test/i4063"},
        "object_attributes": {"noteable_type": "Issue", "noteable_iid": num,
                              "note": note, "id": cid},
        "user": {"username": username},
    }


def _run_pipeline(payload, replay=False):
    return ep.run_pipeline(json.dumps(payload).encode(), {"X-GitCode-Event": "Note Hook"},
                           account="test/i4063", repo_key_override="test/i4063", replay=replay)


# ---------- AC1：在位状态稳定 ----------

def test_pending_review_reads_stable_five_times():
    _seed(status="processing")
    assert ic._set_issue_pending_review(RK, NUM, "审核点内容", "PR#11") is True
    for _ in range(5):
        assert ic._read_issue_status(RK, NUM) == "pending_review"
        assert _row()[0] == "pending_review"


# ---------- AC2：评论（含 Agent 自身评论）不回落 ----------

def test_human_issue_comment_keeps_pending_review(monkeypatch):
    _seed(status="pending_review", stage="pending_review", owner_agent="qa")
    # 显式断言 event_pipeline 3.2（ensure_issue_task）真实执行，避免管理表缺失时
    # 被 3.2 的 except 吞掉造成假绿（QA S1）。
    from unittest.mock import MagicMock
    _spy = MagicMock(wraps=ic.ensure_issue_task)
    monkeypatch.setattr(ic, "ensure_issue_task", _spy)
    _run_pipeline(_comment_payload(username="realuser"), replay=False)
    assert _spy.called, "event_pipeline 3.2 ensure_issue_task 未被调用（管理表缺失假绿）"
    st = _row()
    assert st[0] == "pending_review", st
    assert st[1] == "pending_review", st


def test_ensure_issue_task_does_not_rearm_pending_review():
    """Issue #4063（QA F1）：ensure_issue_task 复用分支不得把 pending_review 回落 active。"""
    _seed(status="pending_review", stage="pending_review", owner_agent="qa")
    ic.ensure_issue_task(RK, NUM, title="comment echo", assignee="realuser",
                         event_type="issue_comment")
    st = _row()
    assert st[0] == "pending_review", st
    assert st[1] == "pending_review", st


def test_agent_self_comment_keeps_pending_review(monkeypatch):
    _seed(status="pending_review", stage="pending_review", owner_agent="qa")
    import src.domains.mgmt.public as mgmt_public
    monkeypatch.setattr(mgmt_public, "get_all_bot_usernames", lambda: [BOT])
    _run_pipeline(_comment_payload(username=BOT, note="[Coding Agent] 已完成"), replay=False)
    assert _row()[0] == "pending_review"


def test_awaiting_input_still_resumes_on_comment():
    """对照：awaiting_input 不受影响，用户回复仍恢复 processing。"""
    _seed(status="awaiting_input", stage="qa", owner_agent="qa")
    _run_pipeline(_comment_payload(username="realuser", note="回答澄清问题"), replay=False)
    assert _row()[0] == "processing"


# ---------- AC3：置位幂等 / 内容变更 ----------

def test_repeat_pending_review_same_content_is_noop():
    _seed(status="processing")
    assert ic._set_issue_pending_review(RK, NUM, "审核点A", "refA") is True
    assert ic._set_issue_pending_review(RK, NUM, "审核点A", "refA") is False
    st = _row()
    assert st[0] == "pending_review" and st[2] == "审核点A" and st[3] == "refA"


def test_pending_review_content_change_changed_true():
    _seed(status="processing")
    assert ic._set_issue_pending_review(RK, NUM, "审核点A", "refA") is True
    assert ic._set_issue_pending_review(RK, NUM, "审核点B", "refB") is True
    st = _row()
    assert st[0] == "pending_review" and st[2] == "审核点B" and st[3] == "refB"


def test_legacy_pending_review_without_snapshot_stays_idempotent():
    """存量在位行无内容快照 → 无法证明变更，保持 #725 幂等语义。"""
    _seed(status="pending_review", stage="pending_review", owner_agent="qa")
    assert ic._set_issue_pending_review(RK, NUM, "任意内容", "") is False
    assert _row()[0] == "pending_review"


# ---------- AC4/AC5：只读投影与权威源一致 ----------

def test_detail_review_point_matches_status_authority():
    _seed(status="pending_review", stage="pending_review", owner_agent="qa",
          num=4063)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE issue SET review_points=?, review_refs=? "
                  "WHERE repo_key=? AND issue_number=?", ("详情审核点", "PR#63", RK, NUM))
        db.commit()
    rp = issues_mod._review_point_readonly([RK], str(NUM))
    assert rp == {"status": "pending_review", "references": "PR#63"}
    status_map = issues_mod._task_status_map([RK])
    assert status_map[str(NUM)] == "pending_review"


def test_detail_review_point_empty_when_not_in_place():
    _seed(status="processing")
    assert issues_mod._review_point_readonly([RK], str(NUM)) == {"status": "", "references": ""}


def test_detail_endpoint_exposes_review_point():
    """detail 响应含只读 review_point.*，且与 task_status 一致。"""
    _seed(status="pending_review", stage="pending_review", owner_agent="qa")
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE issue SET review_points=?, review_refs=? "
                  "WHERE repo_key=? AND issue_number=?", ("端到端审核点", "PR#4063", RK, NUM))
        db.commit()
    out = issues_mod.get_issue_detail("test", "i4063", str(NUM), {"id": 1})
    assert out["task_status"] == "pending_review"
    assert out["review_point"]["status"] == "pending_review"
    assert out["review_point"]["references"] == "PR#4063"


def test_detail_id_and_issue_number_align_status_get():
    """Issue #4063（QA F2 / #3981）：detail 的 id/issue_number 与 status get 同口径。"""
    _seed(status="pending_review", stage="pending_review", owner_agent="qa")
    with db_lock:
        row_id = db.cursor().execute(
            "SELECT id FROM issue WHERE repo_key=? AND issue_number=?", (RK, NUM)).fetchone()[0]
    detail = issues_mod.get_issue_detail("test", "i4063", str(NUM), {"id": 1})
    status = issues_mod.get_issue_status(row_id, {"id": 1})
    assert detail["id"] == row_id
    assert detail["issue_number"] == NUM
    assert status["id"] == detail["id"]
    assert status["issue_number"] == detail["issue_number"]
