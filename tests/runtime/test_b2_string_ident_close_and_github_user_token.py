"""B2 修复回归：① gitee 字符串 ident confirm-done VCS 自动关闭链；② github 建单用户 token 回落。

背景（V1 实测）：
- gitee issue_number 为字符串 ident（如 'IKUFPS'，INTEGER 亲和列存 text）。
  `_close_vcs_issues` 一律 int() 强转，字符串 ident 必炸走早退 → 主平台键
  （gitee）从未写入 issue_closed 结果，VCS 侧永不自动关闭。
- `set_issue_stage`/`_sync_issue_fields` 同族 int() 强转被 except 吞掉 →
  confirm-done 后 stage 恒 'vod'（字符串 ident 保存路径写丢）。
- create_task_via_issue github 分支只查 bot token，不回落 get_user_token_for_repo
  （gitcode/gitee 链有）——bot token 缺席 + 用户已绑 github PAT 仍 400。
"""
import asyncio
from unittest.mock import patch

import pytest

from src.kernel.session_manager import db, db_lock

REPO_KEY = "gitee:acme/b2fix"
GH_REPO_KEY = "github:example-user/demo"


@pytest.fixture(autouse=True)
def _init_db():
    from src.api.db_setup import init_database
    init_database()
    yield


@pytest.fixture()
def user():
    return {"id": 2401, "username": "b2_user", "auth_type": "", "token": ""}


def _identity_stub(gitee_token="tok-gitee-user", github_token="tok-gh-user"):
    def _fake(username, provider, *a, **kw):
        if provider == "gitee":
            return gitee_token
        if provider == "github":
            return github_token
        return ""
    return _fake


def _cleanup(*rks):
    with db_lock:
        db.cursor().execute(
            f"DELETE FROM issue WHERE repo_key IN ({','.join('?' * len(rks))})", rks)
        db.commit()


def test_b2_gitee_str_ident_confirm_done_closes_vcs(user):
    """gitee 仓建需求（字符串 ident）→ confirm-done → VCS issue 自动 closed。

    RED（修复前）：issue_closed 缺 gitee 主平台键（int('IKUFPS') 早退）；
    GREEN：主平台键在，close_issue 收到原生 ident，stage 落 done。
    """
    from src.api.issues_center import TaskCreateBody, create_task_via_issue
    _cleanup(REPO_KEY)
    body = TaskCreateBody(repo_key=REPO_KEY, title="B2 gitee ident close",
                          description="b2", assignee="", scene_id="",
                          is_subtask_mode=0)

    def _fake_create(self, title, body="", assignee=""):
        return {"number": "IKUFPS"}

    with patch("src.api.deps._get_oauth_token_from_identity",
               side_effect=_identity_stub()), \
         patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
         patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_issue", _fake_create):
        result = asyncio.run(create_task_via_issue(body, user))
    assert result.get("ok") is True, result
    assert result["issue_number"] == "IKUFPS"

    with db_lock:
        rows = db.cursor().execute(
            "SELECT id, issue_number, typeof(issue_number), status FROM issue "
            "WHERE repo_key=?", (REPO_KEY,)).fetchall()
    assert rows and rows[0][1] == "IKUFPS" and rows[0][2] == "text", rows

    # confirm-done：mock VCS 关闭链，断言 ident 原生透传 + 主平台键在
    closed_calls = []
    state_calls = []

    def _fake_get_state(self, n):
        state_calls.append(n)
        return "open"

    def _fake_close(self, n):
        closed_calls.append(n)
        return {"number": n, "state": "closed"}

    from src.domains.runtime.routes.issues import confirm_issue_done, _IssueConfirmBody
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient.get_issue_state", _fake_get_state), \
         patch("src.domains.runtime.vcs.api_client.VcsApiClient.close_issue", _fake_close), \
         patch("src.domains.mgmt.user.db.get_repo_bot_token", return_value="tok-bot-gitee"):
        resp = asyncio.run(confirm_issue_done(rows[0][0], _IssueConfirmBody(), user))
    assert resp.get("ok") is True, resp
    issue_closed = resp["issue_closed"]
    assert "gitee" in issue_closed, f"primary platform key missing: {issue_closed}"
    assert issue_closed["gitee"] == "success", issue_closed
    assert closed_calls == ["IKUFPS"], closed_calls
    assert state_calls == ["IKUFPS"], state_calls

    # 字符串 ident 保存路径：stage 落 done（修复前恒 'vod'）
    with db_lock:
        after = db.cursor().execute(
            "SELECT issue_number, typeof(issue_number), status, stage FROM issue "
            "WHERE repo_key=?", (REPO_KEY,)).fetchall()
    assert after == [("IKUFPS", "text", "completed", "done")], after


