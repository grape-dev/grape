"""D44 批次 2（S66）用例：展示/IM/读侧族 11 处收口。

收口清单（均为"分派写法"判据，非平台名分支）：
- scenes 分支端口 ×2：list_repo_branches / create_repo_branch 三路
  （github 手拼 / gitcode 存量 / config-only）合一为 VcsApiClient
  命令层（create_branch 卡能力位 branch_create_form + 方言模板）；
- templates ×4：_load_global_bot_config 来源链、README 读侧方言、
  显式平台 token 走 D3 统一入口、建仓+login 走适配器端口；
- feishu_async ×2：协作者拉取走 list_collaborators 端口、um 列写入
  走 um_column_for_provider 白名单（gitee 不再污染 gitcode_user）；
- repo_roles ×2：_get_platform_login 统一身份解析、_is_admin_permission
  形态归一判定（dict/str/None）；
- outbound_ledger ×1：extract_event_content_id 按 payload 形态派生
  （行为派生，provider 参数不参与分支）。
"""
import json
import urllib.parse
from unittest.mock import patch

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import build_platform_card, invalidate_platform_card_cache
from src.domains.runtime.vcs.api_client import VcsApiClient
from src.kernel.token_resolver import create_provider, GenericVcsProvider


def _client(provider, token="T", owner="acme", repo="web"):
    return VcsApiClient(provider, token, owner, repo)


# ------------------------------------------------ S66 卡能力位 branch_create_form

def test_s66_card_branch_create_form_default_and_validation():
    card = build_platform_card("foo", {"host": "foo.com", "dialect": "github"})
    assert card.branch_create_form == "form"
    with pytest.raises(Exception):
        build_platform_card("foo", {"host": "foo.com", "dialect": "github",
                                    "branch_create_form": "bogus"})
    assert build_platform_card("foo", {"host": "foo.com", "dialect": "github",
                                       "branch_create_form": "refs_sha"}).branch_create_form == "refs_sha"


def test_s66_builtin_cards_branch_create_form():
    # 存量回归锁：github 内置卡=refs_sha（存量两步建 ref），gitcode=form（v5 存量）
    from src.domains.runtime.vcs.platform_card import get_platform_card
    assert get_platform_card("github").branch_create_form == "refs_sha"
    assert get_platform_card("gitcode").branch_create_form == "form"


# ------------------------------------------------ S66 create_branch 方言模板

def test_s66_create_branch_gitcode_form_parity():
    """gitcode（form 默认）：POST /repos/{o}/{r}/branches + form {branch_name, refs}。"""
    c = _client("gitcode")
    with patch.object(VcsApiClient, "_do_request",
                      side_effect=lambda m, u, d=None, headers=None: {"name": "feat/x"}) as m_req:
        r = c.create_branch("feat/x", refs="main")
    assert r["name"] == "feat/x"
    url = m_req.call_args.args[0].split("?")[0]
    assert url == "https://gitcode.com/api/v5/repos/acme/web/branches"
    body = m_req.call_args.args[2]
    assert isinstance(body, str)
    form = dict(urllib.parse.parse_qsl(body))
    assert form == {"branch_name": "feat/x", "refs": "main"}


def test_s66_create_branch_github_refs_sha_parity():
    """github（refs_sha 能力位）：查 base 分支 sha → POST /git/refs（存量两步形态）。"""
    c = _client("github")
    calls = []

    def _fake(url, method, data=None, headers=None):
        # _do_request 位置参数序：(url, method, data, headers)
        calls.append((method, url, data))
        if url.endswith("/branches/main"):
            return {"commit": {"sha": "abc123"}}
        if method == "POST":
            return {}
        return {}

    with patch.object(VcsApiClient, "_do_request", side_effect=_fake):
        r = c.create_branch("feat/y", refs="main")
    assert "error" not in r
    post = [c_ for c_ in calls if c_[0] == "POST"]
    assert len(post) == 1
    assert post[0][1].endswith("/repos/acme/web/git/refs")
    assert post[0][2] == {"ref": "refs/heads/feat/y", "sha": "abc123"}


def test_s66_create_branch_github_default_branch_fallback():
    """refs=main 查不到 sha → 回退默认分支（存量语义）。"""
    c = _client("github")

    def _fake(url, method, data=None, headers=None):
        if url.endswith("/branches/main"):
            return {"error": "HTTP 404"}
        if url.endswith("/repos/acme/web"):
            return {"default_branch": "master"}
        if url.endswith("/branches/master"):
            return {"commit": {"sha": "def456"}}
        return {}

    with patch.object(VcsApiClient, "_do_request", side_effect=_fake):
        r = c.create_branch("feat/z", refs="main")
    assert "error" not in r


