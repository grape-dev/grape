"""Issue #3642 / #2525：codearts trust 符号的新老导入路径兼容。

回归点：
- 安装脚本 5.8 段曾只从 `src.domains.runtime.agents.codearts` 导入
  `write_trust_settings`，M0 领域迁移后该符号位于
  `src.domains.runtime.engine.codearts`，导致 worker 自动升级 ImportError 回滚。
- 兼容层 `agents/codearts.py` 未转出该符号，#2525 修复未进发版线。

断言：
- 兼容层与 `agents` 包仍从旧路径转出 `write_trust_settings` / `probe_env`；
- 安装脚本按“先 engine 新路径、ImportError 回退 agents 老路径”导入；
- 实跑脚本 5.8 段可写入 trustedFolders（rc=0）。
"""
import json
import os
import sys

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import install_worker_harness as harness  # noqa: E402


def test_agents_compat_layer_forwards_trust_symbols():
    from src.domains.runtime.engine import codearts as engine
    from src.domains.runtime.agents import codearts as compat

    assert compat.write_trust_settings is engine.write_trust_settings
    assert compat.probe_env is engine.probe_env
    assert compat.CodeArtsBackend is engine.CodeArtsRuntime


def test_agents_package_exports_trust_symbols():
    from src.domains.runtime.engine import codearts as engine
    import src.domains.runtime.agents as agents

    assert agents.write_trust_settings is engine.write_trust_settings
    assert agents.probe_env is engine.probe_env


def test_install_script_prefers_new_path_then_falls_back():
    script = harness.script_text()
    new_import = "from src.domains.runtime.engine.codearts import write_trust_settings"
    old_import = "from src.domains.runtime.agents.codearts import write_trust_settings"
    assert new_import in script, "安装脚本应优先从 engine.codearts 导入"
    assert old_import in script, "安装脚本应保留 agents.codearts 回退路径"
    assert script.index(new_import) < script.index(old_import), \
        "必须先尝试新路径，再回退老路径"


def _make_fake_venv(tmp_path):
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(sys.executable, venv / "bin" / "python")
    return str(venv)


def _make_fake_codearts_bin(tmp_path):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    exe = bin_dir / "codearts"
    exe.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(exe, 0o755)
    return str(bin_dir)


def test_provision_codearts_trust_runs_via_script_segment(tmp_path):
    """实跑安装脚本 5.8 段：新包上写 trust 配置成功，rc=0。"""
    venv = _make_fake_venv(tmp_path)
    fake_bin = _make_fake_codearts_bin(tmp_path)
    calls = tmp_path / "calls.log"
    tasks = tmp_path / "codearts-tasks"
    home = tmp_path / "home"
    home.mkdir()

    h = harness.build_harness(
        "provision_codearts_trust\n",
        install_dir=ROOT,
        venv=venv,
        calls_log=str(calls),
        extra_functions=["provision_codearts_trust"],
    )
    env = {
        "HOME": str(home),
        "PATH": fake_bin + os.pathsep + os.environ.get("PATH", ""),
        "CODEARTS_TASKS_DIR": str(tasks),
    }
    r = harness.run(h, env=env)
    assert r.returncode == 0, f"5.8 段应以 rc=0 结束: {r.stderr}"

    settings_path = home / ".codeartsdoer" / "cli-data" / "settings.json"
    assert settings_path.exists(), "应写出 settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    trusted = settings["security"]["trustedFolders"]
    assert ROOT in trusted, f"安装目录应被信任: {trusted}"
    assert str(tasks) in trusted, f"tasks 目录应被信任: {trusted}"
    assert "codearts folder trust configured" in calls.read_text(encoding="utf-8")
