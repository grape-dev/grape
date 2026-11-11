"""托管/解除托管时自动管理 GitCode 项目维护者（全局 bot + repo bot）的测试"""

import os

import sys

from unittest.mock import patch, MagicMock

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "src")

import asyncio

from fastapi import HTTPException

def _run(coro):
    import inspect
    if inspect.iscoroutine(coro):
        return asyncio.run(coro)
    return coro  # sync function returns directly

def test_add_collaborator_success():
    """GitCode PUT body 用单数 permission 字段，maintainer 映射为 admin（=仓库维护者）"""
    from src.domains.mgmt.user.repo_manager import add_collaborator
    with patch("src.domains.mgmt.user.repo_manager._req") as mock_req:
        mock_req.return_value = {}
        ok = add_collaborator("tok", "owner", "repo", "hwc-dev-bot", "maintainer")
        assert ok[0] is True  # 现返回 (ok, err_detail)
        url = mock_req.call_args[0][1]
        assert "/repos/owner/repo/collaborators/hwc-dev-bot" in url
        body = mock_req.call_args[0][3]
        assert body.get("permission") == "admin"  # maintainer → admin
        assert "permissions" not in body

def test_add_collaborator_developer_maps_push():
    """developer → push（GitCode 枚举）"""
    from src.domains.mgmt.user.repo_manager import add_collaborator
    with patch("src.domains.mgmt.user.repo_manager._req") as mock_req:
        mock_req.return_value = {}
        ok = add_collaborator("tok", "owner", "repo", "bot", "developer")
        assert ok[0] is True  # 现返回 (ok, err_detail)
        assert mock_req.call_args[0][3].get("permission") == "push"

