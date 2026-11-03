"""Issue #649 回归：创建 Issue 时 assignee 不再默认填创建者 + 成员资格预校验 + 上游 403 非成员归类。

覆盖四个验收场景：
1. 非成员不传 assignee → 创建成功，上游创建请求不带 assignee（留空，由仓库方分派）；
2. 非成员/非协作者显式传 assignee → 预校验 404 → 403「非成员」语义错误（含 trace_id），
   不调用上游 create，主文案不出现「用户名无效」误导措辞；
3. 成员 assignee（预校验 access_level>=20）→ 预校验放行，上游正常创建（成员场景不回退）；
4. 预校验无法判定（unknown）→ 放行由上游裁决，上游 403 权限类 → 非成员语义 + trace_id +
   上游脱敏原文（upstream_detail 字段）。
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

OWNER = "test_649"
REPO = "hello649"
REPO_KEY = f"gitcode:{OWNER}/{REPO}"


def setup_module():
    global _uid, _headers
    _uid = grape_account_db.create_grape_account("test_649", email=None, password="test123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("test_649")
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


def _create(title, assignee=None):
    payload = {"title": title, "description": "d", "repo_key": REPO_KEY}
    if assignee is not None:
        payload["assignee"] = assignee
    return client.post(f"/api/issues/{OWNER}/{REPO}", json=payload, headers=_headers)


class TestCreateIssueAssignee649:
    def test_no_assignee_creates_with_empty_assignee(self):
        """不传 assignee → 创建成功且上游请求 assignee 为空（不默认填创建者）。"""
        with patch("src.api.deps.get_user_token_with_refresh", return_value="test_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
             patch("src.api.issues_center._post_gitcode_issue", return_value={"number": 649}) as _mock_post, \
             patch("src.api.issues_center.ensure_issue_task", return_value=649):
            resp = _create("t_649_no_assignee")
        assert resp.status_code == 200, resp.text
        assert resp.json()["issue_number"] == 649
        assert resp.json()["assignee"] == "", resp.text
        # 上游创建请求不带 assignee（不再默认填创建者）
        _mock_post.assert_called_once()
        assert _mock_post.call_args.kwargs.get("assignee", "") == "", _mock_post.call_args

    def test_no_assignee_skips_member_precheck(self):
        """assignee 为空时不触发成员预校验（无谓上游调用）。"""
        with patch("src.api.deps.get_user_token_with_refresh", return_value="test_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
             patch("src.api.issues_center._gitcode_assignee_member_check",
                   return_value="member") as _mock_chk, \
             patch("src.api.issues_center._post_gitcode_issue", return_value={"number": 649}), \
             patch("src.api.issues_center.ensure_issue_task", return_value=649):
            resp = _create("t_649_skip_check")
        assert resp.status_code == 200, resp.text
        _mock_chk.assert_not_called()

    def test_explicit_assignee_not_member_blocked_before_upstream(self):
        """显式 assignee 预校验非成员 → 403「非成员」语义错误，含 trace_id，不调用上游。"""
        with patch("src.api.deps.get_user_token_with_refresh", return_value="test_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
             patch("src.api.issues_center._gitcode_assignee_member_check",
                   return_value="not_member"), \
             patch("src.api.issues_center._post_gitcode_issue", return_value={"number": 1}) as _mock_post:
            resp = _create("t_649_not_member", assignee="stranger")
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        msg = detail["message"]
        assert "不是仓库" in msg, msg
        assert "stranger" in msg, msg
        assert "成员" in msg, msg
        assert "用户名" not in msg, msg  # 不再误报 assignee 用户名无效
        assert detail.get("trace_id", "").startswith("tr_"), detail
        # 预校验拦截 → 不发起上游创建
        _mock_post.assert_not_called()

    def test_explicit_assignee_member_passes_precheck(self):
        """成员 assignee（预校验 member）→ 预校验放行，上游正常带 assignee 创建。"""
        with patch("src.api.deps.get_user_token_with_refresh", return_value="test_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
             patch("src.api.issues_center._gitcode_assignee_member_check",
                   return_value="member"), \
             patch("src.api.issues_center._post_gitcode_issue", return_value={"number": 650}) as _mock_post, \
             patch("src.api.issues_center.ensure_issue_task", return_value=650):
            resp = _create("t_649_member", assignee="maintainer")
        assert resp.status_code == 200, resp.text
        assert resp.json()["assignee"] == "maintainer", resp.text
        assert _mock_post.call_args.kwargs.get("assignee") == "maintainer", _mock_post.call_args

    def test_precheck_unknown_upstream_403_nonmember_semantics(self):
        """预校验无法判定（unknown）→ 放行；上游 403 权限类 → 非成员语义 + trace_id + 上游原文。"""
        with patch("src.api.deps.get_user_token_with_refresh", return_value="test_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
             patch("src.api.issues_center._gitcode_assignee_member_check",
                   return_value="unknown"), \
             patch("src.api.issues_center._post_gitcode_issue",
                   side_effect=_post("user stranger has no permission to assign")):
            resp = _create("t_649_up_403", assignee="stranger")
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        msg = detail["message"]
        assert "无权指派" in msg, msg
        assert "成员" in msg, msg
        assert "用户名" not in msg, msg  # 主文案不再误导为「assignee 用户名无效」
        assert detail.get("trace_id", "").startswith("tr_"), detail
        assert "user stranger has no permission" in detail.get("upstream_detail", ""), detail
