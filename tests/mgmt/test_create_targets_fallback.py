"""create-targets 空间清单兜底（2026-11-22）

背景：一键创建表单的「所属空间」来自 /repos/create-targets → 适配器
list_create_spaces；该端口只有 gitee/gitlab 实现，github/gitcode 在基类恒
返回 []。而「新建项目」的组织页用的是 user-namespaces 注册表（四平台齐备），
于是同一用户在两个入口看到的空间不一样。

用例锁定：
1. 适配器有数据 → 直接用它，不去打注册表（gitee/gitlab 行为不变）
2. 适配器为空 → 回落注册表，归一成 {path,name,kind}
3. 兜底自身异常 → 不抛，返回个人空间（不阻断建仓）
"""
import pytest

from src.domains.mgmt.vcs import namespaces as vns
from src.domains.mgmt.user.namespaces import NamespaceRef


class _FakeProvider:
    def __init__(self, spaces):
        self._spaces = spaces

    def get_user_login(self, token):
        return "me"

    def list_create_spaces(self, token):
        return list(self._spaces)


def _patch_provider(monkeypatch, spaces):
    monkeypatch.setattr("src.domains.mgmt.vcs.create_provider",
                        lambda p: _FakeProvider(spaces))


def _patch_registry(monkeypatch, refs=None, boom=False):
    def _list(provider, token, account):
        if boom:
            raise RuntimeError("registry down")
        return refs or []

    monkeypatch.setattr("src.domains.mgmt.user.namespaces.list_namespaces", _list)


def test_adapter_spaces_win_without_registry(monkeypatch):
    """适配器已有数据：不再走注册表（避免两套口径互相覆盖）。"""
    _patch_provider(monkeypatch, [{"path": "grp", "name": "GRP", "kind": "group"}])

    def _boom(*a, **k):
        raise AssertionError("适配器有数据时不应调用注册表")

    monkeypatch.setattr("src.domains.mgmt.user.namespaces.list_namespaces", _boom)
    out = vns._collect("gitlab", "tok")
    assert out["spaces"] == [{"path": "grp", "name": "GRP", "kind": "group"}]
    assert out["personal_login"] == "me"


def test_empty_adapter_falls_back_to_registry(monkeypatch):
    """适配器为空（github/gitcode 现状）：回落注册表拿到组织/空间。"""
    _patch_provider(monkeypatch, [])
    _patch_registry(monkeypatch, refs=[
        NamespaceRef(provider="github", path="acme", name="Acme", kind="org"),
    ])
    out = vns._collect("github", "tok")
    assert out["spaces"] == [{"path": "acme", "name": "Acme", "kind": "org"}]


def test_registry_failure_degrades_to_personal_only(monkeypatch):
    """兜底异常不能把建仓入口搞挂：返回空清单 = 只有个人空间。"""
    _patch_provider(monkeypatch, [])
    _patch_registry(monkeypatch, boom=True)
    out = vns._collect("gitcode", "tok")
    assert out["spaces"] == []
    assert out["provider"] == "gitcode"

def test_personal_namespace_not_duplicated(monkeypatch):
    """注册表会把个人 namespace（path == login）也列出来；下拉已有固定
    「个人账号」行，必须剔除避免重复项。"""
    _patch_provider(monkeypatch, [])
    _patch_registry(monkeypatch, refs=[
        NamespaceRef(provider="gitcode", path="me", name="me", kind="personal"),
        NamespaceRef(provider="gitcode", path="signway", name="signway", kind="org"),
    ])
    out = vns._collect("gitcode", "tok")
    assert [s["path"] for s in out["spaces"]] == ["signway"]
