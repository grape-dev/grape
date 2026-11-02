"""hermes ACP 依赖自举测试（hermes-acp-auto-bootstrap-design.md §6 / Issue #613）。

覆盖三断点修复场景（mock subprocess / Popen）：
1. 真实 ACP 握手探测替代 `acp --check`（含失败原因分类）；
2. venv 解析兼容 bash shim / 软链 realpath / console-script shebang 反推；
3. ACP 依赖版本不硬钉 0.9.0（已装版本兼容判断 + 无版本号补装）。
"""
import json
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


class _FakeProc:
    """模拟 `hermes acp` 子进程：可选首行 stdout 响应与退出 stderr。"""

    def __init__(self, first_line="", stderr="", delay=0.0):
        self.stdin = MagicMock()
        self.stdout = MagicMock()
        self.stderr = MagicMock()
        self.stdout.readline = MagicMock(return_value=first_line)
        self.stderr.read = MagicMock(return_value=stderr)
        self.stdin.write = MagicMock()
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class TestAcpProbe:
    def test_handshake_jsonrpc_response_pass(self):
        resp = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}) + "\n"
        with patch("subprocess.Popen", return_value=_FakeProc(first_line=resp)) as m:
            ok, reason = ab._acp_probe("/bin/hermes")
        assert ok is True and reason == ab.ACP_OK
        assert m.call_args.args[0] == ["/bin/hermes", "acp"]
        # 握手请求包含 initialize + session/new 两条 JSON-RPC
        sent = m.return_value.stdin.write.call_args.args[0]
        assert "initialize" in sent and "session/new" in sent

    def test_handshake_non_jsonrpc_output_fail(self):
        with patch("subprocess.Popen", return_value=_FakeProc(first_line="garbage\n")):
            assert ab._acp_probe("/bin/hermes") == (False, ab.ACP_FAIL)

    def test_unknown_subcommand_classified(self):
        proc = _FakeProc(stderr="hermes: error: unrecognized arguments: --check\n")
        with patch("subprocess.Popen", return_value=proc):
            ok, reason = ab._acp_probe("/bin/hermes")
        assert ok is False and reason == ab.ACP_NO_SUBCOMMAND

    def test_deps_missing_classified(self):
        proc = _FakeProc(stderr="ModuleNotFoundError: No module named 'agent_client_protocol'\n")
        with patch("subprocess.Popen", return_value=proc):
            ok, reason = ab._acp_probe("/bin/hermes")
        assert ok is False and reason == ab.ACP_DEPS_MISSING

    def test_timeout_fail(self):
        import threading
        proc = _FakeProc()
        proc.stdout.readline = MagicMock(
            side_effect=lambda: (threading.Event().wait(60) or ""))
        with patch("subprocess.Popen", return_value=proc), \
             patch.object(ab, "ACP_CHECK_TIMEOUT", 0.1):
            ok, reason = ab._acp_probe("/bin/hermes")
        assert ok is False and reason == ab.ACP_FAIL
        assert proc.killed is True

    def test_popen_exception_fail(self):
        with patch("subprocess.Popen", side_effect=Exception("boom")):
            assert ab._acp_probe("/bin/hermes") == (False, ab.ACP_FAIL)

    def test_acp_check_wrapper(self):
        with patch.object(ab, "_acp_probe", return_value=(True, ab.ACP_OK)):
            assert ab._acp_check("/bin/hermes") is True
        with patch.object(ab, "_acp_probe", return_value=(False, ab.ACP_FAIL)):
            assert ab._acp_check("/bin/hermes") is False


