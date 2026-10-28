"""issue-comment-resume-outbound-ledger-fix-design.md 验收测试（TDD 先红后绿）。

两层根因回归护栏：
- RC-A：等待态（awaiting_input/pending_review）用户真实回复经 grape VCS 代理通道发出 → 台账误记账 →
  回流被 outbound_ledger_hit 误拦 → 永不恢复。修复：恢复动作（_resume_applied）优先于台账跳过。
- RC-B：GitCode Note Hook 的 issue 号位于 `object_attributes.noteable_iid`（iid 是评论自身的 id），
  2.0b 恢复块漏解析 → `_resume_issue_waiting` 不执行。修复：补齐 noteable_iid。

覆盖《§六》QA-01~QA-04 / QA-07：
- QA-01：noteable_iid 仅含、Issue awaiting_input、无台账 → resume 生效（status→processing，无 outbound_ledger_hit）
- QA-02：同上但评论 ID 已在台账（模拟经 grape VCS API 代发）→ 不产生 outbound_ledger_hit skip、仍 resume
- QA-03：pending_review 用户回复 → 恢复 processing，不被台账拦
- QA-04：bot/系统作者评论（台账命中、Issue 非等待态）→ 仍 outbound_ledger_hit skip（S11-1 回归护栏）
- QA-07：/done 指令经 noteable_iid 解析 → confirm_issue_done 生效（RC-B 旁证）
"""
import json
import sys
import threading
from unittest.mock import patch

import pytest

import sys as _sys
import sqlite3


class _DbProxy:
    """动态转发到当前 session_manager.db（被 fixture patch 后即 mock 库）。"""

    def __getattr__(self, attr):
        import src.session_manager as _sm
        return getattr(_sm.db, attr)


class _DbLockProxy:
    def __enter__(self):
        import src.session_manager as _sm
        self._real = _sm.db_lock
        return self._real.__enter__()

    def __exit__(self, *args):
        return self._real.__exit__(*args)


db = _DbProxy()
db_lock = _DbLockProxy()

_PATCH_DB_MODULES = (
    "src.session_manager", "src.api.event_store", "src.api.event_pipeline",
    "src.user.db", "src.api.session_store", "src.api.issues_center",
    "src.api.db_setup", "src.api.routes.events", "src.api.token_resolver",
)


@pytest.fixture(autouse=True)
def _mock_db(monkeypatch):
    """每测试独立内存库：建表走真实 init_database（内部函数级 import 取到 mock）。"""
    import src.session_manager as _sm
    import src.api.db_setup as _dbs
    _conn = sqlite3.connect(":memory:", check_same_thread=False)
    _lock = threading.RLock()
    for _name in _PATCH_DB_MODULES:
        _mod = _sys.modules.get(_name)
        if _mod is None:
            continue
        monkeypatch.setattr(_mod, "db", _conn, raising=False)
        monkeypatch.setattr(_mod, "db_lock", _lock, raising=False)
    _dbs.init_database()
    yield
    _conn.close()


from src.api import event_pipeline as ep
from src import outbound_ledger as ol


RK = "gitcode:test/rsum"
_clean_sql = [
    "DELETE FROM events WHERE repo_key LIKE 'gitcode:test/rsum%'",
    "DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:test/rsum%'",
    "DELETE FROM event_active_guard WHERE repo_key LIKE 'gitcode:test/rsum%'",
    "DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:test/rsum%'",
    "DELETE FROM issue WHERE repo_key LIKE 'gitcode:test/rsum%'",
    "DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:test/rsum%'",
    "DELETE FROM outbound_content_ledger WHERE repo_key LIKE 'gitcode:test/rsum%'",
]


def _clean():
    ol._ensure_table()
    with ol._TTL_LOCK:
        ol._TTL_SET.clear()
    with db_lock:
        c = db.cursor()
        for sql in _clean_sql:
            try:
                c.execute(sql)
            except Exception:
                pass
        db.commit()


def _run(payload: dict, header="Note Hook"):
    raw = json.dumps(payload).encode()
    headers = {"X-GitCode-Event": header}
    return ep.run_pipeline(raw, headers, account="test/rsum", repo_key_override="test/rsum")


