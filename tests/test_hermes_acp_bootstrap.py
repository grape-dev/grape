"""hermes ACP 依赖自举测试（hermes-acp-auto-bootstrap-design.md §6）。"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.runtime import acp_bootstrap as ab
from src.runtime import agent as ra


def _run_ok(returncode=0, stdout="ok", stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestAcpCheck:
    def test_check_pass(self):
        with patch("subprocess.run", return_value=_run_ok(0)) as m:
            assert ab._acp_check("/bin/hermes") is True
        assert m.call_args.args[0] == ["/bin/hermes", "acp", "--check"]

    def test_check_fail(self):
        with patch("subprocess.run", return_value=_run_ok(1)):
            assert ab._acp_check("/bin/hermes") is False

    def test_check_exception(self):
        with patch("subprocess.run", side_effect=Exception("boom")):
            assert ab._acp_check("/bin/hermes") is False


class TestHermesVenvBin:
    def test_parses_launcher_exec_line(self, tmp_path):
        launcher = tmp_path / "hermes"
        launcher.write_text('#!/usr/bin/env bash\nexec "/opt/x/.hermes/venv/bin/hermes" "$@"\n')
        # 让 os.path.isfile 对 venv/bin/hermes 返回 True
        with patch("os.path.isfile", side_effect=lambda p: str(p) == str(launcher) or p.endswith("/venv/bin/hermes")):
            assert ab._hermes_venv_bin(str(launcher)) == "/opt/x/.hermes/venv/bin"

    def test_windows_exe_unsupported(self):
        assert ab._hermes_venv_bin("C:/hermes.exe") == ""

    def test_no_exec_line(self, tmp_path):
        launcher = tmp_path / "hermes"
        launcher.write_text("#!/usr/bin/env bash\nsomething else\n")
        assert ab._hermes_venv_bin(str(launcher)) == ""


class TestEnsureHermesAcp:
    def test_check_ok_no_install(self):
        """--check 通过 → 不做任何安装。"""
        with patch.object(ab, "_acp_check", return_value=True) as chk, \
             patch.object(ab, "_pip_install_into") as pip:
            assert ab._ensure_hermes_acp("/bin/hermes") is True
        chk.assert_called_once_with("/bin/hermes")
        pip.assert_not_called()

    def test_missing_then_autopip_success(self):
        """--check 失败 → 解析 venv → 补装 → 复查通过 → True。"""
        with patch.object(ab, "_acp_check", side_effect=[False, True]), \
             patch.object(ab, "_hermes_venv_bin", return_value="/opt/x/venv/bin"), \
             patch.object(ab, "_pip_install_into", return_value=True) as pip:
            assert ab._ensure_hermes_acp("/bin/hermes") is True
        pip.assert_called_once_with("/opt/x/venv/bin")

    def test_venv_unresolvable(self):
        """launcher 解析失败 → False，不尝试 pip。"""
        with patch.object(ab, "_acp_check", return_value=False), \
             patch.object(ab, "_hermes_venv_bin", return_value=""), \
             patch.object(ab, "_pip_install_into") as pip:
            assert ab._ensure_hermes_acp("/bin/hermes") is False
        pip.assert_not_called()

    def test_pip_fails(self):
        """补装失败 → False（不重试）。"""
        with patch.object(ab, "_acp_check", return_value=False), \
             patch.object(ab, "_hermes_venv_bin", return_value="/opt/x/venv/bin"), \
             patch.object(ab, "_pip_install_into", return_value=False):
            assert ab._ensure_hermes_acp("/bin/hermes") is False

    def test_recheck_fails_after_install(self):
        """补装成功但复查仍失败 → False。"""
        with patch.object(ab, "_acp_check", side_effect=[False, False]), \
             patch.object(ab, "_hermes_venv_bin", return_value="/opt/x/venv/bin"), \
             patch.object(ab, "_pip_install_into", return_value=True):
            assert ab._ensure_hermes_acp("/bin/hermes") is False

    def test_empty_cli(self):
        assert ab._ensure_hermes_acp("") is False


class TestPipInstallInto:
    def test_pins_acp_version(self):
        with patch("subprocess.run", return_value=_run_ok(0)) as m:
            assert ab._pip_install_into("/opt/x/venv/bin") is True
        cmd = m.call_args.args[0]
        assert cmd[-2:] == ["install", "agent-client-protocol==0.9.0"]
        assert cmd[0] == "/opt/x/venv/bin/pip"

    def test_falls_back_to_python_m_pip(self):
        """pip 不存在 → python -m pip 兜底。"""
        with patch("subprocess.run", side_effect=[FileNotFoundError, _run_ok(0)]) as m:
            assert ab._pip_install_into("/opt/x/venv/bin") is True
        assert m.call_args_list[1].args[0][:2] == ["/opt/x/venv/bin/python", "-m"]

    def test_all_fail(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert ab._pip_install_into("/opt/x/venv/bin") is False


class TestDiscoverLocalRuntimesIntegration:
    def test_hermes_acp_fail_marks_unhealthy(self):
        """hermes --version 通过但 ACP 失败 → healthy=False；其他引擎不受影响。"""
        with patch("src.runtime.registry.discover_runtimes", return_value=[
                {"name": "hermes", "cli_path": "/bin/hermes", "installed": True},
                {"name": "opencode", "cli_path": "/bin/opencode", "installed": True}]), \
             patch("subprocess.run", return_value=_run_ok(0, stdout="1.0")), \
             patch("src.runtime.acp_bootstrap._ensure_hermes_acp", return_value=False):
            out = {r["name"]: r for r in ra._discover_local_runtimes()}
        assert out["hermes"]["healthy"] is False
        assert out["opencode"]["healthy"] is True

    def test_hermes_acp_ok_stays_healthy(self):
        with patch("src.runtime.registry.discover_runtimes", return_value=[
                {"name": "hermes", "cli_path": "/bin/hermes", "installed": True}]), \
             patch("subprocess.run", return_value=_run_ok(0, stdout="1.0")), \
             patch("src.runtime.acp_bootstrap._ensure_hermes_acp", return_value=True) as ens:
            out = ra._discover_local_runtimes()
        assert out[0]["healthy"] is True
        ens.assert_called_once_with("/bin/hermes")

    def test_non_hermes_engine_skips_acp(self):
        """opencode 引擎不触发 ACP 自举。"""
        with patch("src.runtime.registry.discover_runtimes", return_value=[
                {"name": "opencode", "cli_path": "/bin/opencode", "installed": True}]), \
             patch("subprocess.run", return_value=_run_ok(0, stdout="1.0")), \
             patch("src.runtime.acp_bootstrap._ensure_hermes_acp") as ens:
            ra._discover_local_runtimes()
        ens.assert_not_called()

    def test_uninstalled_hermes_skips_acp(self):
        """--version 都失败的 hermes（healthy 已 False）不再做 ACP 检查。"""
        with patch("src.runtime.registry.discover_runtimes", return_value=[
                {"name": "hermes", "cli_path": "/bin/hermes", "installed": True}]), \
             patch("subprocess.run", side_effect=FileNotFoundError), \
             patch("src.runtime.acp_bootstrap._ensure_hermes_acp") as ens:
            out = ra._discover_local_runtimes()
        assert out[0]["healthy"] is False
        ens.assert_not_called()


class TestLocalProbeHermesArgs:
    def test_hermes_probe_args_overridden(self):
        from src.runtime.local_probe import probe_local_engines
        with patch("src.runtime.local_probe._probe_one",
                   side_effect=lambda e: {**e, "version": "x", "healthy": True}) as p:
            probe_local_engines([{"name": "hermes", "cli_path": "/bin/hermes", "installed": True},
                                 {"name": "opencode", "cli_path": "/bin/opencode", "installed": True}])
        seen = {c.args[0]["name"]: c.args[0] for c in p.call_args_list}
        assert seen["hermes"]["probe_args"] == ["acp", "--check"]
        assert "probe_args" not in seen["opencode"]
