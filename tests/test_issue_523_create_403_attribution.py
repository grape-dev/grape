"""Issue #523 回归：建单 403 错误归因按上游正文语义分流。

覆盖四类：
1. 上游 403 正文 "The current operation is prohibited for model repository and
   dataset repository." → 仓库类型不支持 Issue 提示，不含「处理人无效」措辞；
2. 用户/权限类 403 正文（user not found / permission）→ 保留 assignee 提示；
3. 无法识别的 403 正文 → 通用「上游 403」提示并附脱敏正文，不猜测归因；
4. 正常代码仓建单（mock 200 返回 number）→ 成功路径不受影响。
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from unittest.mock import patch
from fastapi.testclient import TestClient
from src.api.main import app
from src.auth import grape_account_db

client = TestClient(app)

_uid = None
_headers = {}

OWNER = "test_523"
REPO = "hello523"
REPO_KEY = f"gitcode:{OWNER}/{REPO}"


def setup_module():
    global _uid, _headers
    _uid = grape_account_db.create_grape_account("test_523", email=None, password="test123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("test_523")
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    _token = create_access_token(_uid["id"], _uid["username"], get_token_version(_uid["id"]))
    _headers = {"Authorization": f"Bearer {_token}"}
    try:
        from src.api import db_setup
        db_setup.init_database()
    except Exception:
        pass


def teardown_module():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for t in ("events", "issue_state", "issue", "agent_tasks"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key=?", (REPO_KEY,))
            except Exception:
                pass
        db.commit()


def _post(body):
    return RuntimeError(f"GitCode API POST issues failed (403): {body}")


def _create(title):
    return client.post(f"/api/issues/{OWNER}/{REPO}",
                       json={"title": title, "description": "d", "repo_key": REPO_KEY,
                             "assignee": "someone"},
                       headers=_headers)


class TestCreate403Attribution:
    def test_403_repo_type_prohibited_not_assignee_blame(self):
        """GitCode Space/模型仓/数据集仓 403 → 仓库类型限制提示，不再误报 assignee 无效。"""
        with patch("src.api.deps.get_user_token_with_refresh", return_value="test_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
             patch("src.api.issues_center._post_gitcode_issue",
                   side_effect=_post("The current operation is prohibited "
                                     "for model repository and dataset repository.")):
            resp = _create("t_523_repotype")
        assert resp.status_code == 403, resp.text
        msg = resp.json()["detail"]["message"]
        assert "不支持创建 Issue" in msg, msg
        assert "普通代码仓" in msg, msg
        assert "处理人无效" not in msg, msg
        # 上游脱敏正文被保留回显
        assert "prohibited for model repository" in msg, msg

    def test_403_permission_body_keeps_assignee_hint(self):
        """用户/权限类 403 正文 → 保留 assignee 提示并附上游正文。"""
        with patch("src.api.deps.get_user_token_with_refresh", return_value="test_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
             patch("src.api.issues_center._post_gitcode_issue",
                   side_effect=_post("user someone not found or has no permission")):
            resp = _create("t_523_perm")
        assert resp.status_code == 403, resp.text
        msg = resp.json()["detail"]["message"]
        assert "处理人无效或无权限" in msg, msg
        assert "assignee=someone" in msg, msg
        assert "user someone not found" in msg, msg

    def test_403_unrecognized_body_falls_back_generic_with_sanitized_body(self):
        """无法识别的 403 正文 → 通用「上游 403」提示附脱敏正文，不猜测归因。"""
        with patch("src.api.deps.get_user_token_with_refresh", return_value="test_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
             patch("src.api.issues_center._post_gitcode_issue",
                   side_effect=_post("some totally unexpected upstream reason")):
            resp = _create("t_523_unknown")
        assert resp.status_code == 403, resp.text
        msg = resp.json()["detail"]["message"]
        assert "上游 403" in msg, msg
        assert "some totally unexpected upstream reason" in msg, msg
        assert "处理人无效" not in msg, msg

    def test_403_body_truncated_to_200_chars(self):
        """超长上游正文截断 ≤200 字符（脱敏后回显）。"""
        long_body = "x" * 500
        with patch("src.api.deps.get_user_token_with_refresh", return_value="test_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
             patch("src.api.issues_center._post_gitcode_issue",
                   side_effect=_post(long_body)):
            resp = _create("t_523_trunc")
        assert resp.status_code == 403, resp.text
        msg = resp.json()["detail"]["message"]
        assert "x" * 200 in msg, msg
        assert "x" * 201 not in msg, msg

    def test_403_secret_pattern_sanitized(self):
        """正文中 token 类敏感键值被脱敏，不回显。"""
        with patch("src.api.deps.get_user_token_with_refresh", return_value="test_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
             patch("src.api.issues_center._post_gitcode_issue",
                   side_effect=_post("access_token=ghp_secret123 whatever happened")):
            resp = _create("t_523_san")
        assert resp.status_code == 403, resp.text
        msg = resp.json()["detail"]["message"]
        assert "ghp_secret123" not in msg, msg
        assert "whatever happened" in msg, msg

    def test_create_ok_normal_repo_unaffected(self):
        """正常代码仓建单（mock 200 返回 number）→ 成功路径不受影响。"""
        with patch("src.api.deps.get_user_token_with_refresh", return_value="test_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
             patch("src.api.issues_center._post_gitcode_issue", return_value={"number": 523}), \
             patch("src.api.issues_center.ensure_issue_task", return_value=523):
            resp = _create("t_523_ok")
        assert resp.status_code == 200, resp.text
        assert resp.json()["issue_number"] == 523
