"""托管/解除托管时自动管理 GitCode 项目维护者（全局 bot + repo bot）的测试"""
import os
import sys
from unittest.mock import patch, MagicMock

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import asyncio
from fastapi import HTTPException


def _run(coro):
    return asyncio.run(coro)


# ===================== repo_manager.add_collaborator =====================

def test_add_collaborator_success():
    from src.user.repo_manager import add_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.return_value = {}
        ok = add_collaborator("tok", "owner", "repo", "hwc-dev-bot", "maintainer")
        assert ok is True
        url = mock_req.call_args[0][1]
        assert "/repos/owner/repo/collaborators/hwc-dev-bot" in url
        body = mock_req.call_args[0][3]
        assert body.get("permissions") == "maintainer"


def test_add_collaborator_permission_fallback():
    """maintainer 失败时降级 push/developer（用 permissions 字段）"""
    from src.user.repo_manager import add_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("fail")
        ok = add_collaborator("tok", "owner", "repo", "bot", "maintainer")
        # 尝试 maintainer/push/developer 三种
        assert mock_req.call_count >= 3
        assert ok is False


def test_add_collaborator_already_member():
    """已是成员（400）视为幂等成功"""
    from src.user.repo_manager import add_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("用户已经是项目中的成员")
        ok = add_collaborator("tok", "owner", "repo", "bot", "maintainer")
        assert ok is True


def test_add_collaborator_already_invited():
    """已被邀请（400 用户已被邀请）视为幂等成功"""
    from src.user.repo_manager import add_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("用户已被邀请")
        ok = add_collaborator("tok", "owner", "repo", "bot", "maintainer")
        assert ok is True


def test_add_collaborator_no_username():
    from src.user.repo_manager import add_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        ok = add_collaborator("tok", "owner", "repo", "", "maintainer")
        assert ok is False
        mock_req.assert_not_called()


# ===================== repo_manager.remove_collaborator =====================

def test_remove_collaborator_success():
    from src.user.repo_manager import remove_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.return_value = {}
        ok = remove_collaborator("tok", "owner", "repo", "hwc-dev-bot")
        assert ok is True
        url = mock_req.call_args[0][1]
        assert "/repos/owner/repo/collaborators/hwc-dev-bot" in url
        assert mock_req.call_args[0][0] == "DELETE"


def test_remove_collaborator_404_ok():
    """用户本就不是协作者（404）视为成功"""
    from src.user.repo_manager import remove_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("GitCode API DELETE failed (404): not found")
        ok = remove_collaborator("tok", "owner", "repo", "bot")
        assert ok is True


def test_remove_collaborator_error_false():
    """其他错误返回 False"""
    from src.user.repo_manager import remove_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("GitCode API DELETE failed (500): server error")
        ok = remove_collaborator("tok", "owner", "repo", "bot")
        assert ok is False


# ===================== host_repo：添加全局 bot 失败拒绝托管 =====================

