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