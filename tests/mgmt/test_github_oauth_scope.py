# -*- coding: utf-8 -*-
"""GitHub OAuth scope 契约：repo（列仓/读私仓）+ workflow（推 .github/workflows）。

实测（grapedev-test / gt-load-1304 私仓案例）：
scope 只有 read:user user:email 时，GitHub 对 /user/repos 返回空、
对私仓按 404 处理（不泄露存在性）——登录建号正常，但仓库页永远为空，
用户表现为「登录了却一个仓库都看不到」。

workflow 缺失（Issue #4301）：任何含 .github/workflows/*.yml 的 push 被拒
（refusing to allow ... without workflow scope），Coding Agent 只能把工作流
塞进 ci/ 并等人工补交。2026-11-25 负责人拍板把 workflow/admin:repo_hook 加回
登录渠道；此用例锁死该决定，防止下一次「最小权限」收敛把它静默删掉。
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

# 注：offline_access 不锁「必须缺」——它只影响 refresh_token 签发（长期令牌），
# 与仓库写无关；若将来某次决策要求开启 expiring tokens + 刷新链，再加不迟。