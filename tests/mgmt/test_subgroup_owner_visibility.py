"""子组空间（grp/sub）必须能通过建仓前的可见性校验。

回归背景：校验只比对 namespace_paths（GitLab /groups 只返回顶层组），子组永远不在清单里
→ 用户手填 grp/sub 时 400「所属空间不在可用清单内」，而建仓 API 本身支持按完整路径寻址。
"""
from __future__ import annotations

import pytest

from src.domains.mgmt.user import namespaces as ns


@pytest.fixture()
def no_orgs(monkeypatch):
    monkeypatch.setattr(ns, "namespace_paths", lambda *a, **k: {"group-a"}, raising=False)
    return monkeypatch


def _patch_resolve(monkeypatch, result):
    from src.domains.mgmt import vcs as vcs_mod

    class _P:
        def resolve_namespace(self, token, owner):
            return result

    monkeypatch.setattr(vcs_mod, "create_provider", lambda provider: _P())


def test_subgroup_passes_when_resolve_namespace_finds_it(no_orgs):
    _patch_resolve(no_orgs, {"id": 42, "path": "group-a/sub-a"})
    ns.assert_owner_visible("gitlab", "tok", "group-a/sub-a")  # 不抛即通过


def test_subgroup_rejected_when_resolve_namespace_is_empty(no_orgs):
    _patch_resolve(no_orgs, None)
    with pytest.raises(ns.NamespaceNotVisible):
        ns.assert_owner_visible("gitlab", "tok", "group-a/sub-a")


def test_top_level_group_still_uses_the_list(no_orgs):
    """无斜杠的 owner 不走精确解析（清单已覆盖），保持原有拒绝语义。"""
    _patch_resolve(no_orgs, {"id": 1})  # 即便解析能出结果也不该被用到
    with pytest.raises(ns.NamespaceNotVisible):
        ns.assert_owner_visible("gitlab", "tok", "other-group")


def test_non_gitlab_provider_with_slash_is_not_whitelisted(no_orgs):
    _patch_resolve(no_orgs, {"id": 1})
    with pytest.raises(ns.NamespaceNotVisible):
        ns.assert_owner_visible("github", "tok", "owner/repo")


def test_empty_owner_is_personal_space(no_orgs):
    ns.assert_owner_visible("gitlab", "tok", "")