"""Task Center 新建任务测试：同步建 Issue + 幂等 + 状态迁移。

- POST /api/tasks/create：责任人为 General Settings Account（登录用户 gitcode 账号）
- 幂等：同仓库同标题 open issue 复用 → 同一 task id 不重复
- PUT /api/tasks/{id}/status：状态迁移规则
"""
import os
import sys
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock
from src.user import db as user_db
# JWT access token helper（替代旧的 create_session_token）
def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    return create_access_token(user_id, username)

client = TestClient(app)

_uid = None
_token = ""
_headers = {}


def setup_module():
    global _uid, _token, _headers
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username IN ('tc_user')")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user IN ('tc_git_user')")
        db.commit()
    u = user_db.create_user("tc_user", "test123")
    if not u:
        u = user_db.get_user_by_username("tc_user")
    assert u, "测试用户创建失败"
    _uid = u["id"]
    _token = _jwt_access_token(_uid, u["username"])
    _headers = {"Authorization": f"Bearer {_token}"}
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE user_accounts SET gitcode_api_token='tc_token_123' WHERE id=?", (_uid,))
        c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) "
                  "VALUES (?, 'tc_git_user', '', '', datetime('now'))", (_uid,))
        db.commit()


def _req_fake(method, url, token="", body=None):
    """mock GitCode API GET：无同名 open issue（POST 创建走 _post_gitcode_issue，另行 mock）"""
    if method == "GET":
        return {"data": []}
    return {}


def _req_fake_with_existing(method, url, token="", body=None):
    """mock：GET 返回已存在同名 open issue（幂等复用场景）。"""
    if method == "GET":
        return {"data": [{"number": 42, "title": "测试任务标题", "state": "open"}]}
    return {}


def _issue_post_fake(api_token, owner, repo, title, body_text="", assignee=""):
    """mock _post_gitcode_issue：创建返回新 issue number 42。"""
    return {"number": 42, "title": title}


def test_create_task_creates_issue_and_task():
    """新建任务：POST 创建 Issue → 建档 task；assignee= 系统 Account（登录用户 gitcode 账号）。"""
    with patch("src.user.repo_manager._req", side_effect=_req_fake), \
         patch("src.api.routes.tasks._post_gitcode_issue", side_effect=_issue_post_fake):
        r = client.post("/api/tasks/create", headers=_headers,
                        json={"repo_key": "tc_git/demo", "title": "测试任务标题", "description": "任务描述"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["created"] is True
    assert d["issue_number"] == 42
    assert d["task_id"] > 0
    assert d["assignee"] == "tc_user"          # General Settings Account = 当前登录用户名（settings.credentials.account）
    with db_lock:
        row = db.cursor().execute(
            "SELECT assignee, repo_key, issue_number, status FROM tasks WHERE id=?", (d["task_id"],)).fetchone()
    assert row[0] == "tc_user"
    assert row[1] == "tc_git/demo" or row[1] == "gitcode:tc_git/demo"
    assert row[2] == 42
    assert row[3] == "active"


def test_create_reuses_existing_issue_same_task_id():
    """幂等：同仓库同标题 open issue 存在 → 不重复创建；同一 issue 任务 id 复用。"""
    with patch("src.user.repo_manager._req", side_effect=_req_fake_with_existing), \
         patch("src.api.routes.tasks._post_gitcode_issue", side_effect=_issue_post_fake):
        r = client.post("/api/tasks/create", headers=_headers,
                        json={"repo_key": "tc_git/demo", "title": "测试任务标题"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["created"] is False          # 复用已有 issue，未新创建
    assert d["issue_number"] == 42
    with patch("src.user.repo_manager._req", side_effect=_req_fake_with_existing), \
         patch("src.api.routes.tasks._post_gitcode_issue", side_effect=_issue_post_fake):
        r2 = client.post("/api/tasks/create", headers=_headers,
                         json={"repo_key": "tc_git/demo", "title": "测试任务标题"})
    assert r2.status_code == 200
    assert r2.json()["task_id"] == d["task_id"]   # 同一 issue → 任务 id 不重复


def test_status_transition_rules():
    """状态迁移：active→processing→completed 合法；completed 终态不可逆。"""
    with patch("src.user.repo_manager._req", side_effect=_req_fake), \
         patch("src.api.routes.tasks._post_gitcode_issue", side_effect=_issue_post_fake):
        tid = client.post("/api/tasks/create", headers=_headers,
                          json={"repo_key": "tc_git/demo", "title": "状态迁移任务"}).json()["task_id"]
    r = client.put(f"/api/tasks/{tid}/status", headers=_headers, json={"status": "processing"})
    assert r.status_code == 200 and r.json()["status"] == "processing"
    r = client.put(f"/api/tasks/{tid}/status", headers=_headers, json={"status": "completed"})
    assert r.status_code == 200 and r.json()["status"] == "completed"
    # 终态不可改
    r = client.put(f"/api/tasks/{tid}/status", headers=_headers, json={"status": "active"})
    assert r.status_code == 400
    # 非法状态
    r = client.put(f"/api/tasks/{tid}/status", headers=_headers, json={"status": "xxx"})
    assert r.status_code == 400
    # 404
    r = client.put("/api/tasks/999999/status", headers=_headers, json={"status": "completed"})
    assert r.status_code == 404


def test_create_requires_fields():
    """缺 title / repo_key → 400。"""
    r = client.post("/api/tasks/create", headers=_headers, json={"repo_key": "tc_git/demo", "title": ""})
    assert r.status_code == 400
    r = client.post("/api/tasks/create", headers=_headers, json={"repo_key": "", "title": "abc"})
    assert r.status_code == 400