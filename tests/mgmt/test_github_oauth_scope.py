# -*- coding: utf-8 -*-
"""GitHub OAuth scope 必须含 repo（列仓/读私仓依赖）。

实测（grapedev-test / gt-load-1304 私仓案例）：
scope 只有 read:user user:email 时，GitHub 对 /user/repos 返回空、
对私仓按 404 处理（不泄露存在性）——登录建号正常，但仓库页永远为空，
用户表现为「登录了却一个仓库都看不到」。
"""


def test_github_scope_includes_repo():
    import json
    from pathlib import Path

    cfg = json.loads(
        Path("configs/oauth-providers.json").read_text(encoding="utf-8"))
    gh = cfg.get("github") or {}
    scope = gh.get("scope", "")
    assert "repo" in scope.split(), (
        f"github OAuth scope 缺 repo → 登录后 /user/repos 列不出私仓，"
        f"仓库页空。当前 scope={scope!r}")
