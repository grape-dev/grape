"""B5 探活合一：单点分派器 probe_deep/probe_light（2026-11-13 用户拍板）。

合一收「分派知识」不收「探活档位」：
- probe_deep 按 descriptor probe.mode 派发（acp_handshake → probe.impl 指定的
  引擎 ACP 探活实现；cli_run → probe.command 命令表）；
- probe_light 按新增字段 probe.light_args（默认 ["--version"]，hermes 覆盖
  ["acp","--check"]，消 local_probe.py 手抄散点）；
- 消掉的名字分支共三处：worker agent._probe_engine_worker、insight
  engine_probe._probe_engine、local_probe.py 的 hermes probe_args 覆盖；
- insight 侧 hwcloud 探活缺口（原落命令表报 skip）随派发自然修复。

参考: P0 引擎接入解耦 spec「探活两档保留裁定」（deep/light 两档分离）。
"""
import pytest


# ── L0: descriptor 两档字段齐备性（drift 守卫追加项）─────────────────

class TestDescriptorProbeFields:
    def test_all_acp_handshake_have_impl(self):
        """probe.mode=acp_handshake 的引擎必须有 probe.impl（防「有模式无实现」半接入）。"""
        from src.domains.runtime.engine.descriptor import get_registry
        for name, d in get_registry().all().items():
            if d["probe"]["mode"] == "acp_handshake":
                impl = d["probe"].get("impl")
                assert impl, f"{name}: probe.mode=acp_handshake 但缺 probe.impl"

    def test_impl_tokens_registered(self):
        """descriptor 引用的 probe.impl 必须在分派器实现表里注册（防拼错 token 静默落空）。"""
        from src.domains.runtime.engine.descriptor import get_registry
        from src.domains.runtime.engine.probe_dispatch import _ACP_IMPLS
        for name, d in get_registry().all().items():
            impl = d["probe"].get("impl")
            if impl:
                assert impl in _ACP_IMPLS, f"{name}: probe.impl={impl!r} 未注册"


# ── probe_light: 浅探参数来自 descriptor ─────────────────────────────

class TestProbeLight:
    def test_hermes_light_args_from_descriptor(self):
        """hermes 浅探 = acp --check（--version 探不出 ACP 依赖缺失）。"""
        from src.domains.runtime.engine.descriptor import get_registry
        assert get_registry().light_probe_args("hermes") == ["acp", "--check"]

    def test_default_light_args(self):
        """未声明 light_args 的引擎默认 [--version]。"""
        from src.domains.runtime.engine.descriptor import get_registry
        assert get_registry().light_probe_args("opencode") == ["--version"]

    def test_local_probe_no_manual_hermes_override(self):
        """local_probe.probe_local_engines 不再含 hermes 手抄 probe_args 覆盖（散点已消）。

        覆盖逻辑收进 _probe_one：engine dict 未带 probe_args 时按 descriptor
        light_probe_args 取。
        """
        import inspect
        from src.domains.runtime.engine import local_probe
        src = inspect.getsource(local_probe.probe_local_engines)
        assert '"hermes"' not in src, "probe_local_engines 不得再按名字手抄覆盖（合一散点）"
        assert 'probe_args": ' not in src and "probe_args\":" not in src, \
            "probe_local_engines 不得再注入手抄 probe_args"
        src_one = inspect.getsource(local_probe._probe_one)
        assert "light_probe_args" in src_one, "_probe_one 应从 descriptor 取浅探参数"


# ── probe_deep: 单点派发 ─────────────────────────────────────────────