def test_host_repo_global_bot_fail_rejects():
    """全局 bot 添加协作者失败 → 拒绝托管（抛 502）"""
    import src.api.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"

    fake_user = {"id": 1, "username": "tester"}
    gbt_json = '{"gitcode_api_token": "gbt", "bot_usernames": ["hwc-dev-bot"]}'

    with patch.object(repos_mod, "get_user_token", return_value="user_tok"), \
         patch.object(repos_mod, "get_bot_token", return_value="bot_tok"), \
         patch("src.user.db.get_meta_value", return_value="http://webhook"), \
         patch("src.user.repo_manager._check_collaborator_admin", return_value=True), \
         patch("src.user.repo_manager._req", return_value={"login": "tester"}), \
         patch("src.user.repo_manager.add_collaborator", return_value=False) as mock_add, \
         patch("src.user.repo_manager.find_existing_webhook", return_value=None), \
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
    """全局 bot 添加协作者成功 → 托管成功"""
    import src.api.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"

    fake_user = {"id": 1, "username": "tester"}
    gbt_json = '{"gitcode_api_token": "gbt", "bot_usernames": ["hwc-dev-bot"]}'

    with patch.object(repos_mod, "get_user_token", return_value="user_tok"), \
         patch.object(repos_mod, "get_bot_token", return_value="bot_tok"), \
         patch("src.user.db.get_meta_value", return_value="http://webhook"), \
         patch("src.user.repo_manager._check_collaborator_admin", return_value=True), \
         patch("src.user.repo_manager._req", return_value={"login": "tester"}), \
         patch("src.user.repo_manager.add_collaborator", return_value=True), \
         patch("src.user.repo_manager.find_existing_webhook", return_value=None), \
         patch.object(repos_mod, "setup_webhook", return_value=1), \
         patch.object(repos_mod, "user_db") as mock_udb, \
         patch("src.api.session_store.ensure_default_agent_sessions"), \
         patch("builtins.open", MagicMock()) as mock_open:
        mock_f = MagicMock()
        mock_f.__enter__.return_value.read.return_value = gbt_json
        mock_open.return_value = mock_f
        r = _run(repos_mod.host_repo(FakeBody(), fake_user))
        assert "托管成功" in r["message"]
        mock_udb.add_managed_repo.assert_called_once()

# ===================== set_repo_bot_token：自动更新 bot_username + 添加 repo bot =====================

def test_set_repo_bot_token_updates_username():
    """配置 repo bot token 后，自动更新 bot_username 并添加维护者"""
    import src.api.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"
        bot_token = "repo_bot_tok"

    fake_user = {"id": 1, "username": "tester"}

    with patch.object(repos_mod, "update_managed_repo_bot_token", return_value=True) as mock_upd, \
         patch("src.user.repo_manager._req", return_value={"login": "repo-bot-login"}), \
         patch.object(repos_mod, "get_user_token", return_value="owner_tok"), \
         patch.object(repos_mod, "update_managed_repo_bot_username") as mock_upd_username, \
         patch("src.user.repo_manager.add_collaborator", return_value=True) as mock_add:
        _run(repos_mod.set_repo_bot_token(FakeBody(), fake_user))
        mock_upd.assert_called_once()
        mock_upd_username.assert_called_once()
        args = mock_add.call_args[0]
        # 用 owner token 添加 repo bot 为维护者
        assert args[0] == "owner_tok"
        assert args[3] == "repo-bot-login"


# ===================== remove_managed_repo：移除 bot 协作者 =====================

def test_remove_managed_repo_removes_collaborators():
    """解除托管时移除全局 bot + repo bot 协作者"""
    import src.api.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"

    fake_user = {"id": 1, "username": "tester"}

    def fake_managed(user_id):
        return [{"full_name": "owner/repo", "bot_gitcode_token": "repo_tok",
                 "bot_username": "repo-bot-login"}]

    gbt_json = '{"gitcode_api_token": "gbt", "bot_usernames": ["hwc-dev-bot"]}'

    with patch.object(repos_mod, "get_bot_token", return_value="bot_tok"), \
         patch.object(repos_mod, "get_user_token", return_value="user_tok"), \
         patch("src.user.repo_manager._get_managed_webhook", return_value=None), \
         patch("src.user.repo_manager.remove_collaborator") as mock_rm, \
         patch.object(repos_mod, "user_db") as mock_udb, \
         patch("src.api.session_store.remove_agent_sessions"), \
         patch("builtins.open", MagicMock()) as mock_open:
        mock_udb.get_managed_repos.side_effect = fake_managed
        mock_udb.remove_managed_repo.return_value = {"full_name": "owner/repo", "webhook_id": None}
        mock_f = MagicMock()
        mock_f.__enter__.return_value.read.return_value = gbt_json
        mock_open.return_value = mock_f
        _run(repos_mod.remove_managed_repo(FakeBody(), fake_user))
        assert mock_rm.call_count == 2
        usernames = [c.args[3] for c in mock_rm.call_args_list]
        assert "hwc-dev-bot" in usernames
        assert "repo-bot-login" in usernames