class TestHermesVenvBin:
    def test_parses_launcher_exec_line(self, tmp_path):
        launcher = tmp_path / "hermes"
        launcher.write_text('#!/usr/bin/env bash\nexec "/opt/x/.hermes/venv/bin/hermes" "$@"\n')
        # 让 os.path.isfile 对 venv/bin/hermes 返回 True
        with patch("os.path.isfile", side_effect=lambda p: str(p) == str(launcher) or p.endswith("/venv/bin/hermes")):
            assert ab._hermes_venv_bin(str(launcher)) == "/opt/x/.hermes/venv/bin"

    def test_symlink_realpath_console_script(self, tmp_path):
        """软链 → venv/bin/hermes console-script：realpath + shebang 反推 venv/bin。"""
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("")          # 占位解释器
        launcher = venv_bin / "hermes"
        launcher.write_text(f"#!{venv_bin}/python\n# EASY-INSTALL-ENTRY-SCRIPT\nprint('x')\n")
        link = tmp_path / "hermes-link"
        link.symlink_to(launcher)
        with patch("os.path.isfile", side_effect=lambda p: str(p) in (str(link), str(venv_bin / "python"))):
            assert ab._hermes_venv_bin(str(link)) == str(venv_bin)

    def test_console_script_shebang_direct(self, tmp_path):
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("")
        launcher = venv_bin / "hermes"
        launcher.write_text(f"#!{venv_bin}/python\nimport sys\n")
        with patch("os.path.isfile", side_effect=lambda p: str(p) in (str(launcher), str(venv_bin / "python"))):
            assert ab._hermes_venv_bin(str(launcher)) == str(venv_bin)

    def test_env_shebang_not_resolved(self, tmp_path):
        """`#!/usr/bin/env python` 形态 shebang 无法定位 venv → 空串。"""
        launcher = tmp_path / "hermes"
        launcher.write_text("#!/usr/bin/env python\nimport sys\n")
        assert ab._hermes_venv_bin(str(launcher)) == ""

    def test_windows_exe_unsupported(self):
        assert ab._hermes_venv_bin("C:/hermes.exe") == ""

    def test_no_match(self, tmp_path):
        launcher = tmp_path / "hermes"
        launcher.write_text("#!/usr/bin/env bash\nsomething else\n")
        assert ab._hermes_venv_bin(str(launcher)) == ""


class TestInstalledAcpVersion:
    def test_returns_version(self):
        with patch("subprocess.run", return_value=_run_ok(0, stdout="0.12.1\n")) as m:
            assert ab._installed_acp_version("/opt/x/venv/bin") == "0.12.1"
        assert m.call_args.args[0][0] == "/opt/x/venv/bin/python"

    def test_not_installed_empty(self):
        with patch("subprocess.run", return_value=_run_ok(1, stderr="not found")):
            assert ab._installed_acp_version("/opt/x/venv/bin") == ""

    def test_exception_empty(self):
        with patch("subprocess.run", side_effect=Exception("boom")):
            assert ab._installed_acp_version("/opt/x/venv/bin") == ""