def _seed_issue(status="awaiting_input", num=8):
    """构造 issue 卡片（status 可指定 awaiting_input / pending_review / processing ...）。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (RK, num))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (2, "realuser", RK, num, "需求标题", "desc", status))
        c.execute(
            "INSERT INTO issue_state (repo_key, issue_id, stage, owner_agent) VALUES (?,?,?,?)",
            (RK, f"#{num}", "vod", "vod"))
        db.commit()


def _issue_status(num=8):
    with db_lock:
        r = db.cursor().execute(
            "SELECT status FROM issue WHERE repo_key=? AND issue_number=?",
            (RK, num)).fetchone()
        return r[0] if r else None


def _skipped_reasons():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT reason FROM event_skipped WHERE repo_key=? ORDER BY rowid DESC LIMIT 5", (RK,))
        return [r[0] for r in c.fetchall()]


def _comment_payload_noteable(username="realuser", note="回答问题", num=8, cid=90001):
    """GitCode Note Hook 真实形态：issue 号在 noteable_iid，iid 缺省/id 是评论自身 id。"""
    return {
        "object_kind": "note",
        "project": {"path_with_namespace": "test/rsum"},
        "object_attributes": {"noteable_type": "Issue", "noteable_iid": num, "note": note, "id": cid},
        "user": {"username": username},
    }


# ---------- QA-01：noteable_iid 仅含、无台账 → resume 生效（RC-B） ----------

def test_qa01_noteable_iid_resume_awaiting():
    _clean()
    _seed_issue(status="awaiting_input")
    _run(_comment_payload_noteable())
    assert _issue_status() == "processing", _issue_status()
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


# ---------- QA-02：同 QA-01 但评论 ID 已在台账 → resume 优先于台账（RC-A） ----------

def test_qa02_ledger_hit_resume_supersedes():
    _clean()
    _seed_issue(status="awaiting_input")
    ol.record_outbound("gitcode", RK, "issue_comment", "90001", issue_number="8")
    _run(_comment_payload_noteable(cid=90001))
    assert _issue_status() == "processing", _issue_status()
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


# ---------- QA-03：pending_review 用户回复 → 恢复 processing，不被台账拦 ----------

def test_qa03_pending_review_resume():
    _clean()
    _seed_issue(status="pending_review")
    ol.record_outbound("gitcode", RK, "issue_comment", "90003", issue_number="8")
    _run(_comment_payload_noteable(cid=90003))
    assert _issue_status() == "processing", _issue_status()
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


# ---------- QA-04：bot 作者 + 台账命中 + 非等待态 → 仍被台账拦（S11-1 回归护栏） ----------

def test_qa04_bot_self_post_still_ledger_blocked():
    _clean()
    _seed_issue(status="processing")
    ol.record_outbound("gitcode", RK, "issue_comment", "90004", issue_number="8")
    _run(_comment_payload_noteable(username="grape_bot", cid=90004))
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()


# ---------- QA-07：/done 指令经 noteable_iid 解析 → confirm_issue_done（RC-B 旁证） ----------

def test_qa07_done_command_via_noteable_iid():
    """/done 经 noteable_iid 解析 → confirm_issue_done 生效（RC-B 旁证）。

    confirm_issue_done 只翻转 active/processing → completed（awaiting_input 不适用），
    故 seed 用 processing；同时断言记 user_confirm_done skip（证明指令路径被命中而非被 swallow）。
    """
    _clean()
    _seed_issue(status="processing")
    _run(_comment_payload_noteable(note="/done"))
    assert _issue_status() == "completed", _issue_status()
    assert "user_confirm_done" in _skipped_reasons(), _skipped_reasons()


# ---------- 补充护栏：Issue 非等待态 + 台账命中 → 台账照常拦（豁免不开窗） ----------

def test_ledger_still_blocks_when_not_waiting():
    _clean()
    _seed_issue(status="processing")
    ol.record_outbound("gitcode", RK, "issue_comment", "90005", issue_number="8")
    _run(_comment_payload_noteable(cid=90005))
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()


# ---------- 写侧净化（§2.2）：git-repo API 评论记账按 agent_type 区分不增垃圾台账 ----------

def _post_comment_via_git_repo(agent_type: str, body: str = "测试评论", num: int = 8,
                               monkeypatch=None, gitcode_calls=None) -> int:
    """直接调用 git_repo_api.create_issue_comment（token 依赖覆盖），返回落台账的记录数。"""
    from fastapi.testclient import TestClient
    from src.api.main import app
    from src.api.deps import get_current_user_or_api_key
    import src.api.git_repo_api as grapi

    _uid = 2
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": _uid, "username": "realuser", "auth_type": "session_token",
        "agent_type": agent_type, "user_id": 0, "session_key": "s1",
        "repo_key": "gitcode:test/rsum"}

    def _fake_req(method, url, token="", body=None):
        return {"id": 90099, "ok": True}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: "bot_tok")
    monkeypatch.setattr("src.api.deps.get_user_token_with_refresh", lambda *a, **k: "user_tok")

    client = TestClient(app)
    try:
        r = client.post("/api/git-repo/test/rsum/issues/{0}/comments".format(num),
                        json={"body": body})
        assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides.clear()
    with db_lock:
        c = db.cursor()
        return c.execute(
            "SELECT COUNT(*) FROM outbound_content_ledger "
            "WHERE repo_key=? AND content_type='issue_comment' AND content_id='90099'",
            (RK,)).fetchone()[0]


def test_qa05_chat_relay_not_ledgered(monkeypatch):
    """chat 会话（用户 token 代发 = 用户真实回复）→ 不记台账（回声必须触发恢复）。"""
    _clean()
    n = _post_comment_via_git_repo("chat", body="用户澄清回复", monkeypatch=monkeypatch)
    assert n == 0, n


def test_qa06_devops_agent_still_ledgered(monkeypatch):
    """devops 事件 agent（bot 身份系统回帖）→ 仍记台账（防自循环不回退）。"""
    _clean()
    n = _post_comment_via_git_repo("vod", body="系统回帖", monkeypatch=monkeypatch)
    assert n == 1, n