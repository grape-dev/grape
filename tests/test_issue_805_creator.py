"""Issue #805：创建人（created_by/creator）一等数据化回归测试。

覆盖：
- ensure_issue_task 写 creator（新建 INSERT 落库 created_by）
- 幂等不覆盖（复用已有卡时仅空值补写，assignee 流转不改创建人）
- _query_tasks 透出 creator
- _build_creator_filter 单值/多值 SQL 过滤
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from unittest.mock import patch

from src.api import issues_center as ic
from src.session_manager import db, db_lock

REPO = "gitcode:creator-test/repo"


def _creator_of(issue: int):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT created_by FROM issue WHERE repo_key=? AND issue_number=?", (REPO, issue))
        row = c.fetchone()
        return row[0] if row else None


def _cleanup():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        db.commit()


def setup_function():
    _cleanup()


def teardown_function():
    _cleanup()


def test_ensure_writes_creator_on_insert():
    """新建建档：creator 落库 issue.created_by"""
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=11), \
         patch.object(ic, "_get_issue_title", return_value="T"):
        ic.ensure_issue_task(REPO, 101, title="T", assignee="alice",
                             event_type="issue_opened", creator="xocto")
    assert _creator_of(101) == "xocto"


def test_ensure_creator_idempotent_no_overwrite():
    """幂等不覆盖：已有 created_by 非空时，后续事件（创建人不同/为空）不改创建人"""
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=11), \
         patch.object(ic, "_get_issue_title", return_value="T"):
        ic.ensure_issue_task(REPO, 102, title="T", assignee="alice",
                             event_type="issue_opened", creator="xocto")
        # 流转后 assignee 变化、creator 传不同值/空值 → created_by 均不变
        ic.ensure_issue_task(REPO, 102, title="T", assignee="bob",
                             event_type="issue_comment", creator="someone-else")
        ic.ensure_issue_task(REPO, 102, title="T", assignee="carol",
                             event_type="issue_comment", creator="")
    assert _creator_of(102) == "xocto"


def test_ensure_creator_backfill_only_when_empty():
    """存量空值补写：老卡 created_by 为空时，事件携带 creator 仅回填一次"""
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=11), \
         patch.object(ic, "_get_issue_title", return_value="T"):
        # 第一次无 creator（模拟历史建档）→ 空值
        ic.ensure_issue_task(REPO, 103, title="T", assignee="alice", event_type="issue_opened")
        assert _creator_of(103) == ""
        # 后续事件带 creator → 空值回填
        ic.ensure_issue_task(REPO, 103, title="T", assignee="alice",
                             event_type="issue_comment", creator="xocto")
        assert _creator_of(103) == "xocto"


def test_query_tasks_exposes_creator():
    """_query_tasks 返回 dict 含 creator（向后兼容：assignee 仍在）"""
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=11), \
         patch.object(ic, "_get_issue_title", return_value="T"):
        ic.ensure_issue_task(REPO, 104, title="T", assignee="alice",
                             event_type="issue_opened", creator="xocto")
    tasks = ic._query_tasks("repo_key=?", [REPO], "created_at DESC", include_user_id=True)
    hit = [t for t in tasks if t.get("issue_number") == 104]
    assert hit and hit[0].get("creator") == "xocto"
    assert hit[0].get("assignee") == "alice"
    assert hit[0].get("user_id") == 11  # include_user_id 索引不受新增列影响


def test_build_creator_filter():
    """_build_creator_filter：单值 =? / 多值 IN / 空串无操作"""
    clauses, params = [], []
    ic._build_creator_filter("xocto", clauses, params)
    assert clauses == ["created_by=?"] and params == ["xocto"]

    clauses, params = [], []
    ic._build_creator_filter("a, b", clauses, params)
    assert clauses == ["created_by IN (?,?)"] and params == ["a", "b"]

    clauses, params = [], []
    ic._build_creator_filter("", clauses, params)
    assert clauses == [] and params == []


def test_creator_filter_selects_rows():
    """creator 过滤口径：created_by 命中（与 assignee 流转无关）"""
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=11), \
         patch.object(ic, "_get_issue_title", return_value="T"):
        ic.ensure_issue_task(REPO, 105, title="T", assignee="alice",
                             event_type="issue_opened", creator="xocto")
        ic.ensure_issue_task(REPO, 106, title="T", assignee="bob",
                             event_type="issue_opened", creator="other")
    clauses, params = ["repo_key=?"], [REPO]
    ic._build_creator_filter("xocto", clauses, params)
    tasks = ic._query_tasks(" AND ".join(clauses), params, "created_at DESC", include_user_id=False)
    nums = [t["issue_number"] for t in tasks]
    assert 105 in nums and 106 not in nums