def test_add_collaborator_no_fallback():
    """maintainer 邀请失败时不再降级 developer，直接返回 False（由调用方拒绝托管）"""
    from src.domains.mgmt.user.repo_manager import add_collaborator
    with patch("src.domains.mgmt.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("fail")
        ok = add_collaborator("tok", "owner", "repo", "bot", "maintainer")
        assert mock_req.call_count == 1  # 只试一次，不降级
        assert ok[0] is False  # 现返回 (ok, err)

def test_add_collaborator_already_member():
    """已是成员（400）视为幂等成功"""
    from src.domains.mgmt.user.repo_manager import add_collaborator
    with patch("src.domains.mgmt.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("用户已经是项目中的成员")
        ok = add_collaborator("tok", "owner", "repo", "bot", "maintainer")
        assert ok[0] is True  # 现返回 (ok, err_detail)

def test_add_collaborator_already_invited():
    """已被邀请（400 用户已被邀请）视为幂等成功"""
    from src.domains.mgmt.user.repo_manager import add_collaborator
    with patch("src.domains.mgmt.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("用户已被邀请")
        ok = add_collaborator("tok", "owner", "repo", "bot", "maintainer")
        assert ok[0] is True  # 现返回 (ok, err_detail)

def test_add_collaborator_no_username():
    from src.domains.mgmt.user.repo_manager import add_collaborator
    with patch("src.domains.mgmt.user.repo_manager._req") as mock_req:
        ok = add_collaborator("tok", "owner", "repo", "", "maintainer")
        assert ok[0] is False  # 现返回 (ok, err_detail)
        mock_req.assert_not_called()

def test_remove_collaborator_success():
    from src.domains.mgmt.user.repo_manager import remove_collaborator
    with patch("src.domains.mgmt.user.repo_manager._req") as mock_req:
        mock_req.return_value = {}
        ok = remove_collaborator("tok", "owner", "repo", "hwc-dev-bot")
        assert ok is True
        url = mock_req.call_args[0][1]
        assert "/repos/owner/repo/collaborators/hwc-dev-bot" in url
        assert mock_req.call_args[0][0] == "DELETE"

def test_remove_collaborator_404_ok():
    """用户本就不是协作者（404）视为成功"""
    from src.domains.mgmt.user.repo_manager import remove_collaborator
    with patch("src.domains.mgmt.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("GitCode API DELETE failed (404): not found")
        ok = remove_collaborator("tok", "owner", "repo", "bot")
        assert ok is True

def test_remove_collaborator_error_false():
    """其他错误返回 False"""
    from src.domains.mgmt.user.repo_manager import remove_collaborator
    with patch("src.domains.mgmt.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("GitCode API DELETE failed (500): server error")
        ok = remove_collaborator("tok", "owner", "repo", "bot")
        assert ok is False

def test_list_user_invites():
    from src.domains.mgmt.user.repo_manager import list_user_invites, _req_query
    with patch("src.domains.mgmt.user.repo_manager._req_query") as mock_q:
        mock_q.return_value = {"content": [
            {"source_name": "owner/repo", "invitation_token": "tok1", "user_role": "开发者"}
        ]}
        invites = list_user_invites("bot_tok")
        assert len(invites) == 1
        assert invites[0]["invitation_token"] == "tok1"
        args = mock_q.call_args[0]
        assert args[0] == "GET"
        assert "/user/invite" in args[1]
        # token 作为第 3 个位置参数传入，走 _req_query（内部拼 query access_token）而非 Bearer
        assert args[2] == "bot_tok"
        assert args[3] == {"page": 1, "per_page": 100}

def test_accept_invite_for_repo():
    """按 owner/repo 匹配邀请并接受"""
    from src.domains.mgmt.user.repo_manager import accept_invite_for_repo
    with patch("src.domains.mgmt.user.repo_manager.list_user_invites", return_value=[
        {"source_name": "other/repo", "invitation_token": "x"},
        {"source_name": "owner/repo", "invitation_token": "match_tok"},
    ]), patch("src.domains.mgmt.user.repo_manager.accept_project_invite", return_value=True) as mock_acc:
        ok = accept_invite_for_repo("bot_tok", "owner", "repo")
        assert ok is True
        mock_acc.assert_called_once_with("bot_tok", "match_tok")

def test_accept_invite_for_repo_no_match():
    from src.domains.mgmt.user.repo_manager import accept_invite_for_repo
    with patch("src.domains.mgmt.user.repo_manager.list_user_invites", return_value=[
        {"source_name": "other/repo", "invitation_token": "x"},
    ]), patch("src.domains.mgmt.user.repo_manager.accept_project_invite") as mock_acc:
        ok = accept_invite_for_repo("bot_tok", "owner", "repo")
        assert ok is False
        mock_acc.assert_not_called()

def test_get_member_permission():
    from src.domains.mgmt.user.repo_manager import get_member_permission
    with patch("src.domains.mgmt.user.repo_manager._req") as mock_req:
        mock_req.return_value = {"access_level": 40, "role_name": "Maintainer", "permission": "admin"}
        data = get_member_permission("tok", "owner", "repo", "grape_bot")
        assert data["access_level"] == 40
        url = mock_req.call_args[0][1]
        assert "/collaborators/grape_bot/permission" in url

def test_host_repo_global_bot_fail_rejects():
    """全局 bot 添加协作者失败 → 拒绝托管（抛 502）"""
    import src.domains.mgmt.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"

    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}
    gbt_json = '{"gitcode_api_token": "gbt", "bot_usernames": ["hwc-dev-bot"]}'

    with patch.object(repos_mod, "get_user_token", return_value="user_tok"), \
         patch.object(repos_mod, "get_bot_token", return_value="bot_tok"), \
         patch("src.domains.mgmt.user.db.get_meta_value", return_value="http://webhook"), \
         patch("src.domains.mgmt.user.repo_manager._check_collaborator_admin", return_value=True), \
         patch("src.domains.mgmt.user.repo_manager._req", return_value={"login": "tester"}), \
         patch("src.domains.mgmt.user.repo_manager.add_collaborator", return_value=False) as mock_add, \
         patch("src.domains.mgmt.user.repo_manager.find_existing_webhook", return_value=None), \
         patch("builtins.open", MagicMock()) as mock_open:
        mock_f = MagicMock()
        mock_f.__enter__.return_value.read.return_value = gbt_json
        mock_open.return_value = mock_f
        try:
            _run(repos_mod.host_repo(FakeBody(), fake_user))
            assert False, "should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 502
            assert "全局 Bot" in e.detail["message"]
        mock_add.assert_called_once()

def test_host_repo_global_bot_success():
    """全局 bot 添加协作者成功 + 接受邀请 + 已是维护者 → 托管成功"""
    import src.domains.mgmt.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"

    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}
    import contextlib
    with contextlib.ExitStack() as st:
        for m in _host_repo_mocks():
            st.enter_context(m)
        st.enter_context(patch("src.domains.mgmt.user.repo_manager.add_collaborator", return_value=(True, "")))
        mock_acc = st.enter_context(patch("src.domains.mgmt.user.repo_manager.accept_invite_for_repo", return_value=True))
        mock_perm = st.enter_context(patch("src.domains.mgmt.user.repo_manager.get_member_permission",
                                           return_value={"access_level": 40, "role_name": "Maintainer"}))
        mock_setup = st.enter_context(patch("src.domains.mgmt.user.repo_manager.setup_webhook", return_value=1))
        mock_udb = st.enter_context(patch.object(repos_mod, "user_db"))
        r = _run(repos_mod.host_repo(FakeBody(), fake_user, provider="gitcode"))
        assert "托管成功" in r["message"]
        mock_udb.add_managed_repo.assert_called_once()
        mock_acc.assert_called_once()
        mock_perm.assert_called_once()
        assert mock_setup.call_args.args[0] == "gbt"  # 路径 A：全局 bot token

def test_host_repo_global_bot_not_maintainer_rejects():
    """bot 已加入但非维护者（access_level<40）→ 拒绝托管"""
    import src.domains.mgmt.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"

    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}
    gbt_json = '{"gitcode_api_token": "gbt", "bot_usernames": ["hwc-dev-bot"]}'

    with patch.object(repos_mod, "get_user_token", return_value="user_tok"), \
         patch.object(repos_mod, "get_bot_token", return_value="bot_tok"), \
         patch("src.domains.mgmt.user.db.get_meta_value", return_value="http://webhook"), \
         patch("src.domains.mgmt.user.repo_manager._check_collaborator_admin", return_value=True), \
         patch("src.domains.mgmt.user.repo_manager._req", return_value={"login": "tester"}), \
         patch("src.domains.mgmt.user.repo_manager.add_collaborator", return_value=True), \
         patch("src.domains.mgmt.user.repo_manager.accept_invite_for_repo", return_value=True), \
         patch("src.domains.mgmt.user.repo_manager.get_member_permission", return_value={"access_level": 30, "role_name": "Developer"}), \
         patch("src.domains.mgmt.user.repo_manager.find_existing_webhook", return_value=None), \
         patch.object(repos_mod, "setup_webhook", return_value=1), \
         patch.object(repos_mod, "user_db") as mock_udb, \
         patch("src.api.session_store.ensure_default_agent_sessions"), \
         patch("builtins.open", MagicMock()) as mock_open:
        mock_f = MagicMock()
        mock_f.__enter__.return_value.read.return_value = gbt_json
        mock_open.return_value = mock_f
        try:
            _run(repos_mod.host_repo(FakeBody(), fake_user))
            assert False, "should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 502
            assert "维护者" in e.detail["message"]
        mock_udb.add_managed_repo.assert_not_called()

def test_set_repo_bot_token_updates_username():
    """R15: 配置 repo bot token 后，验证 token → 获取 username → 邀 bot → 重建 webhook → 写 DB"""
    import src.domains.mgmt.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"
        bot_token = "repo_bot_tok"

    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}

    # R15: mock DB（仓库存在 + 旧 bot_username 查询）+ VCSProvider + 邀请
    _fake_cursor = MagicMock()
    _fake_cursor.fetchone.side_effect = [
        (1,),            # 步骤 1: 仓库存在检查
        (None,),         # 步骤 4: 旧 bot_username 查询（无旧 bot）
    ]

    with patch.object(repos_mod, "update_managed_repo_bot_token", return_value={"ok": True, "warning": "", "related_repos": []}) as mock_upd, \
         patch.object(repos_mod, "update_managed_repo_bot_username") as mock_upd_username, \
         patch("src.kernel.session_manager.db") as mock_db, \
         patch("src.kernel.session_manager.db_lock"), \
         patch("src.kernel.token_resolver.GitCodeProvider.verify_token", return_value={"login": "repo-bot-login"}), \
         patch("src.kernel.token_resolver.GitCodeProvider.add_collaborator", return_value=True) as mock_add, \
         patch("src.domains.mgmt.user.db.get_host_user_id", return_value=0), \
         patch("src.api.deps.get_user_token_for", return_value="owner_tok"), \
         patch("src.api.deps.check_repo_access"):
        mock_db.cursor.return_value = _fake_cursor
        _run(repos_mod.set_repo_bot_token(FakeBody(), fake_user))
        mock_upd.assert_called_once()
        mock_upd_username.assert_called_once()
        # R15 步骤 4: 邀 bot 为协作者
        args = mock_add.call_args[0]
        # add_collaborator(api_token, owner, repo, username, permission)
        assert args[0] == "owner_tok"  # 用 owner token
        assert args[3] == "repo-bot-login"  # 邀请 bot username

def test_set_repo_bot_token_invalid_token_returns_400():
    """R15.1.2: token 无效时返回 400 + TOKEN_INVALID，不写 DB"""
    import src.domains.mgmt.routes.repos as repos_mod
    from fastapi import HTTPException

    class FakeBody:
        full_name = "owner/repo"
        bot_token = "invalid_tok"

    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}

    _fake_cursor = MagicMock()
    _fake_cursor.fetchone.return_value = (1,)  # 仓库存在

    with patch("src.kernel.session_manager.db") as mock_db, \
         patch("src.kernel.session_manager.db_lock"), \
         patch("src.kernel.token_resolver.GitCodeProvider.verify_token", side_effect=RuntimeError("401")), \
         patch("src.domains.mgmt.user.db.get_host_user_id", return_value=0), \
         patch("src.api.deps.check_repo_access"), \
         patch.object(repos_mod, "update_managed_repo_bot_token") as mock_upd:
        mock_db.cursor.return_value = _fake_cursor
        try:
            _run(repos_mod.set_repo_bot_token(FakeBody(), fake_user))
            assert False, "Should raise HTTPException 400"
        except HTTPException as e:
            assert e.status_code == 400
            assert e.detail.get("error_code") == "TOKEN_INVALID"
        mock_upd.assert_not_called()  # 不写 DB

