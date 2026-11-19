"""S70 用例：统一建单入口用户 token 派生（D37/S57 漏点收口）。

实弹发现（2026-11-04 gitee live-fire）：create_task_via_issue 虽已按
repo_key 解析 _provider（D35），但 JWT 用户 token 解析 else 分支仍调用
get_user_token_with_refresh（gitcode 专用，无 provider 参数）——gitee 仓 +
已绑 gitee PAT 的用户建单必 400「GitCode Token 未配置」。

先测后改纪律：
- RED：gitee 仓 + 仅绑 gitee 用户 token → 当前走 gitcode 链取空 → 400。
- GREEN（修复后）：token 按 repo_key 前缀派生（get_user_token_for_repo），
  查重/建单拿到的 token = gitee 用户 token。
- 特征锁：gitcode 仓存量行为不变（同函数、裸 key 走 gitcode 链）。
"""
import asyncio
from unittest.mock import patch

import pytest

from src.kernel.session_manager import db, db_lock


REPO_KEY = "gitee:acme/web"
GC_REPO_KEY = "gitcode:acme/web"


@pytest.fixture(autouse=True)
def _init_db():
    from src.api.db_setup import init_database
    init_database()
    yield


@pytest.fixture()
def _no_preauth_user():
    """JWT 登录用户（非 api_key/internal、无注入 token）。"""
    return {"id": 701, "username": "s70_user", "auth_type": "", "token": ""}


def _identity_token_stub(gitee_token="tok-gitee-user", gitcode_token="tok-gitcode-user"):
    """external_identities 读取桩：按 provider 分流（gitee 有、gitcode 可空）。"""
    def _fake(username, provider):
        if provider == "gitee":
            return gitee_token
        if provider == "gitcode":
            return gitcode_token
        return ""
    return _fake


def _cleanup_issues():
    with db_lock:
        db.cursor().execute("DELETE FROM issue WHERE repo_key IN (?, ?)", (REPO_KEY, GC_REPO_KEY))
        db.commit()


def test_s70_gitee_user_token_derived(_no_preauth_user):
    """RED→GREEN：gitee 仓建单，用户 token 按 provider 派生（非 gitcode 链）。"""
    from src.api.issues_center import TaskCreateBody, create_task_via_issue
    _cleanup_issues()
    body = TaskCreateBody(repo_key=REPO_KEY, title="S70 gitee user token",
                          description="s70", assignee="", scene_id="",
                          is_subtask_mode=0)
    seen = {}

    def _fake_find(api_token, owner, repo, title, provider="", **kwargs):  # #3349: accept timeout kwarg
        seen["find_token"] = api_token
        seen["find_provider"] = provider
        return None  # 无同名 → 走建单

    def _fake_create(self, title, body="", assignee=""):
        seen["create_token"] = self._token if hasattr(self, "_token") else None
        return {"number": 701}

    with patch("src.api.deps._get_oauth_token_from_identity",
               side_effect=_identity_token_stub()), \
         patch("src.api.issues_center._find_open_issue_by_title", side_effect=_fake_find), \
         patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_issue", _fake_create), \
         patch("src.api.issues_center.ensure_issue_task", return_value=701):
        result = asyncio.run(create_task_via_issue(body, _no_preauth_user))
    assert result.get("ok") is True, f"create failed: {result}"
    # 修复后：token 来自 gitee 身份（非空即派生成功；当前实现取 gitcode 空串 → 400）
    assert seen.get("find_token") == "tok-gitee-user", (
        f"token should be derived from gitee identity, got {seen.get('find_token')!r}")
    assert seen.get("find_provider") == "gitee"


def test_s70_gitee_str_ident_preserved(_no_preauth_user):
    """RED→GREEN：gitee 字符串 issue ident（如 'IKSZ3I'）建单不 int() 崩、保原生形态。"""
    from src.api.issues_center import TaskCreateBody, create_task_via_issue
    _cleanup_issues()
    body = TaskCreateBody(repo_key=REPO_KEY, title="S70 gitee str ident",
                          description="s70", assignee="", scene_id="",
                          is_subtask_mode=0)
    captured = {}

    def _fake_create(self, title, body="", assignee=""):
        return {"number": "IKSZ3I"}  # gitee 实弹观测：字符串 ident

    def _fake_ensure(repo_key, issue_num, title, description="", **kw):
        captured["ensure_issue_num"] = issue_num
        return 700

    with patch("src.api.deps._get_oauth_token_from_identity",
               side_effect=_identity_token_stub()), \
         patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
         patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_issue", _fake_create), \
         patch("src.api.issues_center.ensure_issue_task", side_effect=_fake_ensure):
        result = asyncio.run(create_task_via_issue(body, _no_preauth_user))
    assert result.get("ok") is True, f"create failed: {result}"
    assert result.get("issue_number") == "IKSZ3I"
    assert captured.get("ensure_issue_num") == "IKSZ3I"


def test_s70_gitcode_parity(_no_preauth_user):
    """特征锁：gitcode 仓（裸 key 同语义）仍走 gitcode 用户 token。"""
    from src.api.issues_center import TaskCreateBody, create_task_via_issue
    _cleanup_issues()
    body = TaskCreateBody(repo_key=GC_REPO_KEY, title="S70 gitcode parity",
                          description="s70", assignee="", scene_id="",
                          is_subtask_mode=0)
    seen = {}

    def _fake_find(api_token, owner, repo, title, provider="", **kwargs):  # #3349: accept timeout kwarg
        seen["find_token"] = api_token
        return None

    def _fake_create(self, title, body="", assignee=""):
        return {"number": 702}

    with patch("src.api.deps._get_oauth_token_from_identity",
               side_effect=_identity_token_stub()), \
         patch("src.api.issues_center._find_open_issue_by_title", side_effect=_fake_find), \
         patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_issue", _fake_create), \
         patch("src.api.issues_center.ensure_issue_task", return_value=702):
        result = asyncio.run(create_task_via_issue(body, _no_preauth_user))
    assert result.get("ok") is True, f"create failed: {result}"
    assert seen.get("find_token") == "tok-gitcode-user"
