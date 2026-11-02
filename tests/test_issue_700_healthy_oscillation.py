"""Issue #700：Windows hermes 引擎 healthy 震荡回归测试。

覆盖三块：
1. register 降级守卫（src/api/routes/runtime.py）：
   - 已 healthy 且探活新鲜（probe_status='ok' 且 last probe 在新鲜窗口内）的行，
     re-register 上报 healthy=False 时 DB 不降级（防震荡核心）；
   - 探活过期 / 不新鲜 → 降级仍生效（不掩盖真实故障）；
   - 心跳连败 2 次真实降 0（守卫不掩盖心跳路径）；
   - re-register 上报 True → 无条件升 1（守卫不误伤升路径）；
   - 新引擎首次注册 healthy=False → INSERT 不受守卫影响。
2. ACP_CHECK_TIMEOUT env 覆盖（src/runtime/acp_bootstrap.py）：
   - 默认 ≥20；RUNTIME_ACP_CHECK_TIMEOUT 可覆盖；非法值回退默认。
3. _hermes_venv_bin Windows 启动器解析（src/runtime/acp_bootstrap.py）：
   - .cmd（绝对路径引号形态 / @py -3 形态）/ .bat / .exe 返回有效 venv bin；
   - 解析失败返回空串（保持契约）。
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_credential(name: str = ""):
    name = name or f"cred-700-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status, last_seen_at) "
            "VALUES (?, ?, ?, 'global', '', 'active', ?)",
            (f"hash_{name}", name, f"dev-{name}", _now_iso()))
        cid = c.lastrowid
        db.commit()
    return cid


def _insert_runtime(cid: int, name: str = "", healthy: int = 1,
                    probe_status: str = "ok", probe_fail_count: int = 0,
                    probe_last: str = ""):
    name = name or f"engine-700-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, installed, scope, last_seen_at, "
            "probe_fail_count, probe_status, probe_last) "
            "VALUES (?, ?, 'online', ?, 1, 'global', ?, ?, ?, ?)",
            (cid, name, healthy, _now_iso(), probe_fail_count, probe_status, probe_last))
        rid = c.lastrowid
        db.commit()
    return rid, name


def _cleanup(cid: int):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def _runtime_state(name: str, cid: int):
    """返回 (healthy, probe_status, probe_fail_count, probe_last)。"""
    with db_lock:
        row = db.cursor().execute(
            "SELECT healthy, probe_status, probe_fail_count, probe_last "
            "FROM agent_runtimes WHERE name=? AND credential_id=?",
            (name, cid)).fetchone()
    return tuple(row) if row else None


class _FakeRequest:
    headers = {}
    client = None


def _do_register(runtimes: list, cid: int, device_name: str = "dev"):
    """直接调用 runtime_register（绕过 HTTP 鉴权，credential_id=cid）。"""
    from src.api.routes.runtime import runtime_register, RuntimeRegBody
    body = RuntimeRegBody(device_name=device_name, runtimes=runtimes)
    return runtime_register(body, _FakeRequest(), credential_id=cid)


def _do_heartbeat(probe_results: dict, runtime_ids: list, cid: int):
    """直接调用 runtime_heartbeat（同 test_issue_490 先例）。"""
    from src.api.routes.runtime import runtime_heartbeat, HeartbeatBody
    body = HeartbeatBody(runtime_ids=runtime_ids, probe_results=probe_results)
    return runtime_heartbeat(body, _FakeRequest(), credential_id=cid)


def _mk_engine(name: str, healthy: bool) -> dict:
    return {"name": name, "display_name": name, "version": "1.0",
            "healthy": healthy, "cli_path": "/bin/x", "installed": True}


# ============================ 切片 1：register 降级守卫 ============================

class TestRegisterDegradationGuard:
    """re-register 不得把「已 healthy 且探活新鲜」的行打回 0（#700 震荡根因）。"""

    def test_fresh_ok_probe_not_degraded_by_reregister_false(self):
        """已 healthy + probe ok + 探活新鲜 → re-register 上报 False 不降级。"""
        cid = _insert_credential()
        fresh = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        rid, name = _insert_runtime(cid, healthy=1, probe_status="ok",
                                    probe_fail_count=0, probe_last=fresh)
        try:
            _do_register([_mk_engine(name, healthy=False)], cid)
            state = _runtime_state(name, cid)
            assert state[0] == 1, (
                f"探活新鲜的 healthy 行不应被 re-register 打回 0, got healthy={state[0]}")
        finally:
            _cleanup(cid)

    def test_stale_probe_still_degraded_by_reregister_false(self):
        """探活过期（超过新鲜窗口）→ 降级仍生效（不掩盖真实故障）。"""
        cid = _insert_credential()
        stale = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
        rid, name = _insert_runtime(cid, healthy=1, probe_status="ok",
                                    probe_fail_count=0, probe_last=stale)
        try:
            _do_register([_mk_engine(name, healthy=False)], cid)
            state = _runtime_state(name, cid)
            assert state[0] == 0, "探活过期的行应正常降级"
        finally:
            _cleanup(cid)

    def test_probe_fail_status_still_degraded(self):
        """probe_status 非 ok（如 fail）→ 不守卫，正常降级。"""
        cid = _insert_credential()
        fresh = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        rid, name = _insert_runtime(cid, healthy=1, probe_status="fail",
                                    probe_fail_count=1, probe_last=fresh)
        try:
            _do_register([_mk_engine(name, healthy=False)], cid)
            assert _runtime_state(name, cid)[0] == 0
        finally:
            _cleanup(cid)

    def test_reregister_true_still_upgrades(self):
        """上报 True → 无条件升 1（守卫不误伤升路径，心跳升回语义保留）。"""
        cid = _insert_credential()
        rid, name = _insert_runtime(cid, healthy=0, probe_status="fail",
                                    probe_fail_count=2, probe_last="")
        try:
            _do_register([_mk_engine(name, healthy=True)], cid)
            assert _runtime_state(name, cid)[0] == 1
        finally:
            _cleanup(cid)

    def test_heartbeat_consecutive_failures_still_degrade(self):
        """心跳连败 2 次真实降 0（守卫不掩盖心跳路径——#700 验收红线）。"""
        cid = _insert_credential()
        rid, name = _insert_runtime(cid, healthy=1, probe_status="ok",
                                    probe_fail_count=0, probe_last="")
        try:
            _do_heartbeat({name: {"status": "fail", "latency": 5.0,
                                  "last_probe": "2026-11-02T09:00:00+00:00",
                                  "error": "t1"}}, [rid], cid)
            _do_heartbeat({name: {"status": "fail", "latency": 5.0,
                                  "last_probe": "2026-11-02T09:10:00+00:00",
                                  "error": "t2"}}, [rid], cid)
            state = _runtime_state(name, cid)
            assert state[0] == 0, f"连败 2 次应降 0, got healthy={state[0]}"
            assert state[2] == 2
        finally:
            _cleanup(cid)

    def test_new_engine_insert_unaffected(self):
        """新引擎首次注册 healthy=False → INSERT 分支不受守卫影响。"""
        cid = _insert_credential()
        name = f"engine-700-new-{uuid.uuid4().hex[:8]}"
        try:
            _do_register([_mk_engine(name, healthy=False)], cid)
            state = _runtime_state(name, cid)
            assert state is not None
            assert state[0] == 0
        finally:
            _cleanup(cid)


