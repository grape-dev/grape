"""Issue #471 C2：codearts 目录信任闸门修复（无 TTY 不再致命退出）。

根因（codearts 二进制逆向实证）：folder-trust-middleware 在
settings.json（$KERNEL_DATA_DIR/settings.json）的
security.folderTrust.enabled=true 且 cwd ∉ security.trustedFolders
且 stdin 非 TTY 时 exit 1。

修复：per-task / 探测数据目录预写 settings.json，把任务 cwd 加入
trustedFolders（保留用户已有 enabled 标志与信任列表，只做合并）。
"""
import json
import os
import sys
from unittest.mock import patch

import pytest

from src.runtime import codearts as rc
from src.runtime import agent as ra


class TestWriteTrustSettings:
    def test_creates_settings_trusting_dirs(self, tmp_path):
        data_dir = tmp_path / "cli-data"
        cwd = str(tmp_path / "task-cwd")
        rc.write_trust_settings(str(data_dir), [cwd])
        settings = json.loads((data_dir / "settings.json").read_text())
        assert settings["security"]["folderTrust"]["enabled"] is True
        assert cwd in settings["security"]["trustedFolders"]

    def test_merges_existing_settings(self, tmp_path):
        data_dir = tmp_path / "cli-data"
        data_dir.mkdir()
        (data_dir / "settings.json").write_text(json.dumps({
            "security": {"folderTrust": {"enabled": True},
                         "trustedFolders": ["/home/alice/project"]},
            "other": 1,
        }))
        rc.write_trust_settings(str(data_dir), ["/srv/grape/task-1"])
        settings = json.loads((data_dir / "settings.json").read_text())
        assert "/home/alice/project" in settings["security"]["trustedFolders"]
        assert "/srv/grape/task-1" in settings["security"]["trustedFolders"]
        assert settings["other"] == 1

    def test_enabled_flag_not_forced_true(self, tmp_path):
        """已显式 enabled=false 的配置不被强制打开（闸门关闭本身就没有致命退出）。"""
        data_dir = tmp_path / "cli-data"
        data_dir.mkdir()
        (data_dir / "settings.json").write_text(json.dumps({
            "security": {"folderTrust": {"enabled": False}, "trustedFolders": []}}))
        rc.write_trust_settings(str(data_dir), ["/x"])
        settings = json.loads((data_dir / "settings.json").read_text())
        assert settings["security"]["folderTrust"]["enabled"] is False
        assert "/x" in settings["security"]["trustedFolders"]


class TestPrepareCodeartsDataDir:
    def test_per_task_dir_contains_trust_settings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEARTS_TASKS_DIR", str(tmp_path / "tasks"))
        cwd = str(tmp_path / "worktree")
        os.makedirs(cwd, exist_ok=True)
        cli_data = ra._prepare_codearts_data_dir("t-471", cwd)
        assert cli_data.startswith(str(tmp_path / "tasks"))
        settings = json.loads(open(os.path.join(cli_data, "settings.json")).read())
        assert cwd in settings["security"]["trustedFolders"]
        # 既有权限预写不回归
        perm = os.path.join(cli_data, "storage", "permission", "global.json")
        assert os.path.exists(perm)
        sandbox = os.path.join(cli_data, "storage", "sandbox", "config.json")
        assert json.loads(open(sandbox).read())["network_policy"] == "allow_all"

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEARTS_TASKS_DIR", str(tmp_path / "tasks"))
        cwd = str(tmp_path / "wt")
        os.makedirs(cwd, exist_ok=True)
        d1 = ra._prepare_codearts_data_dir("t-471b", cwd)
        d2 = ra._prepare_codearts_data_dir("t-471b", cwd)
        assert d1 == d2
        settings = json.loads(open(os.path.join(d1, "settings.json")).read())
        assert settings["security"]["trustedFolders"].count(cwd) == 1


class TestCodeartsProbeEnv:
    def test_probe_env_uses_isolated_data_dir_with_trust(self, tmp_path):
        with patch.dict(os.environ, {}, clear=False):
            env = rc.probe_env(str(tmp_path))
        data_dir = env["KERNEL_DATA_DIR"]
        assert data_dir  # 指向专用探测数据目录，不污染宿主配置
        settings = json.loads(open(os.path.join(data_dir, "settings.json")).read())
        assert str(tmp_path) in settings["security"]["trustedFolders"]
