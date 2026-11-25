"""S14/D13：VCS API 命令层配置驱动——gitee 走 VcsApiClient 零代码命令面。"""
import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.api_client import VcsApiClient
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
}


@pytest.fixture()
def gitee_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


class _FakeResp:
    def __init__(self, data):
        self._data = json.dumps(data).encode()
        self.headers = {}

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _calls(monkeypatch):
    """捕获 urllib urlopen 的 (url, method, headers, body)。"""
    seen = []

    class _Req:
        def __init__(self, url, data=None, method="", headers=None):
            self.url, self.data, self.method, self.headers = url, data, method, headers or {}

    def fake_urlopen(req, timeout=30):
        seen.append(req)
        return _FakeResp({"id": 77, "number": 5, "title": "t", "state": "open"})

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(urllib.request, "Request", _Req)
    return seen


def test_s14_comment_hits_card_endpoint(gitee_card, monkeypatch):
    seen = _calls(monkeypatch)
    c = VcsApiClient("gitee", "TK", "oschina", "grape")
    resp = c.post_issue_comment(5, "hello")
    assert "error" not in resp
    req = seen[-1]
    assert req.url == ("https://gitee.com/api/v5/repos/oschina/grape/issues/5/comments"
                       "?access_token=TK")
    assert req.method == "POST"
    assert json.loads(req.data) == {"body": "hello"}


def test_s14_get_issue_and_repo_info(gitee_card, monkeypatch):
    seen = _calls(monkeypatch)
    c = VcsApiClient("gitee", "TK", "oschina", "grape")
    c.get_issue(5)
    assert seen[-1].url == "https://gitee.com/api/v5/repos/oschina/grape/issues/5?access_token=TK"
    c.get_repo_info()
    assert seen[-1].url == "https://gitee.com/api/v5/repos/oschina/grape?access_token=TK"


def test_s14_create_pr_card(gitee_card, monkeypatch):
    seen = _calls(monkeypatch)
    c = VcsApiClient("gitee", "TK", "oschina", "grape")
    c.create_pr("T", "feat", "main", "b")
    req = seen[-1]
    assert req.url == "https://gitee.com/api/v5/repos/oschina/grape/pulls?access_token=TK"
    assert json.loads(req.data)["head"] == "feat"


def test_list_prs_head_filter_form_card_driven(monkeypatch):
    """D105-19 补充锁：head 过滤参数形态按卡 pr_head_filter——gitcode 裸 ref
    （实弹实测 owner:ref 形态返回空列表）、github owner:ref（存量语义）。"""
    seen = _calls(monkeypatch)
    invalidate_platform_card_cache()  # 防前序用例卡缓存串扰（meta fixture 遗留）
    c = VcsApiClient("gitcode", "TK", "own", "repo")
    c.list_prs(state="all", head="feat-x")
    assert "head=feat-x&" in seen[-1].url, seen[-1].url

    c2 = VcsApiClient("github", "TK", "own", "repo")
    c2.list_prs(state="all", head="feat-x")
    assert "head=own:feat-x&" in seen[-1].url, seen[-1].url

    # 卡声明 bare_ref 的 gitee（github 方言）同样发裸 ref
    c3 = VcsApiClient("gitee", "TK", "oschina", "grape")
    c3.list_prs(state="all", head="feat-x")
    assert "head=feat-x&" in seen[-1].url, seen[-1].url


def test_s14_gitlab_dialect_new_platform_paths(monkeypatch, gitee_card):
    """gitlab 方言新平台（如自建 GLS）：project 编码路径 + Bearer 鉴权（卡驱动）。"""
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gls": {"dialect": "gitlab", "host": "gls.internal",
                                 "api_base": "https://gls.internal/api/v4",
                                 "api_auth": "bearer_header"}}),))
        db.commit()
    invalidate_platform_card_cache()
    try:
        seen = _calls(monkeypatch)
        c = VcsApiClient("gls", "TK", "grp", "repo")
        c.post_issue_comment(9, "x")
        req = seen[-1]
        assert req.url == "https://gls.internal/api/v4/projects/grp%2Frepo/issues/9/notes"
        assert req.headers.get("Authorization") == "Bearer TK"
        c.create_pr("T", "h", "b", "")
        assert seen[-1].url == "https://gls.internal/api/v4/projects/grp%2Frepo/merge_requests"
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
            db.commit()
        invalidate_platform_card_cache()