# ============================ 切片 2：ACP_CHECK_TIMEOUT 覆盖 ============================

class TestAcpCheckTimeoutOverride:
    """ACP_CHECK_TIMEOUT 默认 ≥20s，RUNTIME_ACP_CHECK_TIMEOUT 可覆盖。"""

    def test_default_at_least_20s(self):
        import src.runtime.acp_bootstrap as ab
        assert ab.ACP_CHECK_TIMEOUT >= 20, (
            f"默认应 ≥20s（Windows 冷启动余量）, got {ab.ACP_CHECK_TIMEOUT}")

    def test_env_override(self, monkeypatch):
        import src.runtime.acp_bootstrap as ab
        monkeypatch.setenv("RUNTIME_ACP_CHECK_TIMEOUT", "30")
        assert ab._resolve_acp_check_timeout() == 30

    def test_env_override_float_rejected(self, monkeypatch):
        """非正整数（浮点串）→ 回退默认。"""
        import src.runtime.acp_bootstrap as ab
        monkeypatch.setenv("RUNTIME_ACP_CHECK_TIMEOUT", "3.5")
        assert ab._resolve_acp_check_timeout() == ab.ACP_CHECK_TIMEOUT_DEFAULT

    def test_env_override_garbage_rejected(self, monkeypatch):
        import src.runtime.acp_bootstrap as ab
        monkeypatch.setenv("RUNTIME_ACP_CHECK_TIMEOUT", "abc")
        assert ab._resolve_acp_check_timeout() == ab.ACP_CHECK_TIMEOUT_DEFAULT

    def test_env_override_nonpositive_rejected(self, monkeypatch):
        import src.runtime.acp_bootstrap as ab
        monkeypatch.setenv("RUNTIME_ACP_CHECK_TIMEOUT", "0")
        assert ab._resolve_acp_check_timeout() == ab.ACP_CHECK_TIMEOUT_DEFAULT
        monkeypatch.setenv("RUNTIME_ACP_CHECK_TIMEOUT", "-5")
        assert ab._resolve_acp_check_timeout() == ab.ACP_CHECK_TIMEOUT_DEFAULT


