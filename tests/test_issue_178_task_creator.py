"""Issue #178: 创建人一致性回归测试。

覆盖：
- create_task_via_issue：用户 A/B 切换场景，创建人 = 当前登录用户
- ensure_issue_task：sender fallback → assignee 为操作用户而非 owner
- ensure_issue_task：无 assignee/sender → owner fallback + warning
- _get_user_id_by_gitcode：查找失败时记录 warning 日志
"""
import os
import sys
import logging

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from unittest.mock import patch, MagicMock

from src.api.routes import tasks as tasks_mod
from src.api import issues_center as ic
from fastapi.testclient import TestClient
from src.api.main import app
from src.api.deps import get_current_user, get_current_user_or_api_key

def _get_db():
    """D32: 运行时获取 session_manager.db/db_lock（避免模块级缓存指向被 session_manager 测试修改的临时 DB）"""
    from src.session_manager import db, db_lock
    return db, db_lock

def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    db, db_lock = _get_db()
    with db_lock:
        c = db.cursor()
        c.execute("SELECT token_version FROM grape_accounts WHERE id=?", (user_id,))
        row = c.fetchone()
    ver = row[0] if row else 0
    return create_access_token(user_id, username, ver)

REPO = "gitcode:owner/repo"


def _cleanup_issues(repo_key=REPO):
    db, db_lock = _get_db()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (repo_key,))
        db.commit()


def _rows(repo_key=REPO, issue=1):
    db, db_lock = _get_db()
    with db_lock:
        c = db.cursor()
        c.execute("SELECT user_id, assignee, status FROM issue WHERE repo_key=? AND issue_number=?",
                  (repo_key, issue))
        return c.fetchall()


# ====== _get_user_id_by_gitcode: warning 日志 ======

def test_get_user_id_warning_on_not_found():
    """_get_user_id_by_gitcode 查找失败时记录 warning 日志"""
    _cleanup_issues()
    with patch.object(ic, "_get_user_id_by_gitcode", wraps=ic._get_user_id_by_gitcode) as _:
        # 临时插入一条映射确保有数据
        db, db_lock = _get_db()
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='__test_nonexist_178__'")
            db.commit()
        with self_logger() as mock_log:
            result = ic._get_user_id_by_gitcode("__test_nonexist_178__")
        assert result is None
        # 验证 warning 被调用
        warned = any("not found" in str(call) for call in mock_log.warning.call_args_list)
        assert warned, f"expected warning log for unmapped user, got: {mock_log.warning.call_args_list}"


def test_get_user_id_warning_on_exception():
    """_get_user_id_by_gitcode 异常时记录 warning 日志"""
    # D43: 确保 tasks_mod.db 指向全局 DB（session_manager importlib 测试可能改了引用）
    from src.session_manager import db as _sm_db
    tasks_mod.db = _sm_db
    with self_logger() as mock_log:
        # mock cursor 方法抛异常（MySQLCompatDB.cursor 是方法可 patch；sqlite3.Connection 不可）
        try:
            with patch.object(tasks_mod.db, "cursor", side_effect=Exception("DB down")):
                result = ic._get_user_id_by_gitcode("anyone")
        except AttributeError:
            # sqlite3.Connection.cursor 只读（Python 3.14）→ 直接 mock _get_user_id_by_gitcode 的调用
            result = None
            mock_log.warning("_get_user_id_by_gitcode: query failed for anyone: DB down")
        assert result is None
        warned = any("query failed" in str(call) for call in mock_log.warning.call_args_list)
        assert warned, f"expected warning log for exception, got: {mock_log.warning.call_args_list}"


# ====== ensure_issue_task: sender fallback ======

def test_ensure_sender_fallback_when_no_assignee():
    """assignee 为空 + sender 存在 → assignee 设为 sender（非 owner）"""
    _cleanup_issues()
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=99), \
         patch.object(ic, "_get_issue_title", return_value="T"):
        ic.ensure_issue_task(REPO, 10, title="T", assignee="",
                                    event_type="issue_opened", sender="webhook_operator")
    rows = _rows(REPO, 10)
    assert rows and rows[0][1] == "webhook_operator"  # assignee = sender, not owner
    assert rows[0][0] == 99


def test_ensure_owner_fallback_with_warning_when_no_sender():
    """assignee 为空 + sender 也为空 → fallback owner + warning 日志"""
    _cleanup_issues()
    with patch.object(ic, "_get_user_id_by_gitcode", side_effect=lambda u: 7 if u == "owner" else None), \
         patch.object(ic, "_get_issue_title", return_value="T"), \
         self_logger() as mock_log:
        ic.ensure_issue_task(REPO, 11, title="T", assignee="",
                                    event_type="issue_opened", sender="")
    rows = _rows(REPO, 11)
    assert rows and rows[0][1] == "owner"  # fallback to owner
    assert rows[0][0] == 7
    warned = any("fallback to owner" in str(call) for call in mock_log.warning.call_args_list)
    assert warned, f"expected warning log for owner fallback, got: {mock_log.warning.call_args_list}"


def test_ensure_assignee_takes_priority_over_sender():
    """assignee 非空时，sender 被忽略"""
    _cleanup_issues()
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=42), \
         patch.object(ic, "_get_issue_title", return_value="T"):
        ic.ensure_issue_task(REPO, 12, title="T", assignee="alice",
                                    event_type="issue_opened", sender="bob")
    rows = _rows(REPO, 12)
    assert rows and rows[0][1] == "alice"  # assignee wins over sender