def test_set_repo_bot_token_clear_empty():
    """R15.5: 传空串清除仓库级 bot token + 清除 bot_token_status"""
    import src.domains.mgmt.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"
        bot_token = ""

    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}

    _fake_cursor = MagicMock()
    _fake_cursor.rowcount = 1  # UPDATE 成功

    with patch("src.kernel.session_manager.db") as mock_db, \
         patch("src.kernel.session_manager.db_lock"), \
         patch("src.api.deps.check_repo_access"), \
         patch.object(repos_mod, "_remove_old_bot_collaborator"):
        mock_db.cursor.return_value = _fake_cursor
        result = _run(repos_mod.set_repo_bot_token(FakeBody(), fake_user))
        assert result["message"] == "Bot Token 已清除"
        assert result["bot_username"] == ""
        # 验证 UPDATE SQL 包含 bot_token_status=''
        _sql = _fake_cursor.execute.call_args[0][0]
        assert "bot_token_status=''" in _sql

def test_remove_managed_repo_removes_collaborators():
    """解除托管时移除全局 bot + repo bot 协作者"""
    import src.domains.mgmt.routes.repos as repos_mod
    from src.kernel.session_manager import db as _sdb, db_lock as _sdl

    class FakeBody:
        full_name = "owner/repo"

    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}

    # D42/S64：repo_key 存在性检测走真实库（INSERT 测试行）
    with _sdl:
        _sdb.cursor().execute("DELETE FROM managed_repos WHERE repo_key='gitcode:owner/repo'")
        _sdb.cursor().execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, bot_gitcode_token, bot_username) "
            "VALUES ('gitcode:owner/repo', 'owner', 'repo', 'owner/repo', 'repo_tok', 'repo-bot-login')")
        _sdb.commit()
    try:
        def fake_managed(user_id):
            return [{"repo_key": "gitcode:owner/repo", "full_name": "owner/repo",
                     "bot_gitcode_token": "repo_tok", "bot_username": "repo-bot-login"}]

        from src.kernel.token_resolver import GitCodeProvider
        with patch.object(GitCodeProvider, "get_user_login", return_value="owner"), \
                patch("src.api.deps.get_user_token_for", return_value="user_tok"), \
                patch.object(repos_mod, "get_bot_token", return_value="bot_tok"), \
                patch.object(repos_mod, "with_oauth_token",
                             side_effect=lambda u, p, fn, *a, **k: fn("user_tok")), \
                patch("src.domains.mgmt.user.repo_manager._get_managed_webhook", return_value=None), \
                patch("src.domains.mgmt.user.repo_manager.resolve_bot_username", return_value="hwc-dev-bot"), \
                patch("src.domains.mgmt.user.repo_manager.remove_collaborator") as mock_rm, \
                patch("src.domains.mgmt.user.db.get_host_user_id", return_value=0), \
                patch.object(repos_mod, "user_db") as mock_udb, \
                patch("src.kernel.repo_cleanup.cascade_cleanup_repo_data", return_value={}), \
                patch("src.api.session_store.remove_agent_sessions"):
            mock_udb.get_managed_repos.side_effect = fake_managed
            mock_udb.remove_managed_repo.return_value = {"full_name": "owner/repo", "webhook_id": None}
            _run(repos_mod.remove_managed_repo(FakeBody(), fake_user))
            assert mock_rm.call_count == 2
            usernames = [c.args[3] for c in mock_rm.call_args_list]
            assert "hwc-dev-bot" in usernames
    finally:
        with _sdl:
            _sdb.cursor().execute("DELETE FROM managed_repos WHERE repo_key='gitcode:owner/repo'")
            _sdb.commit()


