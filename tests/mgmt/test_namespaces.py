"""命名空间领域（mgmt.user.namespaces）：白名单分桶 + 服务端 owner 校验。

三条不变量：
1. 未知类型不得落进「个人」桶（个人是权限最宽的桶，fail-safe defaults）；
2. GitLab / Gitee 的组织空间必须在清单里（原先 fetcher 注册表只登记了 gitcode/github）；
3. owner 必须与**服务端自己拉的清单**比对，前端下拉只是投影。
"""
import pytest

from src.domains.mgmt.user.namespaces import (
    KIND_ORG,
    KIND_PERSONAL,
    KIND_UNKNOWN,
    NamespaceNotVisible,
    NamespaceRef,
    assert_owner_visible,
    classify_namespace_kind,
    compose_namespaces,
    listable_providers,
    namespace_paths,
)


def test_kind_classification_is_whitelist():
    assert classify_namespace_kind("personal") == KIND_PERSONAL
    assert classify_namespace_kind("user") == KIND_PERSONAL          # 历史字面量
    assert classify_namespace_kind("org") == KIND_ORG
    assert classify_namespace_kind("group") == KIND_ORG              # GitLab 原生
    for unknown in ("", "team", "enterprise", None):
        assert classify_namespace_kind(unknown) == KIND_UNKNOWN      # 未知不落个人桶


def test_namespace_ref_normalizes_payload_type():
    ref = NamespaceRef(provider="gitlab", path="grp/sub", name="SUB", kind="group")
    payload = ref.as_payload()
    assert payload["type"] == KIND_ORG and payload["login"] == "grp/sub"
    personal = NamespaceRef(provider="gitee", path="alice", name="Alice", kind="user")
    assert personal.as_payload()["type"] == KIND_PERSONAL and personal.is_personal


def test_compose_puts_personal_first_and_dedupes_account():
    refs = compose_namespaces("gitlab", {"login": "alice", "name": "Alice"}, [
        {"path": "grp", "name": "GRP"},
        {"path": "alice", "name": "Alice"},          # 与账号同名 → 去重
        {"path": "", "name": "broken"},              # 空路径 → 丢弃
    ])
    assert [r.path for r in refs] == ["alice", "grp"]
    assert refs[0].is_personal and refs[1].is_org


def test_listable_providers_cover_gitlab_and_gitee():
    providers = listable_providers()
    assert {"gitcode", "github", "gitlab", "gitee"} <= set(providers)


def test_gitee_lister_uses_card_http_capability(monkeypatch):
    from src.domains.mgmt.user import namespaces as ns_mod
    import src.kernel.token_resolver as tr

    class _Prov:
        def _http(self, method, path, token):
            assert path.startswith("/user/orgs")
            return [{"login": "grp", "name": "GRP", "avatar_url": ""},
                    {"login": "", "name": "broken"}]

    monkeypatch.setattr(tr, "create_provider", lambda p: _Prov())
    refs = ns_mod.list_org_namespaces("gitee", "tok")
    assert [r.path for r in refs] == ["grp"] and refs[0].kind == KIND_ORG


def test_assert_owner_visible_accepts_personal_and_org(monkeypatch):
    from src.domains.mgmt.user import namespaces as ns_mod

    monkeypatch.setattr(ns_mod, "namespace_paths",
                        lambda provider, token, account=None: {"alice", "grp/sub"})
    assert_owner_visible("gitlab", "tok", "alice", {"login": "alice"})
    assert_owner_visible("gitlab", "tok", "grp/sub", {"login": "alice"})
    assert_owner_visible("gitlab", "tok", "", {"login": "alice"})      # 空 = 个人空间


def test_assert_owner_visible_rejects_unknown_namespace(monkeypatch):
    from src.domains.mgmt.user import namespaces as ns_mod

    monkeypatch.setattr(ns_mod, "namespace_paths", lambda *a, **k: {"alice"})
    with pytest.raises(NamespaceNotVisible) as exc:
        assert_owner_visible("gitlab", "tok", "someone-else", {"login": "alice"})
    assert "不在当前账号可用的空间清单内" in str(exc.value)