def test_s14_existing_providers_request_style_unchanged(monkeypatch):
    """存量三平台请求实现原样（S9 回归：gitcode query_token / github token header / gitlab PRIVATE-TOKEN）。"""
    seen = _calls(monkeypatch)
    VcsApiClient("gitcode", "TK", "o", "r").get_issue(1)
    assert seen[-1].url == "https://gitcode.com/api/v5/repos/o/r/issues/1?access_token=TK"
    VcsApiClient("github", "TK", "o", "r").get_issue(1)
    assert seen[-1].url == "https://api.github.com/repos/o/r/issues/1"
    assert seen[-1].headers.get("Authorization") == "token TK"
    VcsApiClient("gitlab", "TK", "o", "r").get_issue(1)
    assert seen[-1].url == "https://gitlab.com/api/v4/projects/o%2Fr/issues/1"
    assert seen[-1].headers.get("PRIVATE-TOKEN") == "TK"


def test_s14_unsupported_provider_rejected(monkeypatch):
    _calls(monkeypatch)
    c = VcsApiClient("nonexist", "TK", "o", "r")
    resp = c.get_issue(1)
    assert "error" in resp and "unsupported" in resp["error"]


GITLAB_CARD = {
    "name": "GitLab", "dialect": "gitlab", "host": "gitlab.com",
    "webhook_path": "/webhook/gitlab", "event_header": "X-Gitlab-Event",
    "api_base": "https://gitlab.com/api/v4", "api_auth": "private_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
}


@pytest.fixture()
def gitlab_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitlab": GITLAB_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITLAB_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


def test_gitlab_repo_info_uses_projects_endpoint(gitlab_card, monkeypatch):
    """GitLab 仓库根必须走 /projects/{全路径编码}。

    回归（2026-11-20 线上）：`_generic_path(..., "")` 把空串当"未给"，于是
    get_repo_info 对 GitLab 请求了不存在的 `/repos/{o}/{r}` → 恒 404 →
    查重永远得到「不存在」（真实存在的 account-a/hello-fastapi-2 也被判为空）。
    """
    client = VcsApiClient("gitlab", "tok", "account-a", "hello-fastapi-2")
    seen = {}

    def _fake_request(method, path, data=None, form=False, timeout=30):
        seen["path"] = path
        return {"id": 1}

    monkeypatch.setattr(client, "_request", _fake_request)
    client.get_repo_info()
    assert seen["path"] == "/projects/account-a%2Fhello-fastapi-2"


def test_non_gitlab_dialect_keeps_github_style_repo_path(monkeypatch):
    """非 gitlab 方言不受影响：仍用 /repos/{o}/{r}（存量四平台逐字节不变）。"""
    client = VcsApiClient("gitcode", "tok", "owner", "repo")
    seen = {}
    monkeypatch.setattr(client, "_request",
                        lambda m, path, data=None, form=False, timeout=30: seen.update(path=path) or {})
    client.get_repo_info()
    assert seen["path"] == "/repos/owner/repo"


def test_gitlab_dialect_refuses_github_style_path(gitlab_card):
    """gitlab 卡下漏传 gitlab_style 必须报错，不能静默拼出 /repos/（否则 404 当「不存在」）。"""
    from src.domains.runtime.vcs.api_client import VcsPathContractError

    client = VcsApiClient("gitlab", "tok", "account-a", "demo")
    with pytest.raises(VcsPathContractError):
        client._generic_path("/repos/account-a/demo/issues")