def test_s66_create_branch_gitlab_path():
    """gitlab 方言：POST /projects/{o}%2F{r}/repository/branches。"""
    c = _client("gitlab")
    with patch.object(VcsApiClient, "_do_request",
                      side_effect=lambda m, u, d=None, headers=None: {"name": "b"}) as m_req:
        c.create_branch("b", refs="main")
    url = m_req.call_args.args[0].split("?")[0]
    assert url == "https://gitlab.com/api/v4/projects/acme%2Fweb/repository/branches"


# ------------------------------------------------ S66 get_readme 读侧方言

def test_s66_get_readme_github_endpoint():
    c = _client("github")
    import base64
    content = base64.b64encode("hello".encode()).decode()
    with patch.object(VcsApiClient, "_do_request",
                      side_effect=lambda u, m, d=None, headers=None: {"content": content}) as m_req:
        r = c.get_readme()
    assert r["content"] == "hello"
    assert m_req.call_args.args[0].split("?")[0].endswith("/repos/acme/web/readme")


def test_s66_get_readme_gitcode_contents_fallback():
    """gitcode：/readme 未命中（404）→ /contents/README.md（存量链）。"""
    c = _client("gitcode")
    import base64
    content = base64.b64encode("readme-body".encode()).decode()
    urls = []

    def _fake(url, method, data=None, headers=None):
        path = url.split("?")[0]
        urls.append(path)
        if path.endswith("/readme"):
            return {"error": "HTTP 404"}
        return {"content": content}

    with patch.object(VcsApiClient, "_do_request", side_effect=_fake):
        r = c.get_readme()
    assert r["content"] == "readme-body"
    assert urls[-1].endswith("/contents/README.md")


def test_s66_get_readme_all_miss_empty():
    c = _client("gitcode")
    with patch.object(VcsApiClient, "_do_request",
                      side_effect=lambda u, m, d=None, headers=None: {"error": "HTTP 404"}):
        r = c.get_readme()
    assert r == {"content": "", "endpoint": ""}


# ------------------------------------------------ S66 templates 来源链与统一入口

def test_s66_templates_bot_config_source_chain():
    """_load_global_bot_config 统一走来源链（无平台名分支，两平台同一路径）。"""
    from src.domains.mgmt.routes.templates import _load_global_bot_config
    for prov in ("gitcode", "github", "gitee"):
        with patch("src.domains.mgmt.user.db.get_global_bot_token_for", return_value="TK") as m_tok, \
             patch("src.domains.mgmt.user.repo_manager.resolve_bot_username", return_value="bot") as m_name:
            tok, name = _load_global_bot_config(prov)
        assert (tok, name) == ("TK", "bot")
        assert m_tok.call_args.args[0] == prov
        assert m_name.call_args.args[0] == prov


def test_s66_templates_explicit_provider_token_unified():
    """显式 provider 分支的 token 获取统一走 D3 入口（源码守卫：无 gitcode 专属分支）。"""
    import inspect
    from src.domains.mgmt.routes.templates import create_from_template
    src = inspect.getsource(create_from_template)
    assert "get_user_token_with_refresh" not in src
    assert 'body.provider == "gitcode"' not in src
    assert "get_user_token_for" in src


def test_s66_templates_readme_card_env_token():
    """README 拉取 token 走平台卡用户级 env（卡派生，非平台名 env 字面量）。"""
    from src.domains.mgmt.routes import templates as templates_mod
    from src.domains.runtime.vcs.platform_card import get_platform_card
    import os
    card = get_platform_card("gitcode")
    env_name = card.user_token_env_name()
    assert env_name == "USER_GITCODE_TOKEN"
    # 路由读 os.environ[env_name]，空=匿名
    assert os.environ.get(env_name, "") == ""


# ------------------------------------------------ S66 feishu 协作者端口与 um 列

def test_s66_list_collaborators_port_gitcode_delegates():
    """存量平台委托：GitCodeProvider.list_collaborators → _fetch_gitcode_collaborators。"""
    with patch("src.domains.runtime.im.feishu_async._fetch_gitcode_collaborators", return_value=[{"login": "u"}]) as m:
        out = create_provider("gitcode").list_collaborators("T", "acme", "web")
    assert out == [{"login": "u"}]
    assert m.call_args.args == ("acme", "web", "T")


def test_s66_list_collaborators_port_github_delegates():
    with patch("src.domains.runtime.im.feishu_async._fetch_github_collaborators", return_value=[{"login": "g"}]) as m:
        out = create_provider("github").list_collaborators("T", "acme", "web")
    assert out == [{"login": "g"}]
    assert m.call_args.args == ("acme", "web", "T")


