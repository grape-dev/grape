"""Issue #3580 PR #1868 复审修复的回归用例。

覆盖四项修复：
1. GitLab webhook 密钥：必须使用共享密钥，禁止回落 api_token（凭据外泄 + 投递全 404）；
2. gitlab_v4 家族强制路径 B（随机密钥），即使配置了全局 bot token；
3. remove_gitlab_webhook 上游 404 幂等；
4. _run_git 累加调用方已有 GIT_CONFIG_*，不覆盖。
"""
import json

import pytest

from src.domains.mgmt.user import repo_manager as rm
from src.domains.runtime.engine import repogit
from src.kernel import vcs_gitlab_hooks as hooks


def test_gitlab_webhook_secret_required(monkeypatch):
    seen = {}

    def fake_req(method, url, token="", body=None, timeout=30):
        seen["body"] = body
        return {"id": 11}

    monkeypatch.setattr(hooks, "_request", fake_req)

    with pytest.raises(RuntimeError, match="secret required"):
        rm.setup_gitlab_webhook("PAT-must-not-leak", "g", "r",
                                "https://gw/webhook/gitlab-corp",
                                webhook_password="",
                                api_base="https://gl.corp/api/v4")

    rid = rm.setup_gitlab_webhook("PAT-must-not-leak", "g", "r",
                                  "https://gw/webhook/gitlab-corp",
                                  webhook_password="shared-secret",
                                  api_base="https://gl.corp/api/v4")
    assert rid == 11
    assert seen["body"]["token"] == "shared-secret"
    assert seen["body"]["token"] != "PAT-must-not-leak"


def test_gitlab_family_forces_secret_path(monkeypatch):
    """get_hosting_provider_config 对 gitlab_v4 返回 bot_token='' → 调用点走路径 B。"""
    import src.domains.mgmt.user.db as _udb
    monkeypatch.setattr(_udb, "get_global_bot_token_for", lambda provider: "global-bot-token")
    cfg = rm.get_hosting_provider_config("gitlab")
    assert cfg["bot_token"] == ""


def test_remove_gitlab_webhook_404_idempotent(monkeypatch):
    def not_found(method, url, token="", body=None, timeout=30):
        raise hooks.GitLabHookError(method, url, 404, "404 Not Found")

    monkeypatch.setattr(hooks, "_request", not_found)
    assert rm.remove_gitlab_webhook("tok", "g", "r", 9, api_base="https://gl.corp/api/v4") is True

    def forbidden(method, url, token="", body=None, timeout=30):
        raise hooks.GitLabHookError(method, url, 403, "403 Forbidden")

    monkeypatch.setattr(hooks, "_request", forbidden)
    with pytest.raises(hooks.GitLabHookError):
        rm.remove_gitlab_webhook("tok", "g", "r", 9, api_base="https://gl.corp/api/v4")


def test_run_git_accumulates_git_config(monkeypatch):
    captured = {}

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, env=None, **kw):
        captured["cmd"] = cmd
        captured["env"] = env
        return _R()

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "grape")
    monkeypatch.setattr(repogit.subprocess, "run", fake_run)

    repogit._run_git(["--version"], token="t0ken", timeout=5, ca_bundle="/etc/ca.pem")

    env = captured["env"]
    assert env["GIT_CONFIG_KEY_0"] == "user.name"          # 既有项保留
    assert env["GIT_CONFIG_COUNT"] == "4"                  # 1 + 3（auth/CA/followRedirects）
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
             for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["http.sslCAInfo"] == "/etc/ca.pem"
    assert pairs["http.followRedirects"] == "false"
    assert any(k.lower() == "http.extraheader" for k in pairs)
    assert "t0ken" not in " ".join(captured["cmd"])