# ============================ 切片 3：Windows 启动器解析 ============================

class TestHermesVenvBinLinux:
    """_hermes_venv_bin 对 Linux 常见 launcher 形态返回有效 venv bin（Linux 自愈对称补齐）。

    #837 只修了 Windows 侧（.exe/.cmd/.bat）；Linux 侧 pip 安装的 console-script
    默认 shebang 为 `#!/usr/bin/env python*`、bash shim 有不带引号的 exec 形态、
    pipx/pdm 用 `python -m` 形态——此前全部解析失败返回空串，缺依赖时自动补装失效。
    """

    def test_env_shebang_launcher(self, tmp_path, monkeypatch):
        """`#!/usr/bin/env python3` shebang（pip console-script 最常见默认形态）。"""
        import src.runtime.acp_bootstrap as ab
        venv = tmp_path / "venv" / "bin"
        venv.mkdir(parents=True)
        py = venv / "python3"
        py.write_bytes(b"")
        launcher = tmp_path / "hermes"
        launcher.write_text("#!/usr/bin/env python3\nimport hermes\n", encoding="utf-8")
        launcher.chmod(0o755)
        # env 解析: which('python3') → venv python（模拟 PATH 前置 venv）
        monkeypatch.setattr(ab.shutil, "which", lambda name: str(py) if name == "python3" else None)
        out = ab._hermes_venv_bin(str(launcher))
        assert out, f"env-shebang launcher 应解析出 venv bin, got ''（launcher={launcher}）"
        assert os.path.isfile(os.path.join(out, "python3"))

    def test_env_shebang_launcher_system_python(self, tmp_path, monkeypatch):
        """env shebang 解析到系统 python（非 venv）——仍返回其目录（bin 目录本身可作
        pip 安装目标；与 Windows py-launcher 解析到 base python 的行为对齐）。"""
        import src.runtime.acp_bootstrap as ab
        sysdir = tmp_path / "sys" / "bin"
        sysdir.mkdir(parents=True)
        py = sysdir / "python3"
        py.write_bytes(b"")
        launcher = tmp_path / "hermes"
        launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        monkeypatch.setattr(ab.shutil, "which", lambda name: str(py) if name == "python3" else None)
        out = ab._hermes_venv_bin(str(launcher))
        assert out == str(sysdir)

    def test_bash_shim_exec_unquoted(self, tmp_path):
        """bash shim `exec /path/bin/hermes "$@"`（不带引号）。"""
        import src.runtime.acp_bootstrap as ab
        venv = tmp_path / "venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "hermes").write_bytes(b"")
        launcher = tmp_path / "shim"
        launcher.write_text(f'#!/usr/bin/env bash\nexec {venv}/hermes "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
        out = ab._hermes_venv_bin(str(launcher))
        assert out, f"不带引号 exec shim 应解析出 venv bin, got ''"
        assert os.path.isfile(os.path.join(out, "hermes"))

    def test_bash_shim_python_dash_m(self, tmp_path):
        """`exec ".../venv/bin/python" -m hermes` 形态（pipx/pdm 常见）。"""
        import src.runtime.acp_bootstrap as ab
        venv = tmp_path / "venv" / "bin"
        venv.mkdir(parents=True)
        py = venv / "python"
        py.write_bytes(b"")
        launcher = tmp_path / "shim"
        launcher.write_text(f'#!/usr/bin/env bash\nexec "{py}" -m hermes "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
        out = ab._hermes_venv_bin(str(launcher))
        assert out, f"python -m shim 应解析出 venv bin, got ''"
        assert os.path.isfile(os.path.join(out, "python"))

class TestHermesVenvBinWindows:
    """_hermes_venv_bin 对 .exe/.cmd/.bat 返回有效 venv bin（Windows 自愈）。"""

    def test_cmd_absolute_python_quoted(self, tmp_path):
        """.cmd 内 @\"...\\Scripts\\python.exe\" 形态（venv 模板标准引号形态）。"""
        import src.runtime.acp_bootstrap as ab
        venv = tmp_path / "venv" / "Scripts"
        venv.mkdir(parents=True)
        (venv / "python.exe").write_bytes(b"")
        py = str(venv / "python.exe")
        launcher = tmp_path / "hermes.cmd"
        launcher.write_text(f'@"{py}" "C:\\x\\hermes_script.py" %*\n', encoding="utf-8")
        out = ab._hermes_venv_bin(str(launcher))
        assert out, f"应解析出 venv bin, got ''（launcher={launcher}）"
        assert os.path.basename(os.path.normpath(out)).lower() == "scripts"
        assert os.path.isfile(os.path.join(out, "python.exe"))

    def test_cmd_absolute_python_unquoted(self, tmp_path):
        import src.runtime.acp_bootstrap as ab
        venv = tmp_path / "venv" / "Scripts"
        venv.mkdir(parents=True)
        (venv / "python.exe").write_bytes(b"")
        py = str(venv / "python.exe")
        launcher = tmp_path / "hermes.cmd"
        launcher.write_text(f"@{py} C:\\x\\hermes_script.py %*\n", encoding="utf-8")
        out = ab._hermes_venv_bin(str(launcher))
        assert out and os.path.isfile(os.path.join(out, "python.exe"))

    def test_cmd_py_launcher_form(self, tmp_path, monkeypatch):
        """@py -3 形态：py launcher 解析出解释器，再由其定位 venv Scripts。"""
        import src.runtime.acp_bootstrap as ab
        venv = tmp_path / "venv" / "Scripts"
        venv.mkdir(parents=True)
        (venv / "python.exe").write_bytes(b"")
        launcher = venv / "hermes.cmd"
        launcher.write_text("@py -3 C:\\x\\hermes_script.py %*\n", encoding="utf-8")
        # monkeypatch 子进程定位：py -3 -c ... 输出 venv python 路径
        class _FakeCP:
            returncode = 0
            stdout = str(venv / "python.exe")
            stderr = ""
        def _fake_run(cmd, **kw):
            return _FakeCP()
        monkeypatch.setattr(ab.subprocess, "run", _fake_run)
        out = ab._hermes_venv_bin(str(launcher))
        assert out and os.path.isfile(os.path.join(out, "python.exe"))

    def test_bat_absolute_python(self, tmp_path):
        import src.runtime.acp_bootstrap as ab
        venv = tmp_path / "venv" / "Scripts"
        venv.mkdir(parents=True)
        (venv / "python.exe").write_bytes(b"")
        py = str(venv / "python.exe")
        launcher = tmp_path / "hermes.bat"
        launcher.write_text(f'@"{py}" "C:\\x\\hermes_script.py" %*\n', encoding="utf-8")
        out = ab._hermes_venv_bin(str(launcher))
        assert out and os.path.isfile(os.path.join(out, "python.exe"))

    def test_exe_launcher(self, tmp_path):
        """.exe：launcher 所在目录即 venv Scripts（python.exe 同目录校验）。"""
        import src.runtime.acp_bootstrap as ab
        venv = tmp_path / "venv" / "Scripts"
        venv.mkdir(parents=True)
        (venv / "python.exe").write_bytes(b"")
        launcher = venv / "hermes.exe"
        launcher.write_bytes(b"MZ fake exe")
        out = ab._hermes_venv_bin(str(launcher))
        assert out and os.path.isfile(os.path.join(out, "python.exe"))

    def test_exe_without_python_returns_empty(self, tmp_path):
        import src.runtime.acp_bootstrap as ab
        d = tmp_path / "Scripts2"
        d.mkdir(parents=True)
        launcher = d / "hermes.exe"
        launcher.write_bytes(b"MZ fake exe")
        assert ab._hermes_venv_bin(str(launcher)) == ""

    def test_garbage_cmd_returns_empty(self, tmp_path):
        """乱内容 .cmd（无 python 行）→ 空串（回退人工修复指引契约）。"""
        import src.runtime.acp_bootstrap as ab
        launcher = tmp_path / "hermes.cmd"
        launcher.write_text("@echo off\nrem nothing here\n", encoding="utf-8")
        assert ab._hermes_venv_bin(str(launcher)) == ""

    def test_venv_python_windows_scripts_form(self, tmp_path):
        """_venv_python：Windows Scripts 目录（只含 python.exe）返回 python.exe。"""
        import src.runtime.acp_bootstrap as ab
        venv = tmp_path / "venv" / "Scripts"
        venv.mkdir(parents=True)
        (venv / "python.exe").write_bytes(b"")
        out = ab._venv_python(str(venv))
        assert os.path.basename(out) == "python.exe", f"应选 python.exe, got {out}"

    def test_venv_python_unix_bin_form(self, tmp_path):
        """_venv_python：Unix bin 目录（只含 python）返回 python（回归）。"""
        import src.runtime.acp_bootstrap as ab
        venv = tmp_path / "venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        out = ab._venv_python(str(venv))
        assert os.path.basename(out) == "python"

    def test_linux_bash_shim_regression(self, tmp_path):
        """回归：Linux bash shim exec 行解析不受 Windows 分支影响。"""
        import src.runtime.acp_bootstrap as ab
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "hermes").write_text("#!/bin/sh\n", encoding="utf-8")
        launcher = tmp_path / "hermes"
        launcher.write_text(
            "#!/usr/bin/env bash\n"
            f'exec "{venv_bin / "hermes"}" "$@"\n', encoding="utf-8")
        out = ab._hermes_venv_bin(str(launcher))
        assert out and os.path.basename(out) == "bin"


# ============================ 切片 4：py launcher 解析（#837 Required-1 / Suggested-3） ============================

class _FakeRunResult:
    """subprocess.CompletedProcess 桩。"""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestResolvePyLauncherPython:
    """_resolve_py_launcher_python 必须优先 sys.executable（Windows base python 无 _base_executable）。"""

    def test_prefers_sys_executable_over_base(self, monkeypatch):
        """注入的 py -3 代码串优先 sys.executable：base python 下不再 AttributeError。"""
        import src.runtime.acp_bootstrap as ab
        captured = {}

        def _fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return _FakeRunResult(stdout="C:/venv/Scripts/python.exe\n")

        monkeypatch.setattr(ab.subprocess, "run", _fake_run)
        out = ab._resolve_py_launcher_python()
        assert out == "C:/venv/Scripts/python.exe"
        code = captured["cmd"][-1]
        assert "sys.executable or getattr(sys, '_base_executable', '')" in code, (
            f"应优先 sys.executable, got code: {code}")
        assert "sys._base_executable or" not in code

    def test_returns_empty_on_failure(self, monkeypatch):
        import src.runtime.acp_bootstrap as ab

        def _fake_run(cmd, **kw):
            return _FakeRunResult(returncode=1, stderr="py: not found")
        monkeypatch.setattr(ab.subprocess, "run", _fake_run)
        assert ab._resolve_py_launcher_python() == ""

    def test_cli_dir_dead_param_removed(self):
        """cli_dir 死参数已移除（#837 Suggested-3）：无参调用即可。"""
        import inspect
        import src.runtime.acp_bootstrap as ab
        assert len(inspect.signature(ab._resolve_py_launcher_python).parameters) == 0


# ============================ 切片 5：.cmd 包装器 %~dp0 形态 + 同目录兜底（#837 Required-2） ============================

class TestCmdDp0Wrapper:
    """pip/distlib 生成的 .cmd 包装器 `%~dp0\\python.exe` 相对路径形态。"""

    def test_cmd_python_candidates_dp0_expansion(self, tmp_path):
        """%~dp0 展开为 launcher 所在目录。"""
        import src.runtime.acp_bootstrap as ab
        cands = ab._cmd_python_candidates(
            '"%~dp0\\python.exe" "%~dp0\\hermes-script.py" %*\n', str(tmp_path))
        expected = os.path.join(str(tmp_path), "python.exe").replace("\\", "/")
        assert cands == [expected]

    def test_hermes_venv_bin_dp0_form(self, tmp_path):
        """复现 #837 场景：Scripts 目录含 python.exe、launcher 为 %~dp0 形态
        → _hermes_venv_bin 必须返回 venv Scripts（此前返回空串）。"""
        import src.runtime.acp_bootstrap as ab
        script_dir = tmp_path / "venv" / "Scripts"
        script_dir.mkdir(parents=True)
        (script_dir / "python.exe").write_bytes(b"")
        (script_dir.parent / "pyvenv.cfg").write_text("home = C:\\Python313\n", encoding="utf-8")
        launcher = script_dir / "hermes.cmd"
        launcher.write_text(
            '"%~dp0\\python.exe" "%~dp0\\hermes-script.py" %*\n', encoding="utf-8")
        out = ab._hermes_venv_bin(str(launcher))
        assert out, "%~dp0 形态 .cmd 应解析出 venv bin, got ''"
        assert os.path.normpath(out) == os.path.normpath(str(script_dir))
        assert os.path.isfile(os.path.join(out, "python.exe"))

    def test_hermes_venv_bin_dp0_unquoted(self, tmp_path):
        """无引号 @%~dp0\\python.exe 形态（distlib 旧模板）。"""
        import src.runtime.acp_bootstrap as ab
        script_dir = tmp_path / "venv" / "Scripts"
        script_dir.mkdir(parents=True)
        (script_dir / "python.exe").write_bytes(b"")
        launcher = script_dir / "hermes.cmd"
        launcher.write_text('@%~dp0\\python.exe "%~dp0\\hermes-script.py" %*\n', encoding="utf-8")
        out = ab._hermes_venv_bin(str(launcher))
        assert out and os.path.isfile(os.path.join(out, "python.exe"))

    def test_hermes_venv_bin_same_dir_fallback(self, tmp_path):
        """内容解析失败 + launcher 同目录 python.exe → 返回该目录（与 .exe 分支一致）。"""
        import src.runtime.acp_bootstrap as ab
        script_dir = tmp_path / "venv" / "Scripts"
        script_dir.mkdir(parents=True)
        (script_dir / "python.exe").write_bytes(b"")
        launcher = script_dir / "hermes.cmd"
        launcher.write_text("@echo off\nrem no python line\n", encoding="utf-8")
        out = ab._hermes_venv_bin(str(launcher))
        assert out, "同目录 python.exe 兜底应生效, got ''"
        assert os.path.normpath(out) == os.path.normpath(str(script_dir))

    def test_hermes_venv_bin_py_form_remote_venv(self, tmp_path, monkeypatch):
        """@py -3 形态 + launcher 无同目录 python.exe → 仍走 py launcher 子进程定位。"""
        import src.runtime.acp_bootstrap as ab
        venv = tmp_path / "venv" / "Scripts"
        venv.mkdir(parents=True)
        (venv / "python.exe").write_bytes(b"")
        (venv.parent / "pyvenv.cfg").write_text("home = C:\\Python313\n", encoding="utf-8")
        launcher = tmp_path / "hermes.cmd"  # 不在 venv Scripts 内，无法走同目录兜底
        launcher.write_text("@py -3 C:\\x\\hermes_script.py %*\n", encoding="utf-8")

        class _FakeCP:
            returncode = 0
            stdout = str(venv / "python.exe")
            stderr = ""

        def _fake_run(cmd, **kw):
            return _FakeCP()
        monkeypatch.setattr(ab.subprocess, "run", _fake_run)
        out = ab._hermes_venv_bin(str(launcher))
        assert out and os.path.isfile(os.path.join(out, "python.exe"))


# ============================ 切片 6：_pyvenv_scripts_dir Unix bin / isdir 修正（#837 Suggested-2） ============================

class TestPyvenvScriptsDirFix:
    def test_unix_venv_bin_dir(self, tmp_path):
        """Unix venv（pyvenv.cfg + bin/）：返回 venv/bin（bin 是目录，按 isdir 判定）。"""
        import src.runtime.acp_bootstrap as ab
        venv = tmp_path / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
        (venv / "bin" / "python").write_text("#!/usr/bin/python3\n", encoding="utf-8")
        out = ab._pyvenv_scripts_dir(str(venv / "bin" / "python"))
        assert out and os.path.basename(out) == "bin"


# ============================ 切片 7：register 守卫新鲜窗口 env 校验（#837 Suggested-1） ============================

class TestProbeFreshWindowValidation:
    """RUNTIME_PROBE_INTERVAL / RUNTIME_PROBE_FRESH_WINDOW 非法值不得静默失效守卫。"""

    def test_default_window(self, monkeypatch):
        import src.api.routes.runtime as rt
        monkeypatch.delenv("RUNTIME_PROBE_INTERVAL", raising=False)
        monkeypatch.delenv("RUNTIME_PROBE_FRESH_WINDOW", raising=False)
        assert rt._resolve_probe_fresh_window() == 1200

    def test_interval_override(self, monkeypatch):
        import src.api.routes.runtime as rt
        monkeypatch.setenv("RUNTIME_PROBE_INTERVAL", "300")
        monkeypatch.delenv("RUNTIME_PROBE_FRESH_WINDOW", raising=False)
        assert rt._resolve_probe_fresh_window() == 600

    def test_fresh_window_override(self, monkeypatch):
        import src.api.routes.runtime as rt
        monkeypatch.delenv("RUNTIME_PROBE_INTERVAL", raising=False)
        monkeypatch.setenv("RUNTIME_PROBE_FRESH_WINDOW", "60")
        assert rt._resolve_probe_fresh_window() == 60

    def test_interval_garbage_falls_back(self, monkeypatch):
        import src.api.routes.runtime as rt
        monkeypatch.setenv("RUNTIME_PROBE_INTERVAL", "abc")
        monkeypatch.delenv("RUNTIME_PROBE_FRESH_WINDOW", raising=False)
        assert rt._resolve_probe_fresh_window() == 1200

    def test_interval_zero_falls_back(self, monkeypatch):
        import src.api.routes.runtime as rt
        monkeypatch.setenv("RUNTIME_PROBE_INTERVAL", "0")
        monkeypatch.delenv("RUNTIME_PROBE_FRESH_WINDOW", raising=False)
        assert rt._resolve_probe_fresh_window() == 1200

    def test_fresh_window_zero_clamped(self, monkeypatch):
        """0/负数不再静默禁用守卫：钳制回退默认。"""
        import src.api.routes.runtime as rt
        monkeypatch.delenv("RUNTIME_PROBE_INTERVAL", raising=False)
        monkeypatch.setenv("RUNTIME_PROBE_FRESH_WINDOW", "0")
        assert rt._resolve_probe_fresh_window() == 1200
        monkeypatch.setenv("RUNTIME_PROBE_FRESH_WINDOW", "-5")
        assert rt._resolve_probe_fresh_window() == 1200

    def test_fresh_window_garbage_falls_back(self, monkeypatch):
        import src.api.routes.runtime as rt
        monkeypatch.setenv("RUNTIME_PROBE_FRESH_WINDOW", "abc")
        assert rt._resolve_probe_fresh_window() == 1200

    def test_zero_fresh_window_guard_still_works(self, monkeypatch):
        """集成：RUNTIME_PROBE_FRESH_WINDOW=0 时守卫仍生效（探活新鲜不降级）。"""
        import src.api.routes.runtime as rt
        monkeypatch.setenv("RUNTIME_PROBE_FRESH_WINDOW", "0")
        cid = _insert_credential()
        fresh = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        rid, name = _insert_runtime(cid, healthy=1, probe_status="ok",
                                    probe_fail_count=0, probe_last=fresh)
        try:
            _do_register([_mk_engine(name, healthy=False)], cid)
            assert _runtime_state(name, cid)[0] == 1, (
                "RUNTIME_PROBE_FRESH_WINDOW=0 不得静默禁用守卫")
        finally:
            _cleanup(cid)