def test_namespace_paths_shape(monkeypatch):
    from src.domains.mgmt.user import namespaces as ns_mod

    monkeypatch.setattr(ns_mod, "list_org_namespaces",
                        lambda provider, token: [NamespaceRef(provider, "grp", "GRP", kind="org")])
    assert namespace_paths("gitlab", "tok", {"login": "alice"}) == {"alice", "grp"}


def test_route_guard_maps_domain_error_to_400(monkeypatch):
    """路由只做错误码映射：领域异常 → 400 NAMESPACE_NOT_VISIBLE。"""
    from fastapi import HTTPException

    from src.domains.mgmt.routes import repos as repos_route
    import src.domains.mgmt.user.namespaces as ns_mod

    def _boom(*a, **k):
        raise NamespaceNotVisible("gitlab: 所属空间 nope 不在当前账号可用的空间清单内")

    monkeypatch.setattr(ns_mod, "assert_owner_visible", _boom)
    with pytest.raises(HTTPException) as exc:
        repos_route._assert_owner_visible("gitlab", "tok", "nope", {"username": "alice"})
    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["error_code"] == "NAMESPACE_NOT_VISIBLE"

def test_gitlab_lister_uses_provider_spaces_port(monkeypatch):
    """GitLab 组织清单必须走 provider.list_create_spaces（组/子组完整 full_path）。

    回归：路由曾继续用只登记 gitcode/github 的旧 fetcher 表，于是 gitlab 的
    组织空间在清单里恒为空——接口 200 但只有个人项。
    """
    from src.domains.mgmt.user import namespaces as ns_mod
    import src.kernel.token_resolver as tr

    class _Prov:
        def list_create_spaces(self, token):
            return [{"path": "grp/sub", "name": "SUB", "kind": "group"}]

    monkeypatch.setattr(tr, "create_provider", lambda p: _Prov())
    refs = ns_mod.list_org_namespaces("gitlab", "tok")
    assert [(r.path, r.kind) for r in refs] == [("grp/sub", KIND_ORG)]


def test_personal_owner_uses_platform_login_not_local_username(monkeypatch):
    """个人空间必须按**平台登录名**认，不能拿本系统账号名当平台身份。

    实锤（2026-11-20 线上）：gvtest3 绑定 gitlab PAT（平台登录 yangaiwu），
    前端个人空间项 path=yangaiwu；check-name 传 owner=yangaiwu 被 400
    NAMESPACE_NOT_VISIBLE（个人项按 gvtest3 合成），而 owner="" 反而成功。
    """
    from src.domains.mgmt.user import namespaces as ns_mod
    import src.kernel.token_resolver as tr

    class _Prov:
        def verify_token(self, token):
            return {"username": "yangaiwu", "name": "aiwu yang"}   # GitLab /user 字段

        def list_create_spaces(self, token):
            return [{"path": "xiaowu3", "name": "xiaowu-群组", "kind": "group"}]

    monkeypatch.setattr(tr, "create_provider", lambda p: _Prov())

    paths = ns_mod.namespace_paths("gitlab", "tok", {"login": "gvtest3"})
    assert paths == {"yangaiwu", "xiaowu3"}
    ns_mod.assert_owner_visible("gitlab", "tok", "yangaiwu", {"login": "gvtest3"})
    ns_mod.assert_owner_visible("gitlab", "tok", "xiaowu3", {"login": "gvtest3"})
    with pytest.raises(NamespaceNotVisible):
        ns_mod.assert_owner_visible("gitlab", "tok", "gitlab-org", {"login": "gvtest3"})
    # 本系统账号名不再是有效 owner
    with pytest.raises(NamespaceNotVisible):
        ns_mod.assert_owner_visible("gitlab", "tok", "gvtest3", {"login": "gvtest3"})


def test_platform_login_falls_back_when_provider_unavailable(monkeypatch):
    """平台身份解析失败时退回调用方给的账号信息（不因网络抖动把校验全打死）。"""
    from src.domains.mgmt.user import namespaces as ns_mod
    import src.kernel.token_resolver as tr

    def _boom(p):
        raise RuntimeError("provider down")

    monkeypatch.setattr(tr, "create_provider", _boom)
    assert ns_mod.platform_login("gitlab", "tok") == ""
