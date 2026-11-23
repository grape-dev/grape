"""Issue #3511: Windows worker 实测反馈的代码侧修复回归。

覆盖：
1. AtomCode CLI 版本差异（5.0.4 无 `--output-format`）——按 help 能力探测传参；
2. worker 侧引擎探针缺 atomcode → skip 恒不过健康检查；
3. seccomp 架构归一（Windows 的 `AMD64` 不得落到 aarch64 syscall 号）；
4. Windows 上 HERMES_HOME overlay 不可用（symlink 需特权）→ 复用原 home；
5. 安装脚本依赖清单与 pyproject/WORKER_DEPS 同源（不许再漏 fastapi）。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


# ── 1. AtomCode 参数能力探测 ───────────────────────────────────────────
def test_atomcode_args_skip_output_format_when_cli_lacks_it(monkeypatch):
    from src.domains.runtime.engine import atomcode as mod

    monkeypatch.setattr(mod, "cli_help_text",
                        lambda binary="atomcode": "Usage: atomcode [OPTIONS]\n  --prompt-file <PATH>")
    args = mod.AtomCodeRuntime().build_args("/tmp/p.txt", "s1")
    assert args[:2] == ["--prompt-file", "/tmp/p.txt"]
    assert "--output-format" not in args, "CLI 不支持时必须不传（否则 exit=2）"
    assert "--dangerously-skip-permissions" not in args
    assert args[-2:] == ["--resume", "s1"]


def test_atomcode_args_keep_output_format_when_supported(monkeypatch):
    from src.domains.runtime.engine import atomcode as mod

    monkeypatch.setattr(mod, "cli_help_text",
                        lambda binary="atomcode": "--output-format <FMT>  --dangerously-skip-permissions")
    args = mod.AtomCodeRuntime().build_args("/tmp/p.txt")
    assert "--output-format" in args and args[args.index("--output-format") + 1] == "jsonl"
    assert "--dangerously-skip-permissions" in args


def test_atomcode_args_unchanged_when_probe_unavailable(monkeypatch):
    """探测失败（CLI 缺失）不收紧既有参数，老环境行为不回归。"""
    from src.domains.runtime.engine import atomcode as mod

    monkeypatch.setattr(mod, "cli_help_text", lambda binary="atomcode": "")
    args = mod.AtomCodeRuntime().build_args("/tmp/p.txt")
    assert "--output-format" in args
    assert "--dangerously-skip-permissions" in args


# ── 2. worker 侧探针覆盖 atomcode ─────────────────────────────────────
def test_worker_probe_table_has_atomcode():
    from src.domains.runtime.engine import agent as mod

    assert "atomcode" in mod._PROBE_COMMANDS
    cmd = mod._PROBE_COMMANDS["atomcode"]
    assert cmd[0] == "atomcode" and "-p" in cmd


def test_worker_probe_atomcode_adds_optional_flags_only_when_supported(monkeypatch):
    from src.domains.runtime.engine import agent as mod
    from src.domains.runtime.engine import atomcode as amod

    seen: dict = {}

    class _Res:
        returncode = 0
        stdout = "OK"
        stderr = ""

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        return _Res()

    monkeypatch.setattr(amod, "cli_help_text", lambda binary="atomcode": "--prompt-file <PATH>")
    monkeypatch.setattr(mod._subprocess, "run", _fake_run)
    out = mod._probe_engine_worker("atomcode", "atomcode")
    assert out["status"] == "ok"
    assert "--ephemeral" not in seen["cmd"] and "--no-tools" not in seen["cmd"]

    monkeypatch.setattr(amod, "cli_help_text",
                        lambda binary="atomcode": "--ephemeral --no-tools")
    out = mod._probe_engine_worker("atomcode", "atomcode")
    assert out["status"] == "ok"
    assert "--ephemeral" in seen["cmd"] and "--no-tools" in seen["cmd"]


# ── 3. seccomp 架构归一 ───────────────────────────────────────────────
@pytest.mark.parametrize("arch,expected", [
    ("AMD64", 41), ("amd64", 41), ("x86_64", 41), ("X64", 41),
    ("arm64", 198), ("aarch64", 198), ("ARM64", 198),
])
def test_seccomp_socket_syscall_nr(arch, expected):
    from src.domains.runtime.agents.sandbox.isolation.seccomp import socket_syscall_nr

    assert socket_syscall_nr(arch) == expected


def test_seccomp_open_disabled_outside_posix(monkeypatch):
    from src.domains.runtime.agents.sandbox.isolation import seccomp as mod

    prog = mod.BPFSocketFilter()
    monkeypatch.setattr(type(prog), "_SECCOMP_SUPPORTED", False, raising=False)
    assert prog.open() == -1


# ── 4. Windows 不建 overlay ───────────────────────────────────────────
def test_overlay_attempted_on_windows_with_junction_tier(monkeypatch):
    """Windows 也要尝试 overlay：symlink→junction→递归复制 三梯次 + 可用性自检。

    2026-11-20 起的策略改成「先试、不行再回退共享 home」——直接禁用 overlay
    会让 Windows 上 per-task 隔离永久缺失；而 junction 不需要提权。
    """
    from src.domains.runtime.agents import hermes_acp as mod

    monkeypatch.setattr(mod.os, "name", "nt")
    assert mod._overlay_supported() is True
    # 非 Windows 下 _junction_dir 恒 False（无 mklink/权限）
    assert callable(mod._junction_dir) and callable(mod._overlay_looks_usable)


def test_overlay_supported_true_on_posix(monkeypatch):
    from src.domains.runtime.agents import hermes_acp as mod

    monkeypatch.setattr(mod.os, "name", "posix")
    assert mod._overlay_supported() is True


# ── 5. 安装脚本依赖清单同源 ───────────────────────────────────────────
def _worker_deps() -> list[str]:
    sys.path.insert(0, str(REPO))
    from src.kernel.worker_package import WORKER_DEPS

    def base(dep: str) -> str:
        return re.split(r"[><=!~;\[ ]", dep, maxsplit=1)[0].strip().lower().replace("-", "_")

    return [base(d) for d in WORKER_DEPS]


def test_install_worker_sh_deps_cover_worker_deps():
    text = (REPO / "scripts" / "install-worker.sh").read_text(encoding="utf-8")
    m = re.search(r'^WORKER_DEPS="([^"]+)"', text, re.M)
    assert m, "install-worker.sh 的 WORKER_DEPS 声明丢了"
    declared = {d.lower().replace("-", "_") for d in m.group(1).split()}
    missing = [d for d in _worker_deps() if d not in declared]
    assert not missing, f"install-worker.sh 缺依赖: {missing}"


def test_install_worker_ps1_deps_cover_worker_deps():
    text = (REPO / "scripts" / "install-worker.ps1").read_text(encoding="utf-8")
    m = re.search(r"^\$workerDeps\s*=\s*@\(([^)]*)\)", text, re.M)
    assert m, "install-worker.ps1 的 $workerDeps 声明丢了"
    declared = {d.strip().strip('"').lower().replace("-", "_")
                for d in m.group(1).split(",") if d.strip()}
    missing = [d for d in _worker_deps() if d not in declared]
    assert not missing, f"install-worker.ps1 缺依赖: {missing}"


# ── 6. worker 无装配层：端口未绑定路径必须安静 ─────────────────────
def test_token_service_degrades_when_port_unbound(monkeypatch):
    """worker 进程没有 contracts_wiring → 端口未绑定，查询返回空级别不抛。"""
    from src.kernel.ports import tokens as _tokens_port
    from src.kernel import token_service

    monkeypatch.setattr(_tokens_port, "is_bound", lambda: False)
    monkeypatch.setattr(
        _tokens_port, "get_credential",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("kernel port 'tokens' is not bound. The assembly layer")),
        raising=False)
    # 端口未绑定时 provider 构造/fetch 都会抛 PortNotBound，统一降级。
    # 2026-11-23：实现在 token_service 内改走 ports.vcs_provider（符号已迁移），
    # 老用例 patch 的 token_service.create_provider_from_repo_key 不再存在 → 改在这里打桩。
    from src.kernel.ports._base import PortNotBound
    import src.kernel.ports.vcs_provider as _vcs_port
    monkeypatch.setattr(
        _vcs_port, "create_provider_from_repo_key",
        lambda _rk: (_ for _ in ()).throw(PortNotBound("tokens")))

    res = token_service.resolve_repo_config_level("gitcode/grape-dev/grape")
    assert res.level == ""
    assert res.repo_key == "gitcode/grape-dev/grape"


def test_dependency_check_skips_level_probe_when_port_unbound(monkeypatch, caplog):
    import logging
    from src.kernel.ports import tokens as _tokens_port
    from src.domains.runtime.engine import dependency_check as mod

    monkeypatch.setattr(_tokens_port, "is_bound", lambda: False)
    task = {"id": "t1", "repo_key": "gitcode/a/b", "agent_type": "coding",
            "prompt": "do it", "skill": "coding"}
    with caplog.at_level(logging.DEBUG):
        mod.validate_task_dependencies(task=task, engine_name="opencode")
    msgs = [r.message for r in caplog.records]
    assert not [m for m in msgs if "no repo token config" in m], \
        "worker 上不该刷 repo token 级别告警"