def test_fetch_repos_uses_permission_field_no_nplus1():
    """列表响应含 permission 字段时直接判定 is_admin，不再对每个非 owner 仓库调 collaborator API"""
    from src.domains.mgmt.user.repo_manager import fetch_repos_with_role

    repos_payload = [
        {"full_name": "me/own-repo", "name": "own-repo",
         "owner": {"login": "me"}, "permission": {"pull": True, "push": True, "admin": True}},
        {"full_name": "org/repo-admin", "name": "repo-admin",
         "owner": {"login": "org"}, "permission": {"pull": True, "push": True, "admin": True}},
        {"full_name": "org/repo-member", "name": "repo-member",
         "owner": {"login": "org"}, "permission": {"pull": True, "push": False, "admin": False}},
    ]

    calls = []
    def fake_req(method, url, token="", body=None):
        calls.append(url)
        if url.endswith("/user"):
            return {"login": "me"}
        if "/user/repos" in url:
            return repos_payload
        return {}

    with patch("src.domains.mgmt.user.repo_manager._req", side_effect=fake_req) as mock_req:
        result = fetch_repos_with_role("tok")

    # 只应有 2 次请求：/user + /user/repos（无 collaborator permission N+1）
    assert len(calls) == 2, f"应只有 2 次 GitCode 请求，实际 {len(calls)}: {calls}"
    assert not any("collaborators" in u for u in calls), "不应调用 collaborator API"

    by_name = {r["full_name"]: r["is_admin"] for r in result}
    assert by_name["me/own-repo"] is True        # owner -> admin
    assert by_name["org/repo-admin"] is True     # permission.admin -> admin
    assert by_name["org/repo-member"] is False   # permission.admin=false -> member

