"""Tests for Windows worker support (Issue #201).

验证 OS 路径抽象层、跨平台引擎探测、install.ps1 端点、
skill 目录路径跨平台解析、cwd 路径映射等。
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault("GITCODE_API_TOKEN", "test_token")


class TestOsAdapter:
    """src/runtime/os_adapter.py 路径抽象层。"""

    def test_is_windows_returns_bool(self):
        from src.runtime.os_adapter import is_windows
        assert isinstance(is_windows(), bool)

    def test_home_dir_returns_nonempty(self):
        from src.runtime.os_adapter import home_dir
        result = home_dir()
        assert result and isinstance(result, str)

    def test_config_dir_contains_grape(self):
        from src.runtime.os_adapter import config_dir
        result = config_dir()
        assert ".grape" in result

    def test_env_file_ends_with_runtime_env(self):
        from src.runtime.os_adapter import env_file
        result = env_file()
        assert result.endswith("runtime.env")

    def test_version_file_ends_with_worker_version(self):
        from src.runtime.os_adapter import version_file
        result = version_file()
        assert result.endswith("worker.version")

    def test_venv_python_unix(self):
        from src.runtime.os_adapter import venv_python
        with patch("src.runtime.os_adapter._IS_WINDOWS", False):
            result = venv_python("/home/user/grape/.venv")
            assert result == os.path.join("/home/user/grape/.venv", "bin", "python")

    def test_venv_python_windows(self):
        from src.runtime.os_adapter import venv_python
        with patch("src.runtime.os_adapter._IS_WINDOWS", True):
            result = venv_python(r"C:\Users\user\grape\.venv")
            assert "Scripts" in result
            assert result.endswith("python.exe")

    def test_venv_pip_unix(self):
        from src.runtime.os_adapter import venv_pip
        with patch("src.runtime.os_adapter._IS_WINDOWS", False):
            result = venv_pip("/home/user/grape/.venv")
            assert result == os.path.join("/home/user/grape/.venv", "bin", "pip")

    def test_venv_pip_windows(self):
        from src.runtime.os_adapter import venv_pip
        with patch("src.runtime.os_adapter._IS_WINDOWS", True):
            result = venv_pip(r"C:\Users\user\grape\.venv")
            assert "Scripts" in result
            assert result.endswith("pip.exe")

    def test_venv_bin_dir_unix(self):
        from src.runtime.os_adapter import venv_bin_dir
        with patch("src.runtime.os_adapter._IS_WINDOWS", False):
            result = venv_bin_dir("/home/user/grape/.venv")
            assert result == os.path.join("/home/user/grape/.venv", "bin")

    def test_venv_bin_dir_windows(self):
        from src.runtime.os_adapter import venv_bin_dir
        with patch("src.runtime.os_adapter._IS_WINDOWS", True):
            result = venv_bin_dir(r"C:\Users\user\grape\.venv")
            assert "Scripts" in result

    def test_default_install_dir(self):
        from src.runtime.os_adapter import default_install_dir
        result = default_install_dir()
        assert "grape" in result

    def test_path_separator_unix(self):
        from src.runtime.os_adapter import path_separator
        with patch("src.runtime.os_adapter._IS_WINDOWS", False):
            assert path_separator() == ":"

    def test_path_separator_windows(self):
        from src.runtime.os_adapter import path_separator
        with patch("src.runtime.os_adapter._IS_WINDOWS", True):
            assert path_separator() == ";"

    def test_engine_skill_dirs_hermes(self):
        from src.runtime.os_adapter import engine_skill_dirs
        dirs = engine_skill_dirs("hermes")
        assert len(dirs) == 1
        assert ".hermes" in dirs[0]
        assert "skills" in dirs[0]

    def test_engine_skill_dirs_opencode(self):
        from src.runtime.os_adapter import engine_skill_dirs
        dirs = engine_skill_dirs("opencode")
        assert len(dirs) == 1
        assert "opencode" in dirs[0]
        assert "skills" in dirs[0]

    def test_engine_skill_dirs_codearts(self):
        from src.runtime.os_adapter import engine_skill_dirs
        dirs = engine_skill_dirs("codearts")
        assert len(dirs) == 1
        assert ".codeartsdoer" in dirs[0]

    def test_engine_skill_dirs_unknown_engine(self):
        from src.runtime.os_adapter import engine_skill_dirs
        dirs = engine_skill_dirs("unknown")
        assert dirs == []

    def test_extra_skill_dirs_empty(self):
        from src.runtime.os_adapter import extra_skill_dirs
        with patch.dict(os.environ, {"RUNTIME_EXTRA_SKILL_DIRS": ""}):
            assert extra_skill_dirs() == []

    def test_extra_skill_dirs_unix_separator(self):
        from src.runtime.os_adapter import extra_skill_dirs
        with patch("src.runtime.os_adapter._IS_WINDOWS", False), \
             patch.dict(os.environ, {"RUNTIME_EXTRA_SKILL_DIRS": "/path/a:/path/b"}):
            with patch("os.path.isdir", return_value=True):
                result = extra_skill_dirs()
                assert len(result) == 2

    def test_extra_skill_dirs_windows_separator(self):
        from src.runtime.os_adapter import extra_skill_dirs
        with patch("src.runtime.os_adapter._IS_WINDOWS", True), \
             patch.dict(os.environ, {"RUNTIME_EXTRA_SKILL_DIRS": r"C:\path\a;C:\path\b"}):
            with patch("os.path.isdir", return_value=True):
                result = extra_skill_dirs()
                assert len(result) == 2

    def test_find_cli_returns_string(self):
        from src.runtime.os_adapter import find_cli
        result = find_cli("python")
        assert isinstance(result, str)

    def test_find_cli_not_found(self):
        from src.runtime.os_adapter import find_cli
        result = find_cli("nonexistent-cli-xyz-12345")
        assert result == ""

    def test_bun_cache_dir_unix(self):
        from src.runtime.os_adapter import bun_cache_dir
        with patch("src.runtime.os_adapter._IS_WINDOWS", False):
            result = bun_cache_dir()
            assert result == "/var/cache/bun-compile"

    def test_bun_cache_dir_windows(self):
        from src.runtime.os_adapter import bun_cache_dir
        with patch("src.runtime.os_adapter._IS_WINDOWS", True):
            result = bun_cache_dir()
            assert "bun-compile" in result

    def test_sandbox_supported_unix(self):
        from src.runtime.os_adapter import sandbox_supported
        with patch("src.runtime.os_adapter._IS_WINDOWS", False):
            assert sandbox_supported() is True

    def test_sandbox_supported_windows(self):
        from src.runtime.os_adapter import sandbox_supported
        with patch("src.runtime.os_adapter._IS_WINDOWS", True):
            assert sandbox_supported() is False

    def test_supports_process_group_unix(self):
        from src.runtime.os_adapter import supports_process_group
        with patch("src.runtime.os_adapter._IS_WINDOWS", False):
            assert supports_process_group() is True

    def test_supports_process_group_windows(self):
        from src.runtime.os_adapter import supports_process_group
        with patch("src.runtime.os_adapter._IS_WINDOWS", True):
            assert supports_process_group() is False

    def test_remap_cwd_unix_home_grape_remapped(self):
        """Unix 上 /home/grape 前缀应映射到本地 home（如果不同）。"""
        from src.runtime.os_adapter import remap_cwd
        with patch("src.runtime.os_adapter._IS_WINDOWS", False):
            with patch("src.runtime.os_adapter.home_dir", return_value="/home/testuser"):
                result = remap_cwd("/home/grape/chat/test")
                assert result == "/home/testuser/chat/test"

    def test_remap_cwd_unix_already_local(self):
        """Unix 上已是本地 home 路径则不变。"""
        from src.runtime.os_adapter import remap_cwd
        with patch("src.runtime.os_adapter._IS_WINDOWS", False):
            local_home = __import__("os").path.expanduser("~")
            result = remap_cwd(local_home + "/chat/test")
            assert result == local_home + "/chat/test"

    def test_remap_cwd_windows_maps_home_grape(self):
        from src.runtime.os_adapter import remap_cwd
        with patch("src.runtime.os_adapter._IS_WINDOWS", True):
            with patch("src.runtime.os_adapter.home_dir", return_value=r"C:\Users\test"):
                result = remap_cwd("/home/grape/chat/test")
                assert result.startswith(r"C:\Users\test")
                assert "chat/test" in result or r"chat\test" in result

    def test_remap_cwd_windows_maps_grape_workspaces(self):
        from src.runtime.os_adapter import remap_cwd
        with patch("src.runtime.os_adapter._IS_WINDOWS", True):
            with patch("src.runtime.os_adapter.home_dir", return_value=r"C:\Users\test"):
                result = remap_cwd("/home/someuser/grape-workspaces/issue/1")
                assert result.startswith(r"C:\Users\test")
                assert "grape-workspaces" in result

    def test_remap_cwd_empty(self):
        from src.runtime.os_adapter import remap_cwd
        assert remap_cwd("") == ""

    def test_normalize_device_name(self):
        from src.runtime.os_adapter import normalize_device_name
        assert normalize_device_name("My-PC-01") == "my-pc-01"
        assert normalize_device_name("DESKTOP_ABC") == "desktop-abc"

    def test_hostname_returns_nonempty(self):
        from src.runtime.os_adapter import hostname
        result = hostname()
        assert result and isinstance(result, str)


class TestDiscoverBackends:
    """discover_backends() 跨平台引擎探测。"""

    def test_discover_backends_returns_list(self):
        from src.agent.executor import discover_backends
        result = discover_backends()
        assert isinstance(result, list)

    def test_discover_backends_contains_four_engines(self):
        from src.agent.executor import discover_backends
        result = discover_backends()
        names = [r["name"] for r in result]
        for engine in ("opencode", "codearts", "hermes", "codex"):
            assert engine in names, f"{engine} missing from discover_backends result"

    def test_discover_backends_has_required_fields(self):
        from src.agent.executor import discover_backends
        result = discover_backends()
        for r in result:
            assert "name" in r
            assert "installed" in r
            assert "cli_path" in r
            assert "pip_package" in r

    def test_discover_backends_installed_is_bool(self):
        from src.agent.executor import discover_backends
        result = discover_backends()
        for r in result:
            assert isinstance(r["installed"], bool)

    def test_discover_backends_cli_path_is_string(self):
        from src.agent.executor import discover_backends
        result = discover_backends()
        for r in result:
            assert isinstance(r["cli_path"], str)


class TestInstallPs1Endpoint:
    """/api/runtime/install.ps1 端点。"""

    def test_install_ps1_script_exists(self):
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "install-worker.ps1"
        )
        assert os.path.isfile(script_path), "install-worker.ps1 not found"

    def test_install_ps1_contains_param_block(self):
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "install-worker.ps1"
        )
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "param(" in content
        assert "-Token" in content or "-token" in content

    def test_install_ps1_contains_download_logic(self):
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "install-worker.ps1"
        )
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "Invoke-WebRequest" in content or "grape-worker.tar.gz" in content

    def test_install_ps1_contains_venv_creation(self):
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "install-worker.ps1"
        )
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "venv" in content

    def test_install_ps1_contains_config_writing(self):
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "install-worker.ps1"
        )
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "RUNTIME_TOKEN" in content
        assert "runtime.env" in content

    def test_install_ps1_contains_sandbox_degradation_notice(self):
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "install-worker.ps1"
        )
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "沙箱" in content or "sandbox" in content.lower()

    def test_start_worker_ps1_exists(self):
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "start-worker.ps1"
        )
        assert os.path.isfile(script_path), "start-worker.ps1 not found"

    def test_start_worker_ps1_contains_daemon_loop(self):
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "start-worker.ps1"
        )
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "MaxRestarts" in content or "while" in content
        assert "RestartDelay" in content

    def test_start_worker_ps1_contains_env_loading(self):
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "start-worker.ps1"
        )
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "runtime.env" in content

    def test_start_worker_ps1_uses_windows_python_path(self):
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "start-worker.ps1"
        )
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "Scripts" in content
        assert "python.exe" in content


class TestScanLocalSkills:
    """_scan_local_skills() 跨平台 skill 目录解析。"""

    def test_scan_local_skills_returns_list(self):
        from src.runtime.agent import _scan_local_skills
        result = _scan_local_skills()
        assert isinstance(result, list)

    def test_scan_local_skills_uses_os_adapter(self):
        import inspect
        from src.runtime.agent import _scan_local_skills
        source = inspect.getsource(_scan_local_skills)
        assert "engine_skill_dirs" in source or "extra_skill_dirs" in source


class TestRemapCwd:
    """_remap_cwd() 跨平台路径映射。"""

    def test_remap_cwd_empty(self):
        from src.runtime.agent import _remap_cwd
        assert _remap_cwd("") == ""

    def test_remap_cwd_with_mapping(self):
        from src.runtime.agent import _remap_cwd
        with patch.dict(os.environ, {"RUNTIME_CWD_PREFIX_MAP": "/home/grape=/home/lvjuntao"}):
            result = _remap_cwd("/home/grape/chat/test")
            assert result == "/home/lvjuntao/chat/test"

    def test_remap_cwd_mapping_not_matching(self):
        from src.runtime.agent import _remap_cwd
        with patch.dict(os.environ, {"RUNTIME_CWD_PREFIX_MAP": "/home/grape=/home/lvjuntao"}):
            result = _remap_cwd("/opt/data/test")
            assert result == "/opt/data/test"


class TestOpencodeEnvWindows:
    """opencode 引擎 get_env() 跨平台缓存目录。"""

    def test_opencode_get_env_uses_bun_cache_dir(self):
        import inspect
        from src.runtime.opencode import OpencodeRuntime
        source = inspect.getsource(OpencodeRuntime.get_env)
        assert "bun_cache_dir" in source

    def test_opencode_get_env_returns_cache(self):
        from src.runtime.opencode import OpencodeRuntime
        rt = OpencodeRuntime()
        env = rt.get_env()
        assert "BUN_RUNTIME_TRANSPILER_CACHE_PATH" in env
        assert "JSC_diskCachePath" in env
        assert env["BUN_RUNTIME_TRANSPILER_CACHE_PATH"] == env["JSC_diskCachePath"]


class TestCodeArtsEnvWindows:
    """codearts 引擎 get_env() 跨平台路径。"""

    def test_codearts_get_env_uses_home_dir(self):
        import inspect
        from src.runtime.codearts import CodeArtsRuntime
        source = inspect.getsource(CodeArtsRuntime.get_env)
        assert "home_dir" in source
