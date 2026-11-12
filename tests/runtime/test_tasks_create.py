"""Task Center 新建任务测试：同步建 Issue + 幂等 + 状态迁移。

- POST /api/issues/{owner}/{repo}（原 /api/tasks/create 已废弃移除，C2）：责任人为 General Settings Account（登录用户 gitcode 账号）
- 幂等：同仓库同标题 open issue 复用 → 同一 task id 不重复
- PUT /api/tasks/{id}/status：状态迁移规则
"""

import os

import sys

from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient

from src.api.main import app

from src.kernel.session_manager import db, db_lock

from src.domains.mgmt.user import db as user_db

from src.domains.mgmt.auth import grape_account_db

# JWT access token helper（替代旧的 create_session_token）
def _jwt_access_token(user_id, username="test"):
    from src.kernel.auth_sdk.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))

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
        c.execute("DELETE FROM grape_accounts WHERE username IN ('tc_user')")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user IN ('tc_git_user')")
        db.commit()
    u = grape_account_db.create_grape_account("tc_user", email=None, password="test123")
    if not u:
        u = grape_account_db.get_grape_account_by_username("tc_user")
    assert u, "测试用户创建失败"
    _uid = u["id"]
    _token = _jwt_access_token(_uid, u["username"])
    _headers = {"Authorization": f"Bearer {_token}"}
    with db_lock:
        c = db.cursor()
        # S108：D32 后平台 token 在 external_identities（user_accounts 平台列遗留）
        grape_account_db.create_external_identity(
            _uid, "gitcode", "tc_git_user", external_login="tc_git_user",
            access_token="tc_token_123", refresh_token="", expires_in=0)
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

def _issue_post_fake(self, title, body="", assignee=""):
    """mock VcsApiClient.create_issue 缝：创建返回新 issue number 42。"""
    return {"number": 42, "title": title}

def test_create_task_creates_issue_and_task():
    """新建任务：POST 创建 Issue → 建档 task；assignee= 系统 Account（登录用户 gitcode 账号）。"""
    with patch("src.domains.mgmt.user.repo_manager._req", side_effect=_req_fake), \
         patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_issue", side_effect=_issue_post_fake), \
             patch("src.domains.runtime.vcs.api_client.VcsApiClient.find_open_issue_by_title", return_value=None):
        r = client.post("/api/issues/tc_git/demo", headers=_headers,
                        json={"repo_key": "tc_git/demo", "title": "测试任务标题", "description": "任务描述"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["created"] is True
    assert d["issue_number"] == 42
    assert d["task_id"] > 0
        # S108：Issue #649 设计变更——assignee 不再默认填创建者（留空由仓库方分派）
    assert d["assignee"] == ""
    with db_lock:
        row = db.cursor().execute(
            "SELECT assignee, repo_key, issue_number, status FROM issue WHERE id=?", (d["task_id"],)).fetchone()
    assert row[0] == "tc_git_user"  # S108：建档锚定=创建者身份域 login；响应层不默认指派（#649）
    assert row[1] == "tc_git/demo" or row[1] == "gitcode:tc_git/demo"
    assert row[2] == 42
    assert row[3] == "processing"  # S108：建档触发 pipeline → processing

def test_create_reuses_existing_issue_same_task_id():
    """同仓库同标题 open issue 存在 → 409 提示改标题（S108：用户面建单
    #343 防重复语义；静默复用建档仅存在于 onboarding bot_token 路径，
    该路径由 test_issue_372/627 族覆盖）。"""
    with patch("src.domains.mgmt.user.repo_manager._req", side_effect=_req_fake_with_existing), \
         patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_issue", side_effect=_issue_post_fake), \
             patch("src.domains.runtime.vcs.api_client.VcsApiClient.find_open_issue_by_title",
               return_value=42):  # S108：查重命中既有 issue（契约：int|None）
        r = client.post("/api/issues/tc_git/demo", headers=_headers,
                        json={"repo_key": "tc_git/demo", "title": "测试任务标题", "description": "任务描述"})
    # 现行语义（#343 防重复建单）：用户面建单遇同标题 open issue → 409 提示改标题
    assert r.status_code == 409, r.text
    d = r.json()
    assert d["detail"]["issue_number"] == 42
    with patch("src.domains.mgmt.user.repo_manager._req", side_effect=_req_fake_with_existing), \
         patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_issue", side_effect=_issue_post_fake), \
             patch("src.domains.runtime.vcs.api_client.VcsApiClient.find_open_issue_by_title", return_value=None):
        r2 = client.post("/api/issues/tc_git/demo", headers=_headers,
                         json={"repo_key": "tc_git/demo", "title": "测试任务标题", "description": "任务描述"})
    assert r2.status_code == 200 and r2.json()["ok"] is True  # 查重 None → 正常建档

def test_status_transition_rules():
    """状态迁移：active→processing→completed 合法；completed 终态不可逆。"""
    with patch("src.domains.mgmt.user.repo_manager._req", side_effect=_req_fake), \
         patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_issue", side_effect=_issue_post_fake), \
             patch("src.domains.runtime.vcs.api_client.VcsApiClient.find_open_issue_by_title", return_value=None):
        tid = client.post("/api/issues/tc_git/demo", headers=_headers,
                          json={"repo_key": "tc_git/demo", "title": "状态迁移任务", "description": "状态迁移用例描述"}).json()["task_id"]
    r = client.put(f"/api/issues/{tid}/status", headers=_headers, json={"status": "processing"})
    assert r.status_code == 200 and r.json()["status"] == "processing"
    r = client.put(f"/api/issues/{tid}/status", headers=_headers, json={"status": "completed"})
    assert r.status_code == 200 and r.json()["status"] == "completed"
    # 终态不可改
    r = client.put(f"/api/issues/{tid}/status", headers=_headers, json={"status": "active"})
    assert r.status_code == 400
    # 非法状态
    r = client.put(f"/api/issues/{tid}/status", headers=_headers, json={"status": "xxx"})
    assert r.status_code == 400
    # 404
    r = client.put("/api/issues/999999/status", headers=_headers, json={"status": "completed"})
    assert r.status_code == 404

def test_create_requires_fields():
    """缺 title / repo_key → 400。"""
    r = client.post("/api/issues/tc_git/demo", headers=_headers, json={"repo_key": "tc_git/demo", "title": "", "description": "建单描述"})
    assert r.status_code == 400
    r = client.post("/api/issues/tc_git/demo", headers=_headers, json={"repo_key": "", "title": "abc"})
    assert r.status_code == 400

from src.domains.mgmt.auth import grape_account_db