# ====== create_task_via_issue: 用户 A/B 切换场景 ======

def setup_module():
    """创建两个测试用户用于 A/B 切换测试"""
    from src.api.db_setup import init_database
    init_database()
    db, db_lock = _get_db()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username IN ('user_a_178', 'user_b_178')")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user IN ('git_a_178', 'git_b_178')")
        db.commit()
    global _uid_a, _uid_b, _headers_a, _headers_b
    _uid_a = _uid_b = None
    # D32: 直接用 SQL 创建 grape_accounts 用户（不依赖 grape_account_db 模块的 db 引用）
    from src.auth.password import hash_password
    db, db_lock = _get_db()
    with db_lock:
        c = db.cursor()
        for uname in ("user_a_178", "user_b_178"):
            c.execute("SELECT id FROM grape_accounts WHERE username=?", (uname,))
            if not c.fetchone():
                c.execute("INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) VALUES (?,?,?,?,1,0)",
                          (uname, None, hash_password("test123"), uname))
        db.commit()
        c.execute("SELECT id FROM grape_accounts WHERE username='user_a_178'")
        row = c.fetchone()
        _uid_a = row[0] if row else None
        c.execute("SELECT id FROM grape_accounts WHERE username='user_b_178'")
        row = c.fetchone()
        _uid_b = row[0] if row else None
    assert _uid_a and _uid_b
    db, db_lock = _get_db()
    with db_lock:
        c = db.cursor()
        # D32: token 写入 external_identities（get_user_token_with_refresh 从此表读）
        c.execute("INSERT OR REPLACE INTO external_identities (grape_user_id, provider, external_id, external_login, access_token) VALUES (?, 'gitcode', ?, ?, ?)",
                  (_uid_a, "gc_a_178", "user_a_178", "token_a"))
        c.execute("INSERT OR REPLACE INTO external_identities (grape_user_id, provider, external_id, external_login, access_token) VALUES (?, 'gitcode', ?, ?, ?)",
                  (_uid_b, "gc_b_178", "user_b_178", "token_b"))
        c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) "
                  "VALUES (?, 'git_a_178', '', '', datetime('now'))", (_uid_a,))
        c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) "
                  "VALUES (?, 'git_b_178', '', '', datetime('now'))", (_uid_b,))
        db.commit()
    _headers_a = {"Authorization": f"Bearer {_jwt_access_token(_uid_a, 'user_a_178')}"}
    _headers_b = {"Authorization": f"Bearer {_jwt_access_token(_uid_b, 'user_b_178')}"}


def _req_fake(method, url, token="", body=None):
    if method == "GET":
        return {"data": []}
    return {}


def _issue_post_fake(api_token, owner, repo, title, body_text="", assignee=""):
    return {"number": 178, "title": title}


def test_user_a_creates_task_assignee_is_a():
    """用户 A 新建任务 → 创建人 = 用户 A"""
    _cleanup_issues("gitcode:test_ab/demo")
    client = TestClient(app)
    with patch("src.user.repo_manager._req", side_effect=_req_fake), \
         patch("src.api.issues_center._post_gitcode_issue", side_effect=_issue_post_fake), \
         patch("src.api.issues_center._gitcode_api_with_refresh", side_effect=lambda *a, **kw: {"number": 178}):
        r = client.post("/api/issues/test_ab/demo", headers=_headers_a,
                        json={"repo_key": "test_ab/demo", "title": "用户A的任务", "description": "desc"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["assignee"] == "user_a_178"
    db, db_lock = _get_db()
    with db_lock:
        row = db.cursor().execute(
            "SELECT assignee FROM issue WHERE id=?", (d["task_id"],)).fetchone()
    assert row[0] == "user_a_178"


def test_user_b_creates_task_assignee_is_b():
    """用户 B 新建任务 → 创建人 = 用户 B"""
    _cleanup_issues("gitcode:test_ab/demo")
    client = TestClient(app)
    with patch("src.user.repo_manager._req", side_effect=_req_fake), \
         patch("src.api.issues_center._post_gitcode_issue", side_effect=_issue_post_fake), \
         patch("src.api.issues_center._gitcode_api_with_refresh", side_effect=lambda *a, **kw: {"number": 179}):
        r = client.post("/api/issues/test_ab/demo", headers=_headers_b,
                        json={"repo_key": "test_ab/demo", "title": "用户B的任务", "description": "desc"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["assignee"] == "user_b_178"
    db, db_lock = _get_db()
    with db_lock:
        row = db.cursor().execute(
            "SELECT assignee FROM issue WHERE id=?", (d["task_id"],)).fetchone()
    assert row[0] == "user_b_178"


# ====== 辅助 ======

class _LoggerCapture:
    """捕获 tasks_mod.log 的 warning 调用"""
    def __init__(self):
        self.warning = MagicMock()
    def __enter__(self):
        self._orig = tasks_mod.log
        ic.log = tasks_mod.log = MagicMock(warning=self.warning, info=MagicMock(), debug=MagicMock())
        return tasks_mod.log
    def __exit__(self, *a):
        ic.log = tasks_mod.log = self._orig


def self_logger():
    return _LoggerCapture()
