"""托管细节加固：重名判定 / GitLab 建仓能力位 / hook 查询失败不重复建。"""
from __future__ import annotations

import pytest

from src.domains.mgmt.routes.templates import _is_duplicate_repo_error


@pytest.mark.parametrize(
    "msg",
    [
        "HTTP 400: {\"message\":{\"name\":[\"has already been taken\"]}}",  # GitLab 真实文案
        "400 already been taken",
        "HTTP 409 conflict",
        "422 Unprocessable",
        "repository already exists",
        "path already in use",
        "仓库已存在",
    ],
)
def test_duplicate_repo_errors_are_recognised(msg):
    assert _is_duplicate_repo_error(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "502 bad gateway",
        "connection timed out",
        "403 forbidden: token lacks api scope",
        "",
    ],
)
def test_other_errors_are_not_treated_as_duplicates(msg):
    assert _is_duplicate_repo_error(msg) is False


def test_gitlab_card_declares_create_repo_options():
    """能力位缺失时 auto_init=False 会被静默丢掉，模板推送就撞上平台生成的 README。"""
    from src.domains.runtime.vcs.platform_card import build_platform_card
    import json
    import pathlib

    raw = json.loads(pathlib.Path("configs/platforms/gitlab.json").read_text(encoding="utf-8"))
    card = build_platform_card("gitlab", raw)
    assert card.create_repo_options is True


def test_gitlab_find_existing_webhook_does_not_swallow_failures(monkeypatch):
    """查询失败若吞成 None，托管流程会再建一条同 URL 的重复 hook。"""
    from src.domains.mgmt.vcs.gitlab import GitLabProvider
    import src.kernel.vcs_gitlab_hooks as hooks

    def _boom(*a, **k):
        raise RuntimeError("upstream 500")

    monkeypatch.setattr(hooks, "find_hook", _boom)
    p = GitLabProvider.__new__(GitLabProvider)
    p._card = type("C", (), {"api_base": "https://gitlab.example.com"})()

    with pytest.raises(RuntimeError):
        GitLabProvider.find_existing_webhook(p, "tok", "group-a", "repo-a", "https://x/webhook/gitlab")


def test_repair_host_reports_query_failure_instead_of_creating(monkeypatch):
    """托管修复入口不得在查询失败后继续建 hook。"""
    import inspect

    from src.domains.mgmt.routes import repos as repos_mod

    src = inspect.getsource(repos_mod)
    assert "query_webhook" in src
    assert "未重复创建" in src

@pytest.mark.parametrize("msg", ["port 1409 refused", "commit 44229f", "id=4220"])
def test_status_code_substrings_are_not_duplicates(msg):
    """上游错误体里的数字不能被当成 409/422 重名信号。"""
    assert _is_duplicate_repo_error(msg) is False
