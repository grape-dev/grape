"""Issue #1408：已完成需求可手动撤销回待审核。

覆盖：
- 状态机 completed → pending_review 唯一出口；
- 属主撤销成功且回读一致、stage 回退 pending_review、completed_at/completed_reason 清空、审计留痕；
- 撤销刷新 updated_at，审计保留撤销前完成时间/原因；
- 非属主 403、会话 token（tk_）403、非法迁移 400、并发态变更 409、需求不存在 404、缺参 400；
- 重复提交幂等成功；
- 回归 pending_review → processing/completed/cancelled 与 cancelled → active。

隔离设计（只影响本用例，不改动其它用例）：
仓库测试基线存在进程级 `src.kernel.session_manager` 单例污染（部分用例把 stub 注入
`sys.modules`、或在收集期 `reload` 后不恢复），导致全量混跑时「写入的库」与「路由读到的库」
分裂（`sqlite3.OperationalError: no such table: issue`），先例同样问题见
`tests/runtime/test_issue463_table_governance.py`。本用例因此不依赖进程级单例：
模块初始化时用独立库文件（`data/test_issue1408_revert.db`）建 Schema，并在每次调用路由
函数期间把该连接临时挂到 `src.kernel.session_manager` 及相关模块上，调用结束立即还原，
用例之间与用例之外互不影响。路由鉴权语义由 `require_no_session_token` 单测覆盖。
"""
import contextlib
import importlib
import json
import os
import sqlite3
import sys
import threading
import types
from unittest import mock

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _ROOT)

import pytest
from fastapi import HTTPException

OWNER = "test_issue1408"
REPO = "revert"
REPO_KEY = f"gitcode:{OWNER}/{REPO}"
OTHER = "test_issue1408_other"
OWNER_ID = 941408
OTHER_ID = 941409
DB_FILE = os.path.join(_ROOT, "data", "test_issue1408_revert.db")

_OWNER = {"id": OWNER_ID, "username": OWNER, "auth_type": "jwt"}
_OTHER = {"id": OTHER_ID, "username": OTHER, "auth_type": "jwt"}
_SESSION_TOKEN_USER = {"id": OWNER_ID, "username": OWNER, "auth_type": "session_token"}

# 在模块导入期捕获 db/db_lock 的模块：调用路由前临时指向本用例的库
_PATCHED_MODULES = (
    "src.api.db_setup",
    "src.api.issues_center",
    "src.domains.mgmt.auth.audit",
    "src.domains.mgmt.auth.grape_account_db",
    "src.domains.runtime.routes.issues",
)

_S = {}


def _make_stub(conn, lock):
    """构造只暴露本用例连接的 session_manager 替身（其余属性沿用真实模块）。"""
    real = importlib.import_module("src.kernel.session_manager")
    stub = types.ModuleType("src.kernel.session_manager")
    for key, val in list(vars(real).items()):
        if not key.startswith("__"):
            setattr(stub, key, val)
    stub.db = conn
    stub.db_lock = lock
    stub._db_path = DB_FILE
    return stub


@contextlib.contextmanager
def _scoped(conn=None, lock=None):
    """把本用例连接临时挂到 session_manager 与相关模块，退出即还原。"""
    stub, base_conn, base_lock = _S["stub"], _S["conn"], _S["lock"]
    db_obj = base_conn if conn is None else conn
    lock_obj = base_lock if lock is None else lock
    saved_mods = {}
    pkg = sys.modules.get("src.kernel")
    old_attr = getattr(pkg, "session_manager", None) if pkg is not None else None
    old_stub = sys.modules.get("src.kernel.session_manager")
    try:
        stub.db, stub.db_lock = db_obj, lock_obj
        sys.modules["src.kernel.session_manager"] = stub
        if pkg is not None:
            pkg.session_manager = stub
        for name in _PATCHED_MODULES:
            mod = sys.modules.get(name)
            if mod is None:
                continue
            for attr in ("db", "db_lock"):
                if hasattr(mod, attr):
                    saved_mods[(name, attr)] = getattr(mod, attr)
                    setattr(mod, attr, lock_obj if attr == "db_lock" else db_obj)
        yield
    finally:
        for (name, attr), val in saved_mods.items():
            mod = sys.modules.get(name)
            if mod is not None:
                setattr(mod, attr, val)
        if old_stub is not None:
            sys.modules["src.kernel.session_manager"] = old_stub
        else:
            sys.modules.pop("src.kernel.session_manager", None)
        if pkg is not None:
            if old_attr is not None:
                pkg.session_manager = old_attr
            else:
                try:
                    delattr(pkg, "session_manager")
                except AttributeError:
                    pass
        stub.db, stub.db_lock = base_conn, base_lock


