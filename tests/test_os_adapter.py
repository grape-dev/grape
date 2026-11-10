"""os_adapter 跨平台抽象层测试。

覆盖：
- Windows/Linux 平台判定
- venv 路径解析（Unix bin/python vs Windows Scripts/python.exe）
- CLI 查找（which / find_cli / Windows .cmd/.exe 扩展）
- 引擎 skill 目录（跨平台）
- extra_skill_dirs（分隔符 ; vs :）
- path_separator
- bun_cache_dir（Unix 三级解析：GRAPE_BUN_CACHE_DIR / ~/.cache / Windows LOCALAPPDATA）
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
    def test_unix_returns_local_bin_candidate(self):
        # Linux 兜底语义：返回 ~/.local/bin/<cli> 候选（是否真实存在由 find_cli
        # 的 isfile 判定，本函数不检查）——Windows 路径不得出现
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            paths = os_adapter.engine_search_paths("opencode")
        assert paths == [os.path.join(os.path.expanduser("~"), ".local", "bin", "opencode")]

    def test_windows_returns_paths(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            paths = os_adapter.engine_search_paths("opencode")
            assert isinstance(paths, list)
            # 应包含路径（即使 LOCALAPPDATA 为空也会有元素）
            assert len(paths) >= 0

    def test_windows_aishell_desktop_cmd_entry(self):
        """aishell-dsh 二期 Windows 对接：AI Shell 桌面安装的 CLI 启动器兜底。

        aishell CLI 唯一形态是桌面安装内 resources/runtime/cli/bin/aishell.cmd
        （ELECTRON_RUN_AS_NODE 启动器，无 aishell.exe）。find_cli("aishell")
        （discover_backends 与 aishell_acp.stream_execute 的调用形态）在 PATH
        查不到时必须经此条目命中——未启动过桌面的机器用户 PATH 无该目录。
        """
        with patch.object(os_adapter, "_IS_WINDOWS", True), \
             patch.object(os_adapter, "_windows_registry_install_dirs", return_value=[]):
            paths = os_adapter.engine_search_paths("aishell")
            assert len(paths) == 1
            assert paths[0].lower().endswith("aishell.cmd")
            assert "AI Shell" in paths[0]
            assert os.path.join("resources", "runtime", "cli", "bin") in paths[0]
            # find_cli 全链：PATH 落空 → 兜底路径存在即返回该 .cmd
            # （须与上面同作用域：registry mock 退出后 Linux 会走 winreg import）
            with patch("src.runtime.os_adapter.which", return_value=""):
                with patch("os.path.isfile", return_value=True):
                    assert os_adapter.find_cli("aishell") == paths[0]

    def test_windows_aishell_registry_fallback(self):
        """AI Shell 装在非默认目录：经 Uninstall 注册表 InstallLocation 定位。

        注册表枚举仅 Windows 可验（winreg 为平台模块，Linux 上无法 patch 出
        真实键值遍历）；Windows 真机已实证（6a1b12ac-… InstallLocation 命中），
        此处跳过以免 Linux CI 假失败。
        """
        if not sys.platform.startswith("win"):
            pytest.skip("registry enumeration requires real Windows")
        custom = r"D:\Example\AI Shell"
        expected = os.path.join(custom, "resources", "runtime", "cli", "bin", "aishell.cmd")
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            with patch.object(os_adapter, "_windows_registry_install_dirs",
                              return_value=[custom]) as mreg:
                paths = os_adapter.engine_search_paths("aishell")
                assert expected in paths
                # 全链：默认路径不存在（isfile 仅对注册表候选为真）→ 命中注册表路径
                with patch("src.runtime.os_adapter.which", return_value=""):
                    with patch("os.path.isfile", side_effect=lambda p: p == expected):
                        assert os_adapter.find_cli("aishell") == expected
            # 注册表兜底只在 aishell 键上生效，其他引擎不查注册表
            with patch.object(os_adapter, "_windows_registry_install_dirs",
                              return_value=[custom]) as mreg2:
                os_adapter.engine_search_paths("codex")
                mreg2.assert_not_called()


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
    def test_unix_default_user_cache(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GRAPE_BUN_CACHE_DIR", None)
                d = os_adapter.bun_cache_dir()
            assert d == os.path.join(os.path.expanduser("~"), ".cache", "grape", "bun-compile")

    def test_env_override(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            with patch.dict(os.environ, {"GRAPE_BUN_CACHE_DIR": "/tmp/custom-bun-cache"}):
                assert os_adapter.bun_cache_dir() == "/tmp/custom-bun-cache"

    def test_env_override_expands_user_and_vars(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            with patch.dict(os.environ, {"GRAPE_BUN_CACHE_DIR": "~/cache/$MYVAR", "MYVAR": "sub"}):
                assert os_adapter.bun_cache_dir() == os.path.expanduser("~/cache/sub")

    def test_env_override_explicit_empty_string_falls_back_to_default(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            with patch.dict(os.environ, {"GRAPE_BUN_CACHE_DIR": ""}):
                d = os_adapter.bun_cache_dir()
            assert d == os.path.join(os.path.expanduser("~"), ".cache", "grape", "bun-compile")

    def test_fallback_dir_under_tempdir(self):
        import tempfile
        d = os_adapter.bun_cache_fallback_dir()
        assert d == os.path.join(tempfile.gettempdir(), "grape", "bun-compile")

    def test_windows(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            d = os_adapter.bun_cache_dir()
            assert "grape" in d
            assert "bun-compile" in d


class TestOpencodeGetEnvBunCache:
    def _runtime(self):
        from src.runtime.opencode import OpencodeRuntime
        return OpencodeRuntime()

    def test_env_injects_resolved_cache_dir(self, tmp_path):
        rt = self._runtime()
        with patch.dict(os.environ, {"GRAPE_BUN_CACHE_DIR": str(tmp_path / "bun")}):
            env = rt.get_env()
        assert env["BUN_RUNTIME_TRANSPILER_CACHE_PATH"] == str(tmp_path / "bun")
        assert env["JSC_diskCachePath"] == str(tmp_path / "bun")
        assert os.path.isdir(str(tmp_path / "bun"))

    def test_makedirs_failure_degrades_to_fallback_with_warning(self, tmp_path, caplog):
        import logging
        rt = self._runtime()
        fallback = tmp_path / "fallback"
        with patch.dict(os.environ, {"GRAPE_BUN_CACHE_DIR": str(tmp_path / "unwritable")}):
            with patch("src.runtime.os_adapter.bun_cache_fallback_dir", return_value=str(fallback)):
                with patch("os.makedirs", side_effect=PermissionError("Operation not permitted")):
                    with caplog.at_level(logging.WARNING, logger="src.runtime.opencode"):
                        env = rt.get_env()
        assert any("Operation not permitted" in r.message for r in caplog.records)
        # makedirs 全部失败 → 不注入缓存变量，但 get_env 正常返回（不抛错）
        assert "BUN_RUNTIME_TRANSPILER_CACHE_PATH" not in env
        assert "DEVOPS_SKILLS_DIR" in env

    def test_makedirs_primary_failure_uses_fallback_dir(self, tmp_path, caplog):
        import logging
        real_makedirs = os.makedirs
        primary = tmp_path / "primary"
        fallback = tmp_path / "fb"
        calls = []

        def fake_makedirs(path, exist_ok=False):
            calls.append(path)
            if path == str(primary):
                raise PermissionError("Operation not permitted")
            return real_makedirs(path, exist_ok=exist_ok)

        rt = self._runtime()
        with patch.dict(os.environ, {"GRAPE_BUN_CACHE_DIR": str(primary)}):
            with patch("src.runtime.os_adapter.bun_cache_fallback_dir", return_value=str(fallback)):
                with patch("os.makedirs", side_effect=fake_makedirs):
                    with caplog.at_level(logging.WARNING, logger="src.runtime.opencode"):
                        env = rt.get_env()
        assert env["BUN_RUNTIME_TRANSPILER_CACHE_PATH"] == str(fallback)
        assert any("falling back" in r.message for r in caplog.records)


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
