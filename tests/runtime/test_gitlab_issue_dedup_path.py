"""GitLab 查重路径回归（2026-11-22）：分页遍历必须走 project 编码路径。

实锤 bug：`_find_open_issue_impl` 的分页遍历硬编码 `/repos/{owner}/{repo}/issues`，
对 GitLab 是不存在的路由 → 恒 404 → 建单前查重被误报成「幂等查重失败（token 可能失效）。
本用例锁两件事：gitlab 方言用 `/projects/{owner}%2F{repo}/issues` + `state=opened`；
github 系方言（gitcode/gitee/github）仍走 `/repos/…` + `state=open`，零回归。
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest  # noqa: E402

from src.domains.runtime.vcs import api_client as api_mod  # noqa: E402
from src.domains.runtime.vcs.api_client import VcsApiClient  # noqa: E402


def _no_title_cache(monkeypatch):
    """标题索引是模块级函数（带 DB 缓存），用例里旁路掉。"""
    monkeypatch.setattr(api_mod, "_open_title_get", lambda *a, **k: None)
    monkeypatch.setattr(api_mod, "_open_title_put", lambda *a, **k: None)


def _client(provider, dialect, monkeypatch):
    c = VcsApiClient(provider, "tok", "group-a", "demo-a")
    monkeypatch.setattr(c, "_platform_card",
                        lambda: types.SimpleNamespace(dialect=dialect, issue_search=False))
    _no_title_cache(monkeypatch)
    seen = []

    def fake_request(method, path, *a, **kw):
        seen.append(path)
        return []
    monkeypatch.setattr(c, "_request", fake_request)
    return c, seen


def test_gitlab_dedup_uses_project_scoped_issues(monkeypatch):
    c, seen = _client("gitlab", "gitlab", monkeypatch)
    assert c.find_open_issue_by_title("需求标题") is None
    assert seen, "应当至少请求一次 issues 列表"
    assert "/repos/" not in seen[0], f"gitlab 方言不允许 /repos 路径: {seen[0]}"
    assert "/projects/group-a%2Fdemo-a/issues" in seen[0]
    assert "state=opened" in seen[0]


@pytest.mark.parametrize("provider", ["gitcode", "gitee", "github"])
def test_v5_dialects_keep_legacy_path(provider, monkeypatch):
    c, seen = _client(provider, "github", monkeypatch)
    assert c.find_open_issue_by_title("需求标题") is None
    assert seen and seen[0].startswith("/repos/group-a/demo-a/issues")
    assert "state=open&" in seen[0]


def test_gitlab_dedup_finds_existing_iid(monkeypatch):
    c = VcsApiClient("gitlab", "tok", "group-a", "demo-a")
    monkeypatch.setattr(c, "_platform_card",
                        lambda: types.SimpleNamespace(dialect="gitlab", issue_search=False))
    _no_title_cache(monkeypatch)
    monkeypatch.setattr(c, "_request", lambda *a, **kw: [
        {"iid": 42, "title": "需求标题", "state": "opened"},
        {"iid": 43, "title": "别的", "state": "opened"},
    ])
    assert c.find_open_issue_by_title("需求标题") == 42