def _init_db():
    """首次使用时建库建表（幂等）。"""
    if _S.get("conn") is not None:
        return
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_FILE + suffix)
        except OSError:
            pass
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    lock = threading.RLock()
    _S.update(conn=conn, lock=lock, stub=_make_stub(conn, lock))
    for name in _PATCHED_MODULES:
        importlib.import_module(name)
    with _scoped():
        from src.api import db_setup
        db_setup.init_database()


def _db():
    _init_db()
    return _S["conn"], _S["lock"]


def setup_module():
    _init_db()


def teardown_module():
    conn = _S.pop("conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _S.clear()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_FILE + suffix)
        except OSError:
            pass


def _mk_issue(number, user_id=None, status="completed", stage="done",
              reason="review_passed", completed_at="2026-01-01 00:00:00",
              owner_agent="coding", a2a_seq=56916):
    db, db_lock = _db()
    uid = OWNER_ID if user_id is None else user_id
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) "
            "VALUES (?,?,?,?,?,?)",
            (uid, "grape_bot", REPO_KEY, number, f"Revert #{number}", status))
        issue_id = c.lastrowid
        c.execute(
            "UPDATE issue SET status=?, stage=?, completed_reason=?, completed_at=?, "
            "owner_agent=?, a2a_seq=? WHERE id=?",
            (status, stage, reason, completed_at, owner_agent, a2a_seq, issue_id))
        db.commit()
        return issue_id


def _issue_row(issue_id):
    db, db_lock = _db()
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT status, stage, completed_reason, completed_at FROM issue WHERE id=?",
            (issue_id,)).fetchone()
    return row


def _issue_owner_row(issue_id):
    db, db_lock = _db()
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT assignee, owner_agent, a2a_seq FROM issue WHERE id=?",
            (issue_id,)).fetchone()
    return row


def _issue_updated_at(issue_id):
    db, db_lock = _db()
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT updated_at FROM issue WHERE id=?", (issue_id,)).fetchone()
    return (row[0] if row else "") or ""


def _audit_rows(issue_id):
    db, db_lock = _db()
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT detail FROM audit_logs WHERE event_type='issue_reverted_to_pending_review'",
        ).fetchall()
    out = []
    for (detail,) in rows:
        try:
            d = json.loads(detail or "{}")
        except Exception:
            continue
        if d.get("issue_id") == issue_id:
            out.append(d)
    return out


def _call(issue_id, target, user=None, conn=None):
    """调用路由处理函数，调用期间把本用例连接挂到 session_manager。"""
    _init_db()
    from src.domains.runtime.routes.issues import _IssueStatusBody, change_issue_status
    with _scoped(conn=conn):
        return change_issue_status(issue_id, _IssueStatusBody(status=target), user or _OWNER)


class _RaceCursor:
    """包一层 cursor：在首次 SELECT 之后触发一次外部写，模拟并发进程抢先落库。"""

    def __init__(self, cur, on_select):
        self._cur = cur
        self._on_select = on_select
        self.rowcount = -1

    def execute(self, sql, params=()):
        self._cur.execute(sql, params)
        self.rowcount = self._cur.rowcount
        if self._on_select and str(sql).lstrip().upper().startswith("SELECT"):
            cb, self._on_select = self._on_select, None
            cb()
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _RaceConn:
    def __init__(self, conn, on_select):
        self._conn = conn
        self._on_select = on_select

    def cursor(self):
        return _RaceCursor(self._conn.cursor(), self._on_select)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()


class TestStateMachine:
    def test_completed_has_single_revert_exit(self):
        from src.api.issues_center import _TASK_STATUS_FLOWS
        assert _TASK_STATUS_FLOWS["completed"] == ("pending_review",)

    def test_pending_review_flows_intact(self):
        from src.api.issues_center import _TASK_STATUS_FLOWS
        assert set(_TASK_STATUS_FLOWS["pending_review"]) == {"processing", "completed", "cancelled"}


class TestAuthGuard:
    def test_session_token_403(self):
        """会话 token（tk_）不得调用账户/平台管理类端点。"""
        from src.api.deps import require_no_session_token
        with pytest.raises(HTTPException) as ei:
            require_no_session_token(_SESSION_TOKEN_USER)
        assert ei.value.status_code == 403

    def test_jwt_user_passes_guard(self):
        from src.api.deps import require_no_session_token
        assert require_no_session_token(_OWNER) == _OWNER


