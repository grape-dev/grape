"""Gitee「所属空间」用例（NamespaceCapability 方言实现）。

锁三件事：
1. list_create_spaces：GET /user/orgs → [{path, name, kind}]，上游失败降级为空；
2. resolve_namespace：/orgs/{login} 精确命中 → {id, path}；不存在 → None（fail-closed）；
3. 同一事实源：下拉列出的 path 直接可用于建仓（POST /orgs/{owner}/repos）。
"""
from __future__ import annotations

from src.domains.mgmt.vcs.gitee import GiteeProvider


class _FakeHttp:
    def __init__(self, orgs=None, by_login=None, fail=False):
        self.calls: list = []
        self.orgs = orgs if orgs is not None else []
        self.by_login = by_login or {}
        self.fail = fail
        self.created: dict | None = None

    def __call__(self, method, path, token, data=None, form=False):
        self.calls.append((method, path, data))
        if self.fail:
            raise RuntimeError("boom")
        if path.startswith("/user/orgs"):
            return self.orgs
        if path.startswith("/orgs/") and "/repos" in path:
            self.created = dict(data or {})
            owner = path.split("/orgs/", 1)[1].split("/", 1)[0]
            return {"id": 9, "full_name": f"{owner}/proj", "name": "proj"}
        if path.startswith("/orgs/"):
            login = path.split("/orgs/", 1)[1].split("?", 1)[0]
            return self.by_login.get(login, {})
        raise AssertionError(f"unexpected path {path}")


def _make(fake: _FakeHttp) -> GiteeProvider:
    """构造实例并注入 _http（不读真实平台卡）。"""
    class _Card:
        name = "Gitee"
        api_base = "https://gitee.com/api/v5"
        api_auth = "query_token"
        dialect = "github"
        ca_bundle = ""
    p = GiteeProvider.__new__(GiteeProvider)
    p._card = _Card()
    p._http = fake                     # type: ignore[assignment]
    return p


def test_list_create_spaces_parses_orgs():
    fake = _FakeHttp(orgs=[{"id": 15542423, "login": "group-a", "name": "GROUP-A"},
                           {"id": 2, "login": "grape-org", "name": ""}])
    spaces = _make(fake).list_create_spaces("tok")
    assert spaces == [{"path": "group-a", "name": "GROUP-A", "kind": "org"},
                      {"path": "grape-org", "name": "grape-org", "kind": "org"}]
    assert fake.calls[0][1].startswith("/user/orgs")


def test_list_create_spaces_degrades_on_upstream_error():
    assert _make(_FakeHttp(fail=True)).list_create_spaces("tok") == []


def test_resolve_namespace_hits_and_misses():
    fake = _FakeHttp(by_login={"group-a": {"id": 15542423, "login": "group-a"}})
    p = _make(fake)
    assert p.resolve_namespace("tok", "group-a") == {"id": 15542423, "path": "group-a"}
    assert p.resolve_namespace("tok", "") is None
    assert p.resolve_namespace("tok", "no-such-org") is None


def test_listed_space_is_usable_for_create_repo():
    """同一事实源：下拉能列出的组织，建仓时走的正是 /orgs/{owner}/repos。"""
    fake = _FakeHttp(orgs=[{"id": 15542423, "login": "group-a", "name": "GROUP-A"}],
                     by_login={"group-a": {"id": 15542423, "login": "group-a"}})
    p = _make(fake)
    listed = [s["path"] for s in p.list_create_spaces("tok")]
    assert listed == ["group-a"]
    out = p.create_repo("tok", "proj", owner=listed[0], private=True)
    assert out["full_name"] == "group-a/proj"
    assert any(c[1].startswith("/orgs/group-a/repos") for c in fake.calls)