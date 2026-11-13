"""QA C1/C2 回归锁（2026-11-13 审查意见）。

C1：`_REPO_TOKEN_GETTERS` 原以 webhook_dialect 为键，而 `gitee_v5` 被 gitcode 与
gitee 共用 —— gitee 仓会读到 GitCode 的凭据（token 随 Basic 认证发往 gitee.com，
跨平台凭据错发）。登记表必须按 **provider** 键。

C2：`has_user_id` 在 base 就只有使用没有 import（既有缺陷），本批把它从 else
分支扩散到 _getters 主路径 —— `not username and has_user_id(uid)` 每次求值
NameError，被 `except Exception: return ""` 吞成空 token，用户凭据解析全线失效。
"""
import pytest

from src.domains.mgmt.routes import repos as repos_mod
from src.domains.mgmt.user import repo_manager as rm


def test_getters_are_provider_keyed_not_dialect():
    """gitee 与 gitcode 同方言，但不许共用读取器。"""
    assert rm.repo_token_getters("gitee") == {}, "gitee 不应登记存量读取器（应走 TokenResolver 端口）"
    assert rm.repo_token_getters("github").get("bot"), "github 应登记 bot 读取器"
    gc = rm.repo_token_getters("gitcode")
    assert gc.get("bot") and gc.get("use_provider_key") is False


def test_unknown_provider_has_no_getters():
    assert rm.repo_token_getters("gitlab") == {}


def _call_token(monkeypatch, provider: str, user: dict, token: str):
    """在隔离下调用 _repo_platform_token，并记录是否触碰 gitcode 专用读取器。"""
    import src.api.deps as deps

    touched = {"gitcode_bot": 0}

    def _gh_bot(**_kw):
        touched["gitcode_bot"] += 1
        return "gitcode-bot-token"

    monkeypatch.setattr(rm, "get_gitcode_token", _gh_bot, raising=False)
    # 端口分支（gitee/else）用函数内 from-import → 必须 patch deps 模块属性；
    # 同时也 patch repos 的模块级绑定，覆盖两种写法。
    monkeypatch.setattr(deps, "get_user_token_for", lambda *_a, **_k: token)
    monkeypatch.setattr(repos_mod, "get_user_token_for", lambda *_a, **_k: token, raising=False)
    # _getters 分支（github）：importlib 解析 deps 模块属性
    monkeypatch.setattr(deps, "get_user_github_token_with_refresh", lambda *_a, **_k: token)
    monkeypatch.setattr(deps, "get_user_token_with_refresh", lambda *_a, **_k: token)
    from src.kernel.token_resolver import TokenResolver
    monkeypatch.setattr(TokenResolver, "read_stored_token", lambda self, _rk: "")
    tok = repos_mod._repo_platform_token("yang-aiwu-new" if provider == "gitee" else "yangaiwu",
                                         "repo", user, provider)
    return tok, touched


@pytest.mark.parametrize("provider,username,user_id", [
    ("gitee", "yang-aiwu-new", "24"),
    ("github", "yangaiwu", "24"),
])
def test_git_credential_user_token_with_empty_username(monkeypatch, provider, username, user_id):
    """C2 + 会话 token 的 username 常态为空：用户 token 必须仍能解析（不再 NameError 吞空）。"""
    user = {"id": user_id, "username": "", "auth_type": "session_token", "agent_type": "chat"}
    from src.domains.mgmt.auth import grape_account_db
    monkeypatch.setattr(grape_account_db, "get_grape_account_by_id",
                        lambda _uid: {"username": username})
    tok, touched = _call_token(monkeypatch, provider, user, f"{provider}-user-token")
    assert tok == f"{provider}-user-token", f"{provider} 用户 token 解析失败（C2 回归）: {tok!r}"


def test_gitee_never_reads_gitcode_credential(monkeypatch):
    """C1：gitee 仓的 git 凭据不得来自 gitcode 读取器。"""
    user = {"id": "24", "username": "", "auth_type": "session_token", "agent_type": "devops"}
    tok, touched = _call_token(monkeypatch, "gitee", user, "gitee-user-token")
    assert touched["gitcode_bot"] == 0, "gitee 仓触达了 gitcode 专用读取器（跨平台凭据错发）"
    assert tok == "gitee-user-token", f"gitee 仓未拿到 gitee 凭据: {tok!r}"