class TestEnsureHermesAcp:
    def test_probe_ok_no_install(self):
        """握手通过 → 不做任何安装。"""
        with patch.object(ab, "_acp_probe", return_value=(True, ab.ACP_OK)) as chk, \
             patch.object(ab, "_pip_install_into") as pip:
            assert ab._ensure_hermes_acp("/bin/hermes") is True
        chk.assert_called_once_with("/bin/hermes")
        pip.assert_not_called()

    def test_missing_then_autopip_success(self):
        """依赖缺失 → 解析 venv → 补装 → 复查通过 → True。"""
        with patch.object(ab, "_acp_probe", side_effect=[(False, ab.ACP_DEPS_MISSING), (True, ab.ACP_OK)]), \
             patch.object(ab, "_hermes_venv_bin", return_value="/opt/x/venv/bin"), \
             patch.object(ab, "_installed_acp_version", return_value=""), \
             patch.object(ab, "_pip_install_into", return_value=True) as pip:
            assert ab._ensure_hermes_acp("/bin/hermes") is True
        pip.assert_called_once_with("/opt/x/venv/bin")

    def test_no_subcommand_no_autopip(self):
        """hermes 版本过旧无 acp 子命令 → False，不误报依赖缺失、不补装。"""
        with patch.object(ab, "_acp_probe", return_value=(False, ab.ACP_NO_SUBCOMMAND)) as probe, \
             patch.object(ab, "_hermes_venv_bin") as venv, \
             patch.object(ab, "_pip_install_into") as pip:
            assert ab._ensure_hermes_acp("/bin/hermes") is False
        assert probe.call_count == 1
        venv.assert_not_called()
        pip.assert_not_called()

    def test_installed_version_incompatible_no_autopip(self):
        """ACP 已装（如 0.12.x）但握手失败 → 适配器不兼容 → 不盲目补装。"""
        with patch.object(ab, "_acp_probe", return_value=(False, ab.ACP_DEPS_MISSING)), \
             patch.object(ab, "_hermes_venv_bin", return_value="/opt/x/venv/bin"), \
             patch.object(ab, "_installed_acp_version", return_value="0.12.1") as ver, \
             patch.object(ab, "_pip_install_into") as pip:
            assert ab._ensure_hermes_acp("/bin/hermes") is False
        ver.assert_called_once_with("/opt/x/venv/bin")
        pip.assert_not_called()

    def test_venv_unresolvable(self):
        """launcher 解析失败 → False，不尝试 pip。"""
        with patch.object(ab, "_acp_probe", return_value=(False, ab.ACP_DEPS_MISSING)), \
             patch.object(ab, "_hermes_venv_bin", return_value=""), \
             patch.object(ab, "_pip_install_into") as pip:
            assert ab._ensure_hermes_acp("/bin/hermes") is False
        pip.assert_not_called()

    def test_pip_fails(self):
        """补装失败 → False（不重试）。"""
        with patch.object(ab, "_acp_probe", return_value=(False, ab.ACP_DEPS_MISSING)), \
             patch.object(ab, "_hermes_venv_bin", return_value="/opt/x/venv/bin"), \
             patch.object(ab, "_installed_acp_version", return_value=""), \
             patch.object(ab, "_pip_install_into", return_value=False):
            assert ab._ensure_hermes_acp("/bin/hermes") is False

    def test_recheck_fails_after_install(self):
        """补装成功但复查仍失败 → False。"""
        with patch.object(ab, "_acp_probe", side_effect=[(False, ab.ACP_DEPS_MISSING), (False, ab.ACP_FAIL)]), \
             patch.object(ab, "_hermes_venv_bin", return_value="/opt/x/venv/bin"), \
             patch.object(ab, "_installed_acp_version", return_value=""), \
             patch.object(ab, "_pip_install_into", return_value=True):
            assert ab._ensure_hermes_acp("/bin/hermes") is False

    def test_empty_cli(self):
        assert ab._ensure_hermes_acp("") is False

    def test_exception_swallowed(self):
        with patch.object(ab, "_acp_probe", side_effect=Exception("boom")):
            assert ab._ensure_hermes_acp("/bin/hermes") is False


class TestPipInstallInto:
    def test_installs_unpinned_acp(self):
        """不再硬钉 agent-client-protocol==0.9.0，安装无版本约束。"""
        with patch("subprocess.run", return_value=_run_ok(0)) as m:
            assert ab._pip_install_into("/opt/x/venv/bin") is True
        cmd = m.call_args.args[0]
        assert cmd[-2:] == ["install", "agent-client-protocol"]
        assert "==" not in cmd[-1]
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
    def test_hermes_marked_acp_probe(self):
        from src.runtime.local_probe import probe_local_engines
        with patch("src.runtime.local_probe._probe_one",
                   side_effect=lambda e: {**e, "version": "x", "healthy": True}) as p:
            probe_local_engines([{"name": "hermes", "cli_path": "/bin/hermes", "installed": True},
                                 {"name": "opencode", "cli_path": "/bin/opencode", "installed": True}])
        seen = {c.args[0]["name"]: c.args[0] for c in p.call_args_list}
        assert seen["hermes"]["acp_probe"] is True
        assert "acp_probe" not in seen["opencode"]

    def test_probe_one_hermes_uses_handshake(self):
        """hermes 探测：版本走 --version，健康走真实 ACP 握手。"""
        from src.runtime.local_probe import _probe_one
        with patch("subprocess.run", return_value=_run_ok(0, stdout="0.9.0")), \
             patch("src.runtime.acp_bootstrap._acp_check", return_value=True) as chk:
            out = _probe_one({"name": "hermes", "cli_path": "/bin/hermes",
                              "installed": True, "acp_probe": True})
        assert out["healthy"] is True
        assert out["version"] == "0.9.0"
        chk.assert_called_once_with("/bin/hermes")

    def test_probe_one_non_hermes_version_only(self):
        from src.runtime.local_probe import _probe_one
        with patch("subprocess.run", return_value=_run_ok(0, stdout="1.0")) as m:
            out = _probe_one({"name": "opencode", "cli_path": "/bin/opencode", "installed": True})
        assert out["healthy"] is True
        assert m.call_args.args[0] == ["/bin/opencode", "--version"]