class TestProbeDeep:
    def test_unknown_engine_skip(self):
        from src.domains.runtime.engine.probe_dispatch import probe_deep
        r = probe_deep("no-such-engine")
        assert r["status"] == "skip"
        assert "unknown engine" in r["error"]

    def test_acp_handshake_dispatches_by_impl(self, monkeypatch):
        """acp_handshake 按 probe.impl 派发到实现表指定函数（名字分支消失）。"""
        from src.domains.runtime.engine import probe_dispatch
        import src.domains.runtime.engine.agent as agent_mod
        # impl 表指向的符号必须真实存在且为 worker 侧单源实现
        for token, (mod_name, fn_name) in probe_dispatch._ACP_IMPLS.items():
            mod = __import__(mod_name, fromlist=[fn_name])
            assert callable(getattr(mod, fn_name)), f"impl {token} 符号不存在"
        assert probe_dispatch._ACP_IMPLS["hwcloud_acp"] == (
            "src.domains.runtime.engine.agent", "_probe_hwcloud_acp")
        # monkeypatch 实现表后派发路径走新 token（不真跑 ACP）
        monkeypatch.setitem(
            probe_dispatch._ACP_IMPLS, "hwcloud_acp",
            ("tests.runtime.test_probe_dispatch", "_fake_acp_impl"),
        )
        r = probe_dispatch.probe_deep("hwcloud", cli_path="/bin/true")
        assert r == {"status": "ok", "via": "impl"}

    def test_cli_run_builds_command_from_descriptor(self, monkeypatch):
        from src.domains.runtime.engine import probe_dispatch
        ran = []

        class _FakeResult:
            returncode = 0
            stdout = "hello-ok"
            stderr = ""

        def _fake_run(cmd, **kw):
            ran.append(cmd)
            return _FakeResult()

        monkeypatch.setattr(probe_dispatch.subprocess, "run", _fake_run)
        r = probe_dispatch.probe_deep("codex", cli_path="/usr/local/bin/codex")
        assert r["status"] == "ok"
        assert len(ran) == 1
        assert ran[0][0] == "/usr/local/bin/codex"
        assert "--json" in ran[0]

    def test_codearts_env_hook_applied(self, monkeypatch):
        """codearts probe env quirk 留显式 hook：descriptor probe.env_hook 驱动。"""
        from src.domains.runtime.engine import probe_dispatch
        from src.domains.runtime.engine.descriptor import get_registry
        d = get_registry().get("codearts")
        assert d["probe"].get("env_hook") == "codearts_probe_env", \
            "codearts descriptor 应声明 probe.env_hook（folder-trust 数据目录 quirk）"


# ── 消费方收敛：三处名字分支全部改为委托单点分派器 ────────────────────

class TestConsumersDelegate:
    def test_worker_probe_engine_worker_delegates(self, monkeypatch):
        """worker agent._probe_engine_worker 不再含名字分支，全量委托 probe_deep。"""
        from src.domains.runtime.engine import agent, probe_dispatch
        called = []

        def _fake(name, cli_path=""):
            called.append((name, cli_path))
            return {"status": "skip", "latency": 0, "error": ""}

        monkeypatch.setattr(probe_dispatch, "probe_deep", _fake)
        agent._probe_engine_worker("hermes", "/opt/hermes")
        agent._probe_engine_worker("opencode", "/opt/oc")
        assert called == [("hermes", "/opt/hermes"), ("opencode", "/opt/oc")]
        # 名字分支散点已消
        import inspect
        src = inspect.getsource(agent._probe_engine_worker)
        for name in ("hermes", "hwcloud", "aishell"):
            assert f'== "{name}"' not in src, "worker 探活不得残留名字分支"

    def test_insight_probe_engine_delegates(self, monkeypatch):
        """insight engine_probe._probe_engine 委托 probe_deep（hwcloud 缺口随合一修复）。"""
        from src.domains.runtime.engine import probe_dispatch
        from src.domains.insight.observability import engine_probe
        called = []

        def _fake(name, cli_path=""):
            called.append((name, cli_path))
            return {"status": "skip", "latency": 0, "error": ""}

        monkeypatch.setattr(probe_dispatch, "probe_deep", _fake)
        engine_probe._probe_engine("hwcloud", "/opt/hwc")
        engine_probe._probe_engine("codex", "/opt/codex")
        assert called == [("hwcloud", "/opt/hwc"), ("codex", "/opt/codex")]
        import inspect
        src = inspect.getsource(engine_probe._probe_engine)
        for name in ("hermes", "aishell"):
            assert f'== "{name}"' not in src, "insight 探活不得残留名字分支"

    def test_insight_local_hermes_acp_copy_removed(self):
        """insight 自带的 hermes ACP 探活副本删除（单源 = worker 侧 agent 实现）。"""
        from src.domains.insight.observability import engine_probe
        assert not hasattr(engine_probe, "_probe_hermes_acp"), \
            "insight 侧 hermes 探活副本应删（aishell 单源先例：import agent 实现）"


