"""Issue #2381: redispatch 重放后 review-pass 审核点保留与末节点判据统一。

覆盖：
- 末节点公共判据 `_next_is_last_node`：done / 空串 / 未知 / 指向不存在节点；
- `_is_last_stage_node` 与 `_pipeline_last_node_check` 对 next=="" 行为一致；
- 末节点 next=="" 时 review-pass(target=done) 从 processing 可正常收尾（不再 noop）；
- `_resume_issue_waiting(preserve_review=True)` 保留 pending_review，仍恢复 awaiting_input；
- noop 原因码区分「审核点已因 redispatch/回复失效」；
- run_pipeline replay=True 不消费 pending_review，replay=False 仍保持既有 resume 行为。
"""
import json
import sys
import threading
import sqlite3
from unittest.mock import patch

import pytest

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

_PATCH_DB_MODULES = (
    "src.kernel.session_manager", "src.api.event_store", "src.api.event_pipeline",
    "src.domains.mgmt.user.db", "src.api.session_store", "src.api.issues_center",
    "src.api.db_setup", "src.domains.runtime.routes.events", "src.api.token_resolver",
)

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
    _dbs.init_database()
    yield
    try:
        from src.domains.runtime.pipeline.event_pipeline import wait_pi_refresh_drain
        wait_pi_refresh_drain(timeout=35.0)
    except Exception:
        pass
    _conn.close()

from src.api import event_pipeline as ep
from src.api import issues_center as ic
from src.api.session_store import set_issue_stage

RK = "gitcode:test/i2381"
NUM = 2381

_LAST_CFG = {"nodes": [
    {"id": "vod", "next": "issue"},
    {"id": "issue", "next": "coding"},
    {"id": "coding", "next": "qa"},
    {"id": "qa", "next": "sre"},
    {"id": "sre", "next": ""},
]}

_MISCONFIG_CFG = {"nodes": [
    {"id": "qa", "next": "sre"},
    {"id": "sre", "next": "dn"},
]}


def _seed(status="processing", stage="sre", owner_agent="sre", num=NUM):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (RK, num))
        c.execute("DELETE FROM events WHERE repo_key=? AND issue_id=?", (RK, f"#{num}"))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (1, "realuser", RK, num, "需求标题", "desc", status))
        db.commit()
    set_issue_stage(RK, f"#{num}", stage, owner_agent)


def _status(num=NUM):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, stage FROM issue WHERE repo_key=? AND issue_number=?",
            (RK, num)).fetchone()


def _seed_event(agent, status, seq=7001, num=NUM):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, issue_id, event_type, agent_type, status) "
            "VALUES (?,?,?,?,?,?)",
            (seq, RK, f"#{num}", "a2a_event", agent, status))
        db.commit()


def _comment_payload(username="realuser", note="回答问题", num=NUM, cid=92381):
    return {
        "object_kind": "note",
        "project": {"path_with_namespace": "test/i2381"},
        "object_attributes": {"noteable_type": "Issue", "noteable_iid": num, "note": note, "id": cid},
        "user": {"username": username},
    }


def _run_pipeline(payload, replay=False):
    return ep.run_pipeline(json.dumps(payload).encode(), {"X-GitCode-Event": "Note Hook"},
                           account="test/i2381", repo_key_override="test/i2381", replay=replay)


# ---------- 末节点判据 ----------

def test_next_is_last_node_predicate():
    assert ic._next_is_last_node("done") is True
    assert ic._next_is_last_node("") is True
    assert ic._next_is_last_node(None) is True
    assert ic._next_is_last_node("  done  ") is True
    assert ic._next_is_last_node("qa") is False
    assert ic._next_is_last_node("dn") is False


def test_last_node_predicate_next_empty_both_helpers():
    _seed(status="processing", stage="sre")
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=_LAST_CFG):
        assert ic._is_last_stage_node(RK, NUM) is True
        assert ic._pipeline_last_node_check(RK, NUM, "sre") is True


def test_last_node_predicate_next_done_both_helpers():
    _seed(status="processing", stage="sre")
    _done_cfg = {"nodes": [{"id": "qa", "next": "sre"}, {"id": "sre", "next": "done"}]}
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=_done_cfg):
        assert ic._is_last_stage_node(RK, NUM) is True
        assert ic._pipeline_last_node_check(RK, NUM, "sre") is True


def test_last_node_predicate_misconfigured_next_not_last():
    _seed(status="processing", stage="sre")
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=_MISCONFIG_CFG):
        assert ic._is_last_stage_node(RK, NUM) is False
        assert ic._pipeline_last_node_check(RK, NUM, "sre") is False


def test_last_node_predicate_unknown_stage_is_false():
    _seed(status="processing", stage="")
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=_LAST_CFG):
        assert ic._is_last_stage_node(RK, NUM) is False


# ---------- review-pass 末节点 next="" ----------

def test_review_pass_done_on_last_node_next_empty():
    """此前 next=="" 不算末节点 → 非末节点通道只认 pending_review → 恒 noop（#2381 主回归）。"""
    _seed(status="processing", stage="sre")
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=_LAST_CFG):
        res = ic._review_pass(RK, NUM, target="done", evidence="用户确认合入")
    assert res == "passed_done", res
    st = _status()
    assert st[0] == "completed" and st[1] == "done"


def test_review_pass_done_middle_node_still_noop():
    """非末节点防护不变：中间节点 active/processing + target=done 不误终态。"""
    _seed(status="processing", stage="qa")
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=_LAST_CFG):
        res = ic._review_pass(RK, NUM, target="done")
    assert res.startswith("noop:"), res
    assert _status()[0] == "processing"


# ---------- resume 保留审核点 ----------

def test_resume_preserve_review_keeps_pending_review():
    _seed(status="pending_review", stage="pending_review", owner_agent="qa")
    assert ic._resume_issue_waiting(RK, NUM, preserve_review=True) is False
    st = _status()
    assert st[0] == "pending_review" and st[1] == "pending_review"


def test_resume_preserve_review_still_resumes_awaiting_input():
    _seed(status="awaiting_input", stage="qa", owner_agent="qa")
    assert ic._resume_issue_waiting(RK, NUM, preserve_review=True) is True
    assert _status()[0] == "processing"


def test_resume_without_preserve_flips_pending_review():
    _seed(status="pending_review", stage="pending_review", owner_agent="qa")
    assert ic._resume_issue_waiting(RK, NUM) is True
    st = _status()
    assert st[0] == "processing"
    assert st[1] == "qa"  # stage 回落 owner_agent


def test_pipeline_replay_preserves_pending_review():
    _seed(status="pending_review", stage="pending_review", owner_agent="qa")
    _run_pipeline(_comment_payload(), replay=True)
    st = _status()
    assert st[0] == "pending_review", st
    assert st[1] == "pending_review", st


def test_pipeline_non_replay_resumes_pending_review():
    _seed(status="pending_review", stage="pending_review", owner_agent="qa")
    _run_pipeline(_comment_payload(), replay=False)
    assert _status()[0] == "processing"


# ---------- noop 原因码 ----------

def test_noop_reason_checkpoint_invalidated():
    _seed(status="processing", stage="qa", owner_agent="qa")
    _seed_event("sre", "completed")
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=_LAST_CFG):
        assert ic._review_noop_reason(RK, NUM, "completed") == "noop:review_checkpoint_invalidated"


def test_noop_reason_plain_not_pending_review_without_checkpoint_history():
    _seed(status="processing", stage="qa", owner_agent="qa")
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=_LAST_CFG):
        assert ic._review_noop_reason(RK, NUM, "completed") == "noop:not_pending_review"