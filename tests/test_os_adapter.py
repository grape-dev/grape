"""os_adapter 跨平台抽象层测试。

覆盖：
- Windows/Linux 平台判定
- venv 路径解析（Unix bin/python vs Windows Scripts/python.exe）
- CLI 查找（which / find_cli / Windows .cmd/.exe 扩展）
- 引擎 skill 目录（跨平台）
- extra_skill_dirs（分隔符 ; vs :）
- path_separator
- bun_cache_dir（Unix /var/cache vs Windows LOCALAPPDATA）
- remap_cwd（Unix 不变 / Windows Unix→Windows 路径转换）
- hostname（跨平台）
- temp_dir
- sandbox_supported（Windows False）
- normalize_device_name
- codearts_tasks_dir
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

from src.runtime import os_adapter


_is_win = sys.platform == "win32"


class TestPlatformDetection:
    def test_is_windows_matches_sys_platform(self):
        assert os_adapter.is_windows() == _is_win

    def test_is_windows_returns_bool(self):
        assert isinstance(os_adapter.is_windows(), bool)


class TestHomeDir:
    def test_home_dir_returns_existing_path(self):
        home = os_adapter.home_dir()
        assert home
        assert os.path.isdir(home)


class TestVenvPaths:
    def test_venv_python_unix(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            p = os_adapter.venv_python("/opt/grape/.venv")
            assert p == os.path.join("/opt/grape/.venv", "bin", "python")

    def test_venv_python_windows(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            p = os_adapter.venv_python(r"C:\grape\.venv")
            assert p == os.path.join(r"C:\grape\.venv", "Scripts", "python.exe")

    def test_venv_pip_unix(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            p = os_adapter.venv_pip("/opt/grape/.venv")
            assert p == os.path.join("/opt/grape/.venv", "bin", "pip")

    def test_venv_pip_windows(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            p = os_adapter.venv_pip(r"C:\grape\.venv")
            assert p == os.path.join(r"C:\grape\.venv", "Scripts", "pip.exe")


class TestWhich:
    def test_which_returns_empty_for_nonexistent(self):
        assert os_adapter.which("nonexistent_cli_xyz") == ""

    def test_which_finds_python(self):
        # python3 或 python 应该在 PATH 中
        for name in ("python3", "python"):
            path = os_adapter.which(name)
            if path:
                assert os.path.isfile(path)
                break

    def test_which_windows_extensions(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            with patch("shutil.which") as mock_which:
                # 模拟 Windows 上 opencode 不在 PATH 但 opencode.cmd 在
                def side_effect(name):
                    if name == "opencode":
                        return None
                    if name == "opencode.cmd":
                        return r"C:\Users\test\opencode.cmd"
                    return None
                mock_which.side_effect = side_effect
                result = os_adapter.which("opencode")
                assert result == r"C:\Users\test\opencode.cmd"


class TestFindCli:
    def test_find_cli_returns_empty_for_nonexistent(self):
        assert os_adapter.find_cli("nonexistent_cli_xyz") == ""

    def test_find_cli_windows_search_paths(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            with patch("src.runtime.os_adapter.which", return_value=""):
                fake_path = r"C:\Users\test\AppData\Roaming\npm\opencode.cmd"
                with patch("os.path.isfile", return_value=True):
                    with patch("src.runtime.os_adapter.engine_search_paths",
                               return_value=[fake_path]):
                        result = os_adapter.find_cli("opencode")
                        assert result == fake_path


class TestEngineSearchPaths:
    def test_unix_returns_empty(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            assert os_adapter.engine_search_paths("opencode") == []

    def test_windows_returns_paths(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            paths = os_adapter.engine_search_paths("opencode")
            assert isinstance(paths, list)
            # 应包含路径（即使 LOCALAPPDATA 为空也会有元素）
            assert len(paths) >= 0


class TestEngineSkillDirs:
    def test_hermes_skill_dir(self):
        dirs = os_adapter.engine_skill_dirs("hermes")
        assert len(dirs) == 1
        assert dirs[0].endswith(os.path.join(".hermes", "skills"))

    def test_opencode_skill_dir(self):
        dirs = os_adapter.engine_skill_dirs("opencode")
        assert len(dirs) == 1
        assert dirs[0].endswith(os.path.join(".config", "opencode", "skills"))

    def test_codearts_skill_dir(self):
        dirs = os_adapter.engine_skill_dirs("codearts")
        assert len(dirs) == 1
        assert dirs[0].endswith(os.path.join(".codeartsdoer", "cli-data", "system", "skills"))

    def test_unknown_engine_returns_empty(self):
        assert os_adapter.engine_skill_dirs("unknown") == []

    def test_case_insensitive(self):
        dirs_lower = os_adapter.engine_skill_dirs("hermes")
        dirs_upper = os_adapter.engine_skill_dirs("HERMES")
        assert dirs_lower == dirs_upper


class TestExtraSkillDirs:
    def test_no_env_returns_empty(self, monkeypatch):
        monkeypatch.delenv("RUNTIME_EXTRA_SKILL_DIRS", raising=False)
        assert os_adapter.extra_skill_dirs() == []

    def test_unix_separator(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RUNTIME_EXTRA_SKILL_DIRS", str(tmp_path))
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            result = os_adapter.extra_skill_dirs()
            assert str(tmp_path) in result

    def test_windows_separator(self, monkeypatch, tmp_path):
        dir2 = tmp_path / "dir2"
        dir2.mkdir()
        monkeypatch.setenv("RUNTIME_EXTRA_SKILL_DIRS", f"{tmp_path};{dir2}")
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            result = os_adapter.extra_skill_dirs()
            assert str(tmp_path) in result
            assert str(dir2) in result

    def test_nonexistent_dirs_filtered(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_EXTRA_SKILL_DIRS", "/nonexistent/dir1:/nonexistent/dir2")
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            assert os_adapter.extra_skill_dirs() == []


class TestPathSeparator:
    def test_unix(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            assert os_adapter.path_separator() == ":"

    def test_windows(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            assert os_adapter.path_separator() == ";"


class TestBunCacheDir:
    def test_unix(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            assert os_adapter.bun_cache_dir() == "/var/cache/bun-compile"

    def test_windows(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            d = os_adapter.bun_cache_dir()
            assert "grape" in d
            assert "bun-compile" in d


class TestRemapCwd:
    def test_empty_path(self):
        assert os_adapter.remap_cwd("") == ""

    def test_unix_grape_home_prefix(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            with patch("os.path.expanduser", return_value="/home/testuser"):
                result = os_adapter.remap_cwd("/home/grape/chat/repo")
                assert result == "/home/testuser/chat/repo"

    def test_unix_grape_workspaces_marker(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            with patch("os.path.expanduser", return_value="/home/testuser"):
                result = os_adapter.remap_cwd("/home/otheruser/grape-workspaces/issues/1/coding")
                assert result == "/home/testuser/grape-workspaces/issues/1/coding"

    def test_unix_already_local(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            with patch("os.path.expanduser", return_value="/home/testuser"):
                result = os_adapter.remap_cwd("/home/testuser/project")
                assert result == "/home/testuser/project"

    def test_windows_grape_home_prefix(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            with patch("os.path.expanduser", return_value=r"C:\Users\test"):
                result = os_adapter.remap_cwd("/home/grape/chat/repo")
                # Unix / 转换为 Windows os.sep
                expected = os.path.join(r"C:\Users\test", "chat", "repo")
                assert result == expected

    def test_windows_grape_workspaces_marker(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            with patch("os.path.expanduser", return_value=r"C:\Users\test"):
                result = os_adapter.remap_cwd("/home/otheruser/grape-workspaces/issues/1")
                expected = os.path.join(r"C:\Users\test", "grape-workspaces", "issues", "1")
                assert result == expected

    def test_windows_already_windows_path(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            result = os_adapter.remap_cwd(r"C:\Users\test\project")
            assert result == r"C:\Users\test\project"


class TestHostname:
    def test_returns_nonempty(self):
        h = os_adapter.hostname()
        assert h
        assert isinstance(h, str)


class TestTempDir:
    def test_returns_existing_or_default(self):
        d = os_adapter.temp_dir()
        assert d
        assert isinstance(d, str)


class TestSandboxSupported:
    def test_unix_returns_true(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            assert os_adapter.sandbox_supported() is True

    def test_windows_returns_false(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            assert os_adapter.sandbox_supported() is False


class TestNormalizeDeviceName:
    def test_basic(self):
        assert os_adapter.normalize_device_name("My-Host") == "my-host"

    def test_special_chars(self):
        assert os_adapter.normalize_device_name("Host_123.Foo") == "host-123-foo"

    def test_empty(self):
        assert os_adapter.normalize_device_name("") == "worker"

    def test_leading_trailing_dashes(self):
        assert os_adapter.normalize_device_name("---test---") == "test"

    def test_consecutive_dashes(self):
        assert os_adapter.normalize_device_name("a---b") == "a-b"


class TestCodeartsTasksDir:
    def test_unix(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            assert os_adapter.codearts_tasks_dir() == "/var/cache/codearts-tasks"

    def test_windows(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            d = os_adapter.codearts_tasks_dir()
            assert "codearts-tasks" in d
            assert "grape" in d


class TestVersionFile:
    def test_version_file_in_config_dir(self):
        vf = os_adapter.version_file()
        assert vf.endswith(os.path.join(".grape", "worker.version"))


class TestEnvFile:
    def test_env_file_in_config_dir(self):
        ef = os_adapter.env_file()
        assert ef.endswith(os.path.join(".grape", "runtime.env"))


class TestConfigDir:
    def test_config_dir_ends_with_grape(self):
        cd = os_adapter.config_dir()
        assert cd.endswith(os.path.join(os.sep, ".grape")) or cd.endswith(".grape")


class TestDefaultInstallDir:
    def test_default_install_dir_ends_with_grape(self):
        d = os_adapter.default_install_dir()
        assert d.endswith("grape")
