"""git 传输凭据按平台派生 S1–S5（评审②）。

修复前所有 fetch/push/ls-remote 都用默认 x-access-token：
  gitee 要求 Basic <令牌账号>:<token>；gitlab 要求 oauth2；github/gitcode 用 x-access-token。
用例抓 git 组装出的 Authorization 头，逐平台断言。
"""
from __future__ import annotations

import base64

import pytest

from src.domains.runtime.engine import repogit as rg


@pytest.fixture()
def capture(monkeypatch):
    seen = {}

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(cmd, **kw):
        seen["cmd"] = cmd
        return _R()

    monkeypatch.setattr(rg.subprocess, "run", _run)
    return seen


def _auth_header(seen) -> str:
    for i, a in enumerate(seen["cmd"]):
        if a.startswith("http.extraheader="):
            return a.split("Authorization: ", 1)[1]
    raise AssertionError(f"no header in {seen['cmd']}")


def _decoded(seen) -> str:
    scheme, b64 = _auth_header(seen).split(" ", 1)
    assert scheme == "Basic"
    return base64.b64decode(b64).decode()


def test_s1_gitee_uses_basic_password_with_owner(capture):
    rg._run_git(["fetch"], token="tok", repo_key="gitee:acme/widget")
    assert _decoded(capture) == "acme:tok"


def test_s2_gitlab_uses_oauth2(capture):
    rg._run_git(["fetch"], token="tok", repo_key="gitlab:acme/widget")
    assert _decoded(capture).endswith(":tok")
    assert _decoded(capture).startswith("oauth2")


def test_s3_github_keeps_x_access_token(capture):
    rg._run_git(["push"], token="tok", repo_key="github:acme/widget")
    assert _decoded(capture) == "x-access-token:tok"


def test_s4_without_repo_key_keeps_the_legacy_default(capture):
    rg._run_git(["fetch"], token="tok")
    assert _decoded(capture) == "x-access-token:tok"


def test_s5_explicit_scheme_still_wins(capture):
    rg._run_git(["fetch"], token="tok", repo_key="gitee:acme/widget", scheme="x_access_token")
    assert _decoded(capture) == "x-access-token:tok"


def test_s6_network_call_sites_pass_the_repository_key():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "src/domains/runtime"
    checks = {
        "engine/repogit.py": "ls-remote",
        "engine/branch_guard.py": "repo_key=repo_key",
        "pipeline/event_pipeline.py": "repo_key=_rk",
    }
    for rel, needle in checks.items():
        assert needle in (root / rel).read_text(encoding="utf-8"), rel
