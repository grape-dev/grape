"""GitLab 子组仓库（grp/sub/proj）的 project id 必须整体编码。

回归背景：只编码 repo 段会拼出 /projects/grp/sub%2Fproj，而 GitLab 要的是
/projects/grp%2Fsub%2Fproj —— 子组仓库的一切读写都会 404（查重也恒说"不存在"）。
"""
from __future__ import annotations

import pytest

from src.domains.runtime.vcs.api_client import VcsApiClient


@pytest.mark.parametrize(
    "owner,repo,expected",
    [
        ("org-a", "repo-a", "org-a%2Frepo-a"),
        # 子组：owner 自带斜杠，必须一起编码
        ("group-a/sub-a", "repo-a", "group-a%2Fsub-a%2Frepo-a"),
        ("group-a/sub-a/sub-b", "repo-a", "group-a%2Fsub-a%2Fsub-b%2Frepo-a"),
        # 空格/特殊字符同样整体编码
        ("group a", "re po", "group%20a%2Fre%20po"),
    ],
)
def test_project_path_encodes_full_owner_repo(owner, repo, expected):
    c = VcsApiClient("gitlab", "t", owner, repo)
    assert c._project_path == expected


def test_no_bare_owner_slash_left_in_project_paths():
    """类内不得再出现 {owner}%2F{repo} 的手拼（子组会漏编码）。"""
    import inspect

    from src.domains.runtime.vcs import api_client as mod

    src = inspect.getsource(mod)
    assert "{self.owner}%2F{self.repo}" not in src


def test_subgroup_path_is_used_for_branch_listing(monkeypatch):
    """下游真实调用点取到的路径必须是编码后的 project id（不是手拼 owner/repo）。"""
    c = VcsApiClient("gitlab", "t", "group-a/sub-a", "repo-a")
    seen = {}

    def fake_request(method, path, *a, **k):
        seen["path"] = path
        return []

    monkeypatch.setattr(c, "_request", fake_request, raising=False)
    monkeypatch.setattr(c, "_card_request", fake_request, raising=False)
    try:
        c.list_branches()
    except Exception:  # noqa: BLE001 - 只关心路径形态
        pass
    assert "/projects/group-a%2Fsub-a%2Frepo-a" in seen.get("path", "")