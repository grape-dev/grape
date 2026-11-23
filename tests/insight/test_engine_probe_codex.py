"""Tests for codex engine probe coverage (Issue #81).

[P0b B5 探活合一后修正] worker/insight 两侧探活命令表与名字分支已消，
两侧 10 分钟循环同走 probe_dispatch.probe_deep 单点分派（配置来自
descriptor probe 字段）。本文件改验分派口径：codex 经 descriptor
派生命令可探活、未知引擎 skip、覆盖判定按 probe.mode。
"""
import os
import sys

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault("GITCODE_API_TOKEN", "test_token")


def _reg():
    import src.domains.runtime.engine.registry  # noqa: F401 循环导入前置
    from src.domains.runtime.engine.descriptor import get_registry
    return get_registry()


def _probe_cmds():
    return _reg().probe_commands()


class TestDispatcherProbeCommands:
    """单点分派器探活（probe_dispatch + descriptor，worker/insight 共用）。"""

    def test_codex_in_probe_commands(self):
        assert "codex" in _probe_cmds()

    def test_codex_probe_command_format(self):
        cmd = _probe_cmds()["codex"]
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "--json" in cmd
        assert "--sandbox" in cmd
        assert "danger-full-access" in cmd
        assert "hello" in cmd

    def test_codex_probe_returns_result_not_skip(self):
        """probe_deep 对 codex 不应返回 skip（有命令模板）。"""
        from src.domains.runtime.engine.probe_dispatch import probe_deep
        result = probe_deep("codex", "")
        assert result["status"] != "skip"

    def test_unknown_engine_returns_skip(self):
        from src.domains.runtime.engine.probe_dispatch import probe_deep
        assert probe_deep("unknown-engine", "")["status"] == "skip"

    def test_all_known_engines_covered(self):
        """覆盖判定按 descriptor probe.mode：cli_run ∈ 命令表（值同源），
        acp_handshake 的探活实现按 probe.impl 注册（结构性覆盖，无死数据）。"""
        reg = _reg()
        from src.domains.runtime.engine import probe_dispatch
        cmds = reg.probe_commands()
        for eng, d in reg.all().items():
            if d["probe"]["mode"] == "cli_run":
                assert eng in cmds, f"{eng} (probe.mode=cli_run) 缺探活命令"
            elif d["probe"]["mode"] == "acp_handshake":
                assert d["probe"].get("impl") in probe_dispatch._ACP_IMPLS, \
                    f"{eng} (probe.mode=acp_handshake) 缺已注册的 probe.impl"


class TestBothLoopsShareDispatcher:
    """worker 与 insight 循环同走单点分派（双表漂移结构性不可能）。"""

    def test_worker_and_insight_delegate_to_same_dispatcher(self):
        from src.domains.runtime.engine import agent, probe_dispatch
        from src.domains.insight.observability import engine_probe
        # 两消费方的委托目标均为 probe_dispatch.probe_deep（同源验证）
        import inspect
        w = inspect.getsource(agent._probe_engine_worker)
        i = inspect.getsource(engine_probe._probe_engine)
        assert "probe_deep" in w and "probe_dispatch" in w
        assert "probe_deep" in i and "probe_dispatch" in i
        assert callable(probe_dispatch.probe_deep)

    def test_hwcloud_probed_via_acp_impl(self, monkeypatch):
        """[B5 修复实证] insight 侧 hwcloud 缺口修复：hwcloud 按 probe.impl
        派发到 ACP 握手实现（原落命令表报 skip）。"""
        from src.domains.runtime.engine import probe_dispatch
        from src.domains.insight.observability import engine_probe
        monkeypatch.setattr(
            probe_dispatch, "_ACP_IMPLS",
            {**probe_dispatch._ACP_IMPLS,
             "hwcloud_acp": ("tests.insight.test_engine_probe_codex", "_fake_hwcloud_impl")},
        )
        result = engine_probe._probe_engine("hwcloud", "/opt/hwc")
        assert result == {"status": "ok", "via": "hwcloud_acp"}


def _fake_hwcloud_impl(cli_path):
    return {"status": "ok", "via": "hwcloud_acp"}
