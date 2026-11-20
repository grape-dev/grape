"""S72 用例：GitLab 托管链收口（D47，先测后改，实弹+官方文档形态驱动）。

现场（2026-11-05 实测/文档核对）：
- GenericVcsProvider._http 对 api_auth=private_token 发 Bearer 头——
  GitLab 要求 PRIVATE-TOKEN 头，verify_token 必 401；
- 协作者/webhook/建仓路径是 github 形态（/repos/{o}/{r}/...），GitLab 实际
  是 /projects/{o}%2F{r} 家族——托管链全断；
- GitLabBackend 缺 verify（已另修，S71）。

先测后改：RED = 形态断言（URL/头/事件位逐字段）+ gitcode/github parity 锁。
"""
import json
from unittest.mock import patch

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache
from src.kernel.token_resolver import create_provider, GenericVcsProvider, GitLabProvider


@pytest.fixture(autouse=True)
def _clean_meta():
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


def _prov():
    p = create_provider("gitlab")
    assert isinstance(p, GitLabProvider), f"gitlab should map GitLabProvider, got {type(p)}"
    return p


def _capture(calls):
    def _fake(method, path, token, data=None, form=False):
        calls.append({"method": method, "path": path, "token": token,
                      "data": data, "form": form})
        return {"id": 42, "username": "glu", "url": "http://wh"}
    return _fake


def test_s72_private_token_header():
    """api_auth=private_token → PRIVATE-TOKEN 头（非 Bearer）——verify_token 通。"""
    p = _prov()
    calls = []
    with patch.object(GenericVcsProvider, "_http", side_effect=_capture(calls)):
        info = p.verify_token("glpat-1")
    assert info.get("username") == "glu"
    assert calls[0]["path"] == "/user"
    # 基座 _http 形态：单独直测（不 mock 本体）
    req = {}
    import urllib.request as _ur
    orig = _ur.Request

    def _spy(url, data=None, method=None, headers=None, **kw):
        req.update({"url": url, "headers": headers or {}})
        return orig(url, data=data, method=method, headers=headers or {}, **kw)

    # Issue #3580 复审：上游 HTTP 收口到 src.kernel.upstream_http.open_with_policy（私有 CA /
    # 不跟随重定向），patch 点随之迁移；断言仍锁 PRIVATE-TOKEN 头与"不发 Bearer"。
    import src.kernel.upstream_http as _uh
    with patch.object(_ur, "Request", side_effect=_spy), \
         patch.object(_uh, "open_with_policy",
                      return_value=FakeResp({"id": 1, "username": "u"})):
        p2 = create_provider("gitlab")
        p2._http("GET", "/user", "glpat-1")
    assert req["headers"].get("PRIVATE-TOKEN") == "glpat-1", (
        f"PRIVATE-TOKEN header missing: {req['headers']}")
    assert "Authorization" not in req["headers"]


class FakeResp:
    def __init__(self, payload):
        self._p = json.dumps(payload).encode()

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_s72_get_user_login_gitlab_field():
    """gitlab /user 的登录名字段是 username（非 login）。"""
    p = _prov()
    with patch.object(GenericVcsProvider, "_http",
                      return_value={"id": 9, "username": "gluser", "name": "GL User"}):
        assert p.get_user_login("t") == "gluser"


def test_s72_webhook_projects_path_and_flags():
    """建钩：/projects/{o}%2F{r}/hooks + token + 布尔位事件集（GitLab 形态）。

    口径更新（Issue #3580 复审）：GitLab hooks 实现已收口到
    `src/kernel/vcs_gitlab_hooks.py`，provider 侧只做委托，故拦截点由
    `GenericVcsProvider._http` 改为 `vcs_gitlab_hooks._request`（断言改为完整 URL）。
    """
    from src.kernel import vcs_gitlab_hooks as _hooks

    p = _prov()
    calls = []

    def _fake(method, url, token="", body=None, timeout=30):
        calls.append({"method": method, "url": url, "body": body})
        return {"id": 42}

    with patch.object(_hooks, "_request", side_effect=_fake):
        assert p.setup_webhook("T", "acme", "web", "http://wh", webhook_secret="sec") == 42
    c = calls[0]
    assert "/projects/acme%2Fweb/hooks" in c["url"], c["url"]
    body = c["body"]
    assert body.get("url") == "http://wh"
    assert body.get("token") == "sec"
    assert body.get("push_events") is True
    assert body.get("issues_events") is True
    assert body.get("note_events") is True
    assert body.get("merge_requests_events") is True


def test_s72_find_and_remove_webhook_projects_path():
    """查/删钩（实现已收口到 kernel.vcs_gitlab_hooks）：路径同上，删除按 id 定位。"""
    from src.kernel import vcs_gitlab_hooks as _hooks

    p = _prov()
    calls = []

    def _fake(method, url, token="", body=None, timeout=30):
        calls.append({"method": method, "url": url})
        if method == "GET":
            return [{"id": 7, "url": "http://wh"}, {"id": 8, "url": "http://other"}]
        return {}

    with patch.object(_hooks, "_request", side_effect=_fake):
        assert p.find_existing_webhook("T", "acme", "web", "http://wh") == 7
        ok = p.remove_webhook("T", "acme", "web", 7)
    assert ok is True
    assert "projects/acme%2Fweb/hooks" in calls[0]["url"]
    assert "projects/acme%2Fweb/hooks/7" in calls[1]["url"]


def test_s72_create_repo_projects():
    p = _prov()
    with patch.object(GenericVcsProvider, "_http",
                      return_value={"id": 5, "path_with_namespace": "glu/r1"}) as m:
        r = p.create_repo("T", "r1", "d", private=True)
    assert r["path_with_namespace"] == "glu/r1"
    c = m.call_args
    assert c.args[1] == "/projects"
    body = c.args[3]
    assert body.get("name") == "r1"
    assert body.get("visibility") in ("private",)


def test_s72_add_collaborator_member():
    """加协作者：项目 members（按 username 查 user_id 再 POST）。"""
    p = _prov()
    seq = [[{"id": 77, "username": "bot"}],  # GET /users?username= → 用户列表
           {"id": 9, "access_level": 40}]     # POST members 返回
    calls = []

    def _fake(method, path, token, data=None, form=False):
        calls.append({"method": method, "path": path, "data": data})
        return seq.pop(0)

    with patch.object(GenericVcsProvider, "_http", side_effect=_fake):
        ok = p.add_collaborator("T", "acme", "web", "bot")
    assert ok is True
    assert calls[0]["path"].startswith("/users?username=bot") or "username=bot" in calls[0]["path"]
    assert "projects/acme%2Fweb/members" in calls[1]["path"]
    assert calls[1]["data"].get("user_id") == 77
    assert calls[1]["data"].get("access_level") == 40


def test_s72_fetch_repos_membership():
    p = _prov()
    with patch.object(GenericVcsProvider, "_http",
                      return_value=[{"path_with_namespace": "acme/web", "permissions":
                                     {"project_access": {"access_level": 40}}}]) as m:
        repos = p.fetch_repos_with_role("T")
    assert m.call_args.args[1].startswith("/projects?membership")
    assert repos[0]["full_name"] == "acme/web"
    assert repos[0]["is_admin"] is True


def test_s72_gitcode_github_parity_unchanged():
    """特征锁：gitcode/github 不落 GitLabProvider（既有分派不变）。"""
    from src.kernel.token_resolver import GitCodeProvider, GitHubProvider
    assert isinstance(create_provider("gitcode"), GitCodeProvider)
    assert isinstance(create_provider("github"), GitHubProvider)