def _fake_acp_impl(cli_path):
    return {"status": "ok", "via": "impl"}


# ── 复审整改：spawn 三态 + atomcode 可选参数 help 能力探测 ─────────────

class TestSpawnFailedThreeState:
    """R2：cli_run 资源类 spawn 失败须单独成态（认领闸门/告警链消费）。"""

    def _probe_with_run_raising(self, monkeypatch, exc):
        from src.domains.runtime.engine import probe_dispatch

        def _raise(cmd, **kw):
            raise exc

        monkeypatch.setattr(probe_dispatch.subprocess, "run", _raise)
        return probe_dispatch.probe_deep("codex", cli_path="/usr/local/bin/codex")

    def test_spawn_failure_status(self, monkeypatch):
        import errno
        r = self._probe_with_run_raising(
            monkeypatch, OSError(errno.EMFILE, "too many open files"))
        assert r["status"] == "spawn_failed", r
        assert "spawn failed" in r["error"]

    def test_non_spawn_os_failure_stays_fail(self, monkeypatch):
        r = self._probe_with_run_raising(monkeypatch, OSError("boom"))
        assert r["status"] == "fail", r

    def test_consumer_gate_uses_spawn_failed(self):
        """消费方语义：service 认领闸门挡 spawn_failed（不挡普通 fail）。"""
        import inspect
        from src.domains.runtime.engine import service
        src = inspect.getsource(service)
        assert "spawn_failed" in src, \
            "engine service 须消费 spawn_failed 态（认领闸门拦截派单）"


class TestAtomcodeArgsHook:
    """R3：可选参数按 CLI help 能力追加（5.0.4 等旧版盲传会 exit=2 误报 fail）。"""

    def _probe_with_help(self, monkeypatch, help_text):
        from src.domains.runtime.engine import probe_dispatch
        seen = {}

        class _Res:
            returncode = 0
            stdout = "OK"
            stderr = ""

        class _HelpRes:
            returncode = 0
            stdout = help_text
            stderr = ""

        def _fake_run(cmd, **kw):
            if "--help" in cmd:
                return _HelpRes()
            seen["cmd"] = list(cmd)
            return _Res()

        monkeypatch.setattr(probe_dispatch.subprocess, "run", _fake_run)
        r = probe_dispatch.probe_deep("atomcode", cli_path="atomcode")
        return r, seen.get("cmd")

    def test_flags_appended_when_supported(self, monkeypatch):
        r, cmd = self._probe_with_help(monkeypatch, "--ephemeral --no-tools")
        assert r["status"] == "ok"
        assert "--ephemeral" in cmd and "--no-tools" in cmd, cmd

    def test_flags_omitted_when_unsupported(self, monkeypatch):
        """help 无这些开关（如 5.0.4）→ 保持基础参数，不误报。"""
        r, cmd = self._probe_with_help(monkeypatch, "--prompt-file <PATH>")
        assert r["status"] == "ok"
        assert "--ephemeral" not in cmd and "--no-tools" not in cmd, cmd

    def test_help_probe_unavailable_keeps_base_args(self, monkeypatch):
        """help 探测失败（CLI 缺失）不收紧基础参数（老环境行为不回归）。"""
        from src.domains.runtime.engine import probe_dispatch

        def _raise(cmd, **kw):
            if "--help" in cmd:
                raise FileNotFoundError("no cli")
            return type("_R", (), {"returncode": 0, "stdout": "OK", "stderr": ""})()

        monkeypatch.setattr(probe_dispatch.subprocess, "run", _raise)
        r = probe_dispatch.probe_deep("atomcode", cli_path="atomcode")
        assert r["status"] == "ok"

    def test_descriptor_declares_args_hook(self):
        from src.domains.runtime.engine.descriptor import get_registry
        d = get_registry().get("atomcode")
        assert d["probe"].get("args_hook") == "atomcode_help_flags"
        assert d["probe"]["command"] == ["atomcode", "-p", "Reply OK"], \
            "版本相关可选开关不得固化进 probe.command"
        assert set(d["probe"].get("optional_flags", [])) == {"--no-tools", "--ephemeral"}