def test_query_token_auth_failure_log_redacted(gitee_card, monkeypatch, caplog):
    """query 形态（Issue #4064）：请求 URL 仍带 token，日志只留 `access_token=***`。"""
    import src.kernel.upstream_http as _uh
    seen = {}

    def boom(req, **kw):
        seen["url"] = req.full_url
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                     io.BytesIO(b'{"error":"bad token"}'))

    monkeypatch.setattr(_uh, "open_with_policy", boom)
    with caplog.at_level("WARNING"):
        VcsApiClient("gitee", "TK_SECRET_QUERY", "oschina", "grape").get_issue(5)
    assert seen["url"].endswith("?access_token=TK_SECRET_QUERY")   # 鉴权形态不变
    assert "access_token=TK_SECRET_QUERY" not in caplog.text
    assert "access_token=***" in caplog.text


@pytest.mark.parametrize("provider,token,header", [
    ("github", "TK_SECRET_HEADER", "Authorization"),
    ("gitlab", "TK_SECRET_PRIVATE", "PRIVATE-TOKEN"),
])
def test_header_token_auth_failure_log_redacted(provider, token, header, monkeypatch, caplog):
    """header 形态（Issue #4064）：URL 不含 token，异常串回显 header 也须打码。"""
    import src.kernel.upstream_http as _uh
    seen = {}

    def boom(req, **kw):
        seen["headers"] = dict(req.headers or {})
        seen["url"] = req.full_url
        dumped = "; ".join(f"{k}: {v}" for k, v in seen["headers"].items())
        raise ConnectionError(f"upstream refused {req.full_url} ({dumped})")

    monkeypatch.setattr(_uh, "open_with_policy", boom)
    with caplog.at_level("WARNING"):
        VcsApiClient(provider, token, "own", "repo").get_issue(1)
    assert token in "".join(seen["headers"].values())            # 请求本身仍带凭据
    assert token not in caplog.text
    assert "VcsApiClient: GET" in caplog.text


def test_every_generic_path_call_site_declares_gitlab_form():
    """静态守卫：新增端点若忘记给 gitlab_style，这里直接红（防止再漏一个 /repos/）。"""
    import ast
    import pathlib

    src = pathlib.Path("src/domains/runtime/vcs/api_client.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_generic_path"):
            has_kw = any(k.arg == "gitlab_style" for k in node.keywords)
            if len(node.args) < 2 and not has_kw:
                offenders.append(node.lineno)
    assert not offenders, f"_generic_path 调用点未声明 GitLab 形态（行号）: {offenders}"


def test_issue_3348_get_user_hits_card_users_endpoint(gitee_card, monkeypatch):
    """Issue #3348：BOT_USERNAME 校验走 GET /users/{username}（卡 api_base + 鉴权）。"""
    seen = _calls(monkeypatch)
    c = VcsApiClient("gitee", "TK", "oschina", "grape")
    resp = c.get_user("grape bot")
    assert "error" not in resp
    req = seen[-1]
    assert req.url == "https://gitee.com/api/v5/users/grape%20bot?access_token=TK"
    assert req.method == "GET"


def test_issue_3348_gitlab_get_user_exact_match(gitlab_card, monkeypatch):
    """GitLab 用户查询需 username 精确匹配，避免模糊命中他人后误判有效。"""
    client = VcsApiClient("gitlab", "tok", "grp", "repo")
    seen = {}

    def _fake_request(method, path, data=None, form=False, timeout=30):
        seen["path"] = path
        return [{"username": "grape_bot_extra"}, {"username": "grape_bot", "id": 7}]

    monkeypatch.setattr(client, "_request", _fake_request)
    user = client.get_user("grape_bot")
    assert seen["path"] == "/users?username=grape_bot"
    assert user["id"] == 7


def test_issue_3348_gitlab_get_user_network_error_not_404(gitlab_card, monkeypatch):
    """GitLab 网络异常应原样返回错误 dict（非 404），由调用方 fail-open。"""
    client = VcsApiClient("gitlab", "tok", "grp", "repo")
    monkeypatch.setattr(client, "_request",
                        lambda *a, **kw: {"error": "connection reset"})
    resp = client.get_user("grape_bot")
    assert resp.get("error") == "connection reset"
    assert resp.get("status_code") is None
