"""B5 探活合一：单点分派器 probe_deep/probe_light（2026-11-13 用户拍板）。

合一收「分派知识」不收「探活档位」：
- probe_deep 按 descriptor probe.mode 派发（acp_handshake → probe.impl 指定的
  引擎 ACP 探活实现；cli_run → probe.command 命令表）；
- probe_light 按新增字段 probe.light_args（默认 ["--version"]，hermes 覆盖
  ["acp","--check"]，消 local_probe.py 手抄散点）；
- 消掉的名字分支共三处：worker agent._probe_engine_worker、insight
  engine_probe._probe_engine、local_probe.py 的 hermes probe_args 覆盖；
- insight 侧 hwcloud 探活缺口（原落命令表报 skip）随派发自然修复。

参考: docs/todo/2026-11-13-p0-引擎接入解耦实施回执.md「探活两档保留裁定」。
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