def test_s66_list_collaborators_generic_card_driven():
    """generic 适配器：GET /repos/{o}/{r}/collaborators 分页（卡驱动）。"""
    # gitee 无子类（纯配卡）→ 落在 GenericVcsProvider 基座；gitlab 已有子类
    # （S107/G2 方言层补齐，/projects 形态），不再是基座代表
    p = create_provider("gitee")
    assert isinstance(p, GenericVcsProvider)
    pages = [[{"login": f"u{i}"} for i in range(100)], [{"login": "u100"}]]

    def _fake(method, path, token, data=None, form=False):
        assert method == "GET" and token == "T"
        assert path.startswith("/repos/acme/web/collaborators?per_page=100&page=")
        return pages.pop(0)

    with patch.object(p, "_http", side_effect=_fake):
        out = p.list_collaborators("T", "acme", "web")
    assert len(out) == 101


def test_s66_feishu_sync_mapping_um_branch_by_whitelist():
    """群成员映射写入分支按同源白名单派生（无平台名 if 分支）。"""
    import inspect
    from src.domains.runtime.im.feishu_async import FeishuAsync
    fn_src = inspect.getsource(FeishuAsync.sync_repo_feishu_mapping)
    assert 'provider == "github"' not in fn_src
    assert 'provider == "gitcode"' not in fn_src
    assert "um_login_is_grape_account" in fn_src


def test_s66_um_login_is_grape_account_whitelist():
    """同源白名单：仅 gitcode（grape 账号名=gitcode 登录名，历史迁移形态）。"""
    from src.kernel.repo_key import um_login_is_grape_account
    assert um_login_is_grape_account("gitcode") is True
    for p in ("github", "gitee", "gitlab", ""):
        assert um_login_is_grape_account(p) is False


# ------------------------------------------------ S66 repo_roles 身份解析统一

def test_s66_repo_roles_platform_login_unified():
    from src.domains.mgmt.auth.repo_roles import _get_platform_login
    idents = [{"provider": "gitcode", "external_login": "gc"},
              {"provider": "github", "external_login": "gh"},
              {"provider": "gitee", "external_login": "ge"}]
    for prov, expected in (("gitcode", "gc"), ("github", "gh"), ("gitee", "ge")):
        with patch("src.domains.mgmt.auth.grape_account_db.get_user_identities_with_tokens",
                   return_value=idents):
            assert _get_platform_login({"id": 1}, prov) == expected
    with patch("src.domains.mgmt.auth.grape_account_db.get_user_identities_with_tokens",
               return_value=idents):
        assert _get_platform_login({"id": 1}, "gitlab") == ""


def test_s66_repo_roles_admin_check_via_port():
    """get_repo_admin_identity 权限判定走适配器端口（mock 端口，无平台名分支）。"""
    from src.domains.mgmt.auth.repo_roles import get_repo_admin_identity
    user = {"id": 7, "username": "u7"}
    # github 路径：端口返回 "admin" 字符串 → admin
    with patch("src.kernel.token_resolver.create_provider") as m_cp, \
         patch("src.domains.mgmt.auth.repo_roles._get_platform_login", return_value="someone"), \
         patch("src.api.oauth_token.with_oauth_token",
               side_effect=lambda user, provider, fn, *a, **k: fn("TOK")):
        m_cp.return_value.get_collaborator_permission.return_value = "admin"
        assert get_repo_admin_identity(user, "github:o/r") is not None
        assert m_cp.call_args.args[0] == "github"
    # gitee（generic）路径：端口返回 None → fail-closed
    with patch("src.kernel.token_resolver.create_provider") as m_cp, \
         patch("src.domains.mgmt.auth.repo_roles._get_platform_login", return_value="someone"), \
         patch("src.api.oauth_token.with_oauth_token",
               side_effect=lambda user, provider, fn, *a, **k: fn("TOK")):
        m_cp.return_value.get_collaborator_permission.return_value = None
        assert get_repo_admin_identity(user, "gitee:o/r") is None


# ------------------------------------------------ S66 outbound_ledger 形态派生

def test_s66_extract_content_id_shape_derived():
    from src.kernel.outbound_ledger import extract_event_content_id
    # GitHub 形态：comment.id 优先
    gh = {"comment": {"id": 11}, "object_attributes": {"id": 99}}
    assert extract_event_content_id("github", gh) == "11"
    # Gitee 系形态：Note Hook object_attributes.id（无顶层 comment）
    gc = {"object_attributes": {"id": 55}}
    assert extract_event_content_id("gitcode", gc) == "55"
    # 新平台（gitlab/gitee）走同一形态链，provider 参数不参与分支
    assert extract_event_content_id("gitee", gh) == "11"
    assert extract_event_content_id("gitlab", gc) == "55"
    assert extract_event_content_id("gitee", {}) == ""


def test_s66_extract_content_id_source_guard():
    """源码守卫：extract_event_content_id 无 provider 名分支。"""
    import inspect
    from src.kernel.outbound_ledger import extract_event_content_id as fn
    src = inspect.getsource(fn)
    assert 'provider == "gitcode"' not in src
    assert 'provider == "github"' not in src