class TestRevertCompleted:
    def test_owner_revert_success(self):
        issue_id = _mk_issue(14081)
        out = _call(issue_id, "pending_review")
        assert out["ok"] is True
        assert out["status"] == "pending_review"

        status, stage, reason, completed_at = _issue_row(issue_id)
        assert status == "pending_review"
        assert stage == "pending_review"
        assert reason is None or reason == ""
        assert completed_at is None or completed_at == ""

        audits = _audit_rows(issue_id)
        assert len(audits) == 1
        assert audits[0]["from_status"] == "completed"
        assert audits[0]["to_status"] == "pending_review"
        assert audits[0]["from_stage"] == "done"

    def test_owner_revert_preserves_owner_fields(self):
        """撤销只回退 status/stage，不抹掉 assignee/owner_agent/a2a_seq。"""
        issue_id = _mk_issue(14086)
        _call(issue_id, "pending_review")
        assignee, owner_agent, a2a_seq = _issue_owner_row(issue_id)
        assert assignee == "grape_bot"
        assert owner_agent == "coding"
        assert a2a_seq == 56916

    def test_owner_revert_audit_uses_actual_stage(self):
        """审计 from_stage 取实际 stage，而非硬编码 done。"""
        issue_id = _mk_issue(14087, stage="legacy_done")
        _call(issue_id, "pending_review")
        audits = _audit_rows(issue_id)
        assert len(audits) == 1
        assert audits[0]["from_stage"] == "legacy_done"

    def test_owner_revert_audit_keeps_prev_completion(self):
        """审计 detail 保留撤销前的 completed_at/completed_reason。"""
        issue_id = _mk_issue(14088, reason="review_passed",
                             completed_at="2026-01-02 03:04:05")
        _call(issue_id, "pending_review")
        audits = _audit_rows(issue_id)
        assert len(audits) == 1
        assert audits[0]["prev_completed_at"] == "2026-01-02 03:04:05"
        assert audits[0]["prev_completed_reason"] == "review_passed"

    def test_owner_revert_refreshes_updated_at(self):
        """撤销写路径刷新 updated_at，与基线写路径惯例一致。"""
        issue_id = _mk_issue(14089)
        before = _issue_updated_at(issue_id)
        _call(issue_id, "pending_review")
        after = _issue_updated_at(issue_id)
        assert after and after != before

    def test_stale_status_guard_conflict_409(self):
        """撤销 UPDATE 带 status='completed' 守卫：读后写前被并发改成 cancelled 时返回 409 且不写审计。"""
        issue_id = _mk_issue(14095)
        conn, _ = _db()

        def _concurrent_update():
            # 模拟另一进程在「SELECT 之后、UPDATE 之前」把该需求改成 cancelled 并落库
            other = conn.cursor()
            other.execute("UPDATE issue SET status='cancelled' WHERE id=?", (issue_id,))
            conn.commit()

        with pytest.raises(HTTPException) as ei:
            _call(issue_id, "pending_review", conn=_RaceConn(conn, _concurrent_update))
        assert ei.value.status_code == 409
        assert _issue_row(issue_id)[0] == "cancelled"
        assert _audit_rows(issue_id) == []

    def test_non_owner_403(self):
        issue_id = _mk_issue(14082)
        with pytest.raises(HTTPException) as ei:
            _call(issue_id, "pending_review", _OTHER)
        assert ei.value.status_code == 403
        assert _issue_row(issue_id)[0] == "completed"

    def test_illegal_migration_400(self):
        issue_id = _mk_issue(14084)
        with pytest.raises(HTTPException) as ei:
            _call(issue_id, "processing")
        assert ei.value.status_code == 400
        assert _issue_row(issue_id)[0] == "completed"

    def test_unknown_issue_404(self):
        with pytest.raises(HTTPException) as ei:
            _call(149999999, "pending_review")
        assert ei.value.status_code == 404

    def test_illegal_status_400(self):
        issue_id = _mk_issue(14096)
        with pytest.raises(HTTPException) as ei:
            _call(issue_id, "not_a_status")
        assert ei.value.status_code == 400

    def test_repeat_submit_idempotent(self):
        issue_id = _mk_issue(14085)
        out1 = _call(issue_id, "pending_review")
        assert out1["status"] == "pending_review"
        out2 = _call(issue_id, "pending_review")
        assert out2["status"] == "pending_review"
        assert _issue_row(issue_id)[0] == "pending_review"


class TestRegression:
    @pytest.mark.parametrize("target", ["processing", "completed", "cancelled"])
    def test_pending_review_to_targets(self, target):
        issue_id = _mk_issue(14090 + ["processing", "completed", "cancelled"].index(target),
                             status="pending_review", stage="pending_review",
                             reason=None, completed_at=None)
        out = _call(issue_id, target)
        assert out["status"] == target
        assert _issue_row(issue_id)[0] == target

    def test_cancelled_to_active(self):
        issue_id = _mk_issue(14093, status="cancelled", stage="pending_review",
                             reason="cancelled", completed_at="2026-01-01 00:00:00")
        out = _call(issue_id, "active")
        assert out["status"] == "active"
        assert _issue_row(issue_id)[0] == "active"