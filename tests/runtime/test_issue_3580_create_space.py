"""Issue #3580（M 批）：一键创建项目支持「所属空间」（GitLab 组/子组）。

锁四件事：
1. GitLabProvider.create_repo：owner 非空时先 `/namespaces?search=` 解析 namespace_id
   再 POST /projects（GitLab 只认数字 id）；解析不到 → 明确错误，不带 id 硬建到个人空间；
2. list_create_spaces：`/groups?min_access_level=30` → full_path 列表；上游失败降级为空；
3. 后端校验与回填：`_valid_space_path` 接受组路径、拒绝点段/空段/首尾斜杠；
   `_owner_from_full_name` 对 `grp/sub/proj` 回填空路径（不能截成 grp）；
4. 创建表单的数据源接口 `/repos/create-targets` 与 `/repos/check-name?owner=`：
   指定空间时按空间查重（子组仓不误判成占用）。
"""
from __future__ import annotations

import pytest

from src.domains.mgmt.routes.repos import _owner_from_full_name, _valid_space_path
from src.kernel.token_resolver import GitLabProvider


class _FakeHttp:
    def __init__(self, namespaces=None, groups=None, create_response=None):
        self.calls: list = []
        self.namespaces = namespaces if namespaces is not None else []
        self.groups = groups if groups is not None else []
        self.create_response = create_response or {"id": 1, "full_name": "grp/sub/proj"}

    def __call__(self, method, path, token, data=None):
        self.calls.append((method, path, data))
        if path.startswith("/namespaces"):
            return self.namespaces
        if path.startswith("/groups"):
            return self.groups
        return self.create_response


def _provider(fake: _FakeHttp) -> GitLabProvider:
    p = GitLabProvider()
    p._http = fake          # type: ignore[assignment]
    return p


def test_create_repo_resolves_namespace_for_subgroup():
    fake = _FakeHttp(namespaces=[{"id": 42, "full_path": "grp/sub", "kind": "group"},
                                 {"id": 7, "full_path": "grp", "kind": "group"}])
    out = _provider(fake).create_repo("tok", "proj", owner="grp/sub", private=True)
    assert out["full_name"] == "grp/sub/proj"
    paths = [c[1] for c in fake.calls]
    assert paths[0].startswith("/namespaces?search=grp%2Fsub")
    post = [c for c in fake.calls if c[0] == "POST"][-1]
    assert post[1] == "/projects"
    assert post[2]["namespace_id"] == 42          # 不是 7（前缀组），必须 full_path 精确匹配
    assert post[2]["visibility"] == "private"
    assert post[2]["initialize_with_readme"] is True


def test_create_repo_without_owner_keeps_personal_path():
    fake = _FakeHttp()
    _provider(fake).create_repo("tok", "proj")
    assert all(not str(c[1]).startswith("/namespaces") for c in fake.calls)
    assert "namespace_id" not in [c for c in fake.calls if c[0] == "POST"][-1][2]


def test_create_repo_unknown_namespace_fails_loudly():
    fake = _FakeHttp(namespaces=[])
    out = _provider(fake).create_repo("tok", "proj", owner="grp/nope")
    assert "error" in out and "grp/nope" in out["error"]
    assert all(c[0] != "POST" for c in fake.calls)   # 不落库到错误的命名空间


def test_list_create_spaces_returns_full_paths():
    fake = _FakeHttp(groups=[{"id": 1, "full_path": "grp", "name": "GRP"},
                             {"id": 2, "full_path": "grp/sub", "name": "SUB"}])
    spaces = _provider(fake).list_create_spaces("tok")
    assert [s["path"] for s in spaces] == ["grp", "grp/sub"]

    fake_bad = _FakeHttp()
    fake_bad.__call__ = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    assert _provider(fake_bad).list_create_spaces("tok") == []


def test_space_path_validation():
    for good in ("grp", "grp/sub", "grp-a/sub_1.2"):
        assert _valid_space_path(good) is True
    for bad in ("", "/grp", "grp/", "grp//sub", "grp/../sub", "grp/./sub", "gr p/sub"):
        assert _valid_space_path(bad) is False


def test_owner_from_full_name_keeps_subgroup_path():
    assert _owner_from_full_name("grp/sub/proj") == "grp/sub"
    assert _owner_from_full_name("owner/repo") == "owner"
    assert _owner_from_full_name("bare") == ""

def test_upstream_rejection_becomes_error_dict_not_exception(monkeypatch):
    """上游拒绝建仓（403/400/重名）必须落 error dict：抛异常会被路由当 500，用户看不到原因。"""
    prov = _provider(_FakeHttp())

    def boom(*a, **k):
        raise RuntimeError('GitLab API POST /projects failed (400): Bad Request | '
                           '{"message":{"namespace":["is not valid"]}}')

    monkeypatch.setattr(prov, "_http", boom)
    out = prov.create_repo("tok", "demo")
    assert out.get("error")
    assert "is not valid" in out["error"] or "上游拒绝" in out["error"]