def test_b2_github_create_user_token_fallback(user):
    """github 建单：bot token 缺席 → 回落用户 github PAT（get_user_token_for_repo 派生）。"""
    from src.api.issues_center import TaskCreateBody, create_task_via_issue
    _cleanup(GH_REPO_KEY)
    body = TaskCreateBody(repo_key=GH_REPO_KEY, title="B2 github user token",
                          description="b2", assignee="", scene_id="",
                          is_subtask_mode=0)
    seen = {}

    def _fake_find(api_token, owner, repo, title, provider=""):
        seen["find_token"] = api_token
        return None

    def _fake_create(self, title, body="", assignee=""):
        seen["create_token"] = getattr(self, "_token", None) or seen.get("find_token")
        return {"number": 42}

    with patch("src.domains.mgmt.user.db.get_github_token", return_value=""), \
         patch("src.api.deps._get_oauth_token_from_identity",
               side_effect=_identity_stub()), \
         patch("src.api.issues_center._find_open_issue_by_title", side_effect=_fake_find), \
         patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_issue", _fake_create), \
         patch("src.api.issues_center.ensure_issue_task", return_value=42):
        result = asyncio.run(create_task_via_issue(body, user))
    assert result.get("ok") is True, f"create failed: {result}"
    assert seen.get("find_token") == "tok-gh-user", (
        f"should fall back to user github token, got {seen.get('find_token')!r}")


def test_b2_github_create_bot_token_still_preferred(user):
    """特征锁：bot token 在场时优先（存量行为不变），不回落用户 token。"""
    from src.api.issues_center import TaskCreateBody, create_task_via_issue
    _cleanup(GH_REPO_KEY)
    body = TaskCreateBody(repo_key=GH_REPO_KEY, title="B2 github bot preferred",
                          description="b2", assignee="", scene_id="",
                          is_subtask_mode=0)
    seen = {}

    def _fake_find(api_token, owner, repo, title, provider=""):
        seen["find_token"] = api_token
        return None

    with patch("src.domains.mgmt.user.db.get_github_token", return_value="tok-gh-bot"), \
         patch("src.api.issues_center._find_open_issue_by_title", side_effect=_fake_find), \
         patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_issue",
               lambda self, t, body="", assignee="": {"number": 43}), \
         patch("src.api.issues_center.ensure_issue_task", return_value=43):
        result = asyncio.run(create_task_via_issue(body, user))
    assert result.get("ok") is True, result
    assert seen.get("find_token") == "tok-gh-bot", seen


def test_b2_github_create_no_token_error(user):
    """两侧 token 均缺席 → 400 明确提示（不静默回落）。"""
    from src.api.issues_center import TaskCreateBody, create_task_via_issue
    from fastapi import HTTPException
    _cleanup(GH_REPO_KEY)
    body = TaskCreateBody(repo_key=GH_REPO_KEY, title="B2 github none",
                          description="b2", assignee="", scene_id="",
                          is_subtask_mode=0)
    with patch("src.domains.mgmt.user.db.get_github_token", return_value=""), \
         patch("src.api.deps._get_oauth_token_from_identity",
               side_effect=_identity_stub(gitee_token="", github_token="")):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(create_task_via_issue(body, user))
    assert ei.value.status_code == 400
