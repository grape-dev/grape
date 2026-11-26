# -*- coding: utf-8 -*-
"""GitHub OAuth scope contract: repo (list/read private repos) + workflow (push .github/workflows).

实测（grapedev-test / gt-load-1304 私仓案例）：
scope 只有 read:user user:email 时，GitHub 对 /user/repos 返回空、
对私仓按 404 处理（不泄露存在性）——登录建号正常，但仓库页永远为空，
用户表现为「登录了却一个仓库都看不到」。

Missing workflow (Issue #4301): any push containing .github/workflows/*.yml is refused
(refusing to allow ... without workflow scope), and the Coding Agent can only park workflows
under ci/ for manual hand-in. The 2026-11-25 owner decision added workflow/admin:repo_hook back
to the login channel; this case pins that decision against the next minimal-scope sweep.
"""
import json
from pathlib import Path

import pytest

CONFIGS = ("configs/oauth-providers.json", "configs/oauth-providers.example.json")


def _github_scope(path: str) -> str:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    return ((cfg.get("github") or {}).get("scope") or "")


@pytest.mark.parametrize("path", CONFIGS)
def test_github_scope_includes_repo(path):
    scope = _github_scope(path)
    assert "repo" in scope.split(), (
        f"{path}: github OAuth scope 缺 repo → 登录后 /user/repos 列不出私仓，"
        f"仓库页空。当前 scope={scope!r}")


@pytest.mark.parametrize("path", CONFIGS)
def test_github_scope_includes_workflow(path):
    scope = _github_scope(path)
    assert "workflow" in scope.split(), (
        f"{path}: github OAuth scope 缺 workflow → 含 .github/workflows 的推送被 "
        f"GitHub 拒绝（Issue #4301）。当前 scope={scope!r}")


@pytest.mark.parametrize("path", CONFIGS)
def test_github_scope_includes_repo_hook_admin(path):
    scope = _github_scope(path)
    assert "admin:repo_hook" in scope.split(), (
        f"{path}: github OAuth scope 缺 admin:repo_hook → webhook 建/改失败。"
        f"当前 scope={scope!r}")

# Note: offline_access is not pinned as "must be absent" - it only enables refresh-token issuance,
# unrelated to repo writes; add it if a future decision enables expiring tokens + refresh.
