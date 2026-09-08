"""Issue-45 M4：grape git 本地透传（方案③ token 零暴露）。

C3/C4 关键验收：token 不进 git argv；工作区无 token 文件；stdout 无 token。
"""
import json
import os
import sys
import uuid

import pytest
from click.testing import CliRunner

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "cli"))

from grape_cli import config as cli_config
from grape_cli.cli import cli

SPAWNED = []


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    cli_config.CONFIG_DIR = tmp_path / ".grape"
    cli_config.CONFIG_FILE = cli_config.CONFIG_DIR / "config.json"
    monkeypatch.setenv("GRAPE_BASE_URL", "http://mock")
    monkeypatch.setenv("GRAPE_API_TOKEN", "grape_secret_123")
    SPAWNED.clear()
    cli._loaded = False
    cli._groups = {}
    yield
    cli._loaded = False
    cli._groups = {}


@pytest.fixture
def fake_api(monkeypatch):
    from grape_cli.client import ApiClient
    monkeypatch.setenv("PATH", r"C:\Windows\System32" + os.pathsep + os.environ.get("PATH", ""))
    import unittest.mock

    def _fake_request(self, method, path, **kw):
        if path.endswith("/workspace"):
            return 200, {"workspace": r"C:\ws\owner\repo"}
        if path.endswith("/git-credential"):
            return 200, {"provider": "gitcode", "username": "x-access-token",
                         "password": "platinum_secret", "expires_in": 600}
        return 200, {}

    monkeypatch.setattr(ApiClient, "request", _fake_request)


@pytest.fixture
def fake_git(monkeypatch):
    """拦截 subprocess.run（git 不存在环境），记录 argv/env。"""
    import subprocess
    from grape_cli.commands import git as git_mod

    def _fake_run(cmd, env=None, cwd=None):
        SPAWNED.append({"argv": list(cmd), "env": dict(env or {}), "cwd": cwd})
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(git_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(subprocess, "run", _fake_run)


def test_git_passthrough_and_repo_resolution(fake_api, fake_git):
    runner = CliRunner()
    r = runner.invoke(cli, ["git", "--repo", "owner/repo", "log", "--oneline", "-5"])
    assert r.exit_code == 0, r.output
    sp = SPAWNED[0]
    assert sp["argv"] == ["git", "log", "--oneline", "-5"]
    assert sp["cwd"] == r"C:\ws\owner\repo"


def test_c3_token_not_in_argv(fake_api, fake_git):
    import base64
    runner = CliRunner()
    r = runner.invoke(cli, ["git", "--repo", "owner/repo", "push"])
    assert r.exit_code == 0, r.output
    argv = " ".join(SPAWNED[0]["argv"])
    assert "secret" not in argv and "platinum_secret" not in argv
    # token 只经 GIT_CONFIG env 注入（base64 编码）
    env = SPAWNED[0]["env"]
    assert env.get("GIT_CONFIG_COUNT") == "1"
    header = env.get("GIT_CONFIG_VALUE_0", "")
    assert header.startswith("Authorization: Basic ")
    decoded = base64.b64decode(header.split(" ", 2)[2]).decode()
    assert "platinum_secret" in decoded


def test_c4_token_not_on_disk_or_stdout(fake_api, fake_git, tmp_path):
    runner = CliRunner()
    r = runner.invoke(cli, ["git", "--repo", "owner/repo", "status"])
    assert r.exit_code == 0
    assert "platinum_secret" not in r.output
    # 工作区无 token 文件（git 由 CLI 子进程执行，CLI 未写任何文件）
    leaked = [str(p) for p in tmp_path.rglob("*") if "secret" in str(p)]
    assert leaked == []


def test_git_no_repo_passthrough_in_cwd(fake_git):
    runner = CliRunner()
    r = runner.invoke(cli, ["git", "status"])
    assert r.exit_code == 0, r.output
    assert SPAWNED[0]["argv"] == ["git", "status"]
    assert SPAWNED[0]["cwd"] is None