def _host_repo_mocks():
    from src.kernel.token_resolver import GitCodeProvider
    return [
        patch.object(repos_mod_import(), "get_user_token_for", return_value="user_tok"),
        patch.object(repos_mod_import(), "with_oauth_token",
                     side_effect=lambda u, p, fn, *a, **k: fn("user_tok")),
        patch.object(GitCodeProvider, "get_user_login", return_value="owner"),
        patch("src.domains.mgmt.user.db.get_webhook_url", return_value="http://wh/webhook/gitcode"),
        patch("src.domains.mgmt.user.db.get_global_bot_token_for", return_value="gbt"),
        patch("src.domains.mgmt.user.repo_manager.resolve_bot_username", return_value="hwc-dev-bot"),
        patch("src.domains.mgmt.user.repo_manager.find_existing_webhook", return_value=None),
        patch("src.domains.mgmt.user.db.set_host_status"),
        patch("src.api.session_store.ensure_default_agent_sessions"),
        patch("src.api.onboarding.cleanup_stale_repo_data"),
    ]

def repos_mod_import():
    import src.domains.mgmt.routes.repos as _rm
    return _rm

def test_host_repo_global_bot_not_maintainer_falls_back_path_b():
    """bot 非维护者（access_level<40）→ §16.1 降级路径 B 托管成功（旧"拒绝"语义已升级）"""
    import src.domains.mgmt.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"

    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}
    import contextlib
    with contextlib.ExitStack() as st:
        for m in _host_repo_mocks():
            st.enter_context(m)
        st.enter_context(patch("src.domains.mgmt.user.repo_manager.add_collaborator", return_value=(False, "无权限")))
        st.enter_context(patch("src.domains.mgmt.user.repo_manager.get_member_permission",
                               return_value={"access_level": 30, "role_name": "Developer"}))
        mock_setup = st.enter_context(patch("src.domains.mgmt.user.repo_manager.setup_webhook", return_value=2))
        mock_udb = st.enter_context(patch.object(repos_mod, "user_db"))
        r = _run(repos_mod.host_repo(FakeBody(), fake_user, provider="gitcode"))
        assert "托管成功" in r["message"]  # 降级路径 B 仍托管成功
        kw = mock_udb.add_managed_repo.call_args.kwargs
        assert kw.get("bot_gitcode_token") == ""  # 路径 B 不写 bot token
        assert kw.get("webhook_secret")  # 随机密钥
        assert mock_setup.call_args.args[0] == "user_tok"  # 路径 B 用用户 token

def test_host_repo_global_bot_fail_falls_back_path_b():
    """add_collaborator 失败 + 权限查询失败 → §16.1 降级路径 B（旧 502 语义已升级）"""
    import src.domains.mgmt.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"

    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}
    import contextlib
    with contextlib.ExitStack() as st:
        for m in _host_repo_mocks():
            st.enter_context(m)
        mock_add = st.enter_context(patch("src.domains.mgmt.user.repo_manager.add_collaborator",
                                          return_value=(False, "邀请失败")))
        st.enter_context(patch("src.domains.mgmt.user.repo_manager.get_member_permission",
                               side_effect=RuntimeError("perm check failed")))
        st.enter_context(patch("src.domains.mgmt.user.repo_manager.setup_webhook", return_value=3))
        mock_udb = st.enter_context(patch.object(repos_mod, "user_db"))
        r = _run(repos_mod.host_repo(FakeBody(), fake_user, provider="gitcode"))
        assert "托管成功" in r["message"]
        mock_add.assert_called_once()
        kw = mock_udb.add_managed_repo.call_args.kwargs
        assert kw.get("host_user_id") == 1  # 路径 B
