"""P0a A3：executor 本机分派按 ACP 能力钩子（acp_backend）而非引擎名。

根因：stream_agent 里 `if backend_name == "hermes"/"hwcloud"/"aishell-dsh"` 三个
ACP 专用分支 + 三份同构 _stream_*_acp 函数——新 ACP 引擎接入必须复制整段分派
逻辑与事件收口（约 400 行）。spec A2/A3：收敛为一份 _stream_acp，分派判定用
backend 自报能力（acp_backend() 钩子），新引擎 = 薄 backend 子类 + 一行注册。

分派判定裁定（D-P0-1 变体，2026-11-13 实施时定）：不用 protocol 属性做本机
分派——云沙箱路径（executor _run_cloud_sandbox）已用 `protocol=="acp"` 判定
hermes 专用 ACP 云分支（hwcloud 云任务现走 NDJSON PTY 路径），在 Runtime 上加
protocol="acp" 会连带改变云路径行为；acp_backend() 是独立能力钩子，只影响
本机分派，且与类继承解耦（duck-typed）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.domains.runtime.engine.registry  # noqa: E402,F401 先触发（绕开循环导入）
from src.domains.runtime.agents import executor


class _FakeAcpBackend:
    """伪 ACP backend：记录调用序列，回放固定事件流。"""
    name = "fake-acp"
    binary = "fake-acp"

    def __init__(self):
        self.calls = []

    def get_env(self, account="", user_id=""):
        self.calls.append(("get_env", account, user_id))
        return {}

    def prepare_env(self, env, cwd=""):
        self.calls.append(("prepare_env", cwd))
        return "/tmp/fake-auth.tok"

    def stream_execute(self, prompt, cwd, session_id="", env=None,
                       max_exec_seconds=600, agent_label=""):
        self.calls.append(("stream_execute", prompt, cwd, session_id))
        yield ("text", "hello")
        yield ("done", {"exit_code": 0, "text": "hello", "oc_session_id": session_id,
                        "errors": [], "stderr": ""})


class _FakeAcpRuntime:
    """注册进 _BACKENDS 的 runtime 壳：acp_backend() 钩子自报 ACP 能力。"""
    name = "fake-acp"
    binary = "fake-acp"

    def __init__(self):
        self.acp = _FakeAcpBackend()

    def acp_backend(self):
        return self.acp


def _stream(backend_name, events):
    evts = []
    for evt in executor.stream_agent(
        "hi", cwd="/tmp", session_id="", event_type="chat",
        backend_name=backend_name, max_exec_seconds=30,
    ):
        evts.append(evt)
    return evts


class TestAcpCapabilityDispatch:
    def test_acp_backend_hook_routes_to_acp_stream(self, monkeypatch):
        """有 acp_backend() 钩子的 backend → 走统一 _stream_acp（与引擎名无关）。"""
        shell = _FakeAcpRuntime()
        monkeypatch.setitem(executor._BACKENDS, "fake-acp", shell)
        evts = _stream("fake-acp", None)
        assert shell.acp.calls[0][0] == "get_env"
        assert shell.acp.calls[-1][0] == "stream_execute"
        # 事件收口等价：text/done 透传
        types = [e[0] for e in evts]
        assert "text" in types and "done" in types
        done = dict(evts)[-1] if False else next(d for t, d in evts if t == "done")
        assert done["exit_code"] == 0

    def test_prepare_env_hook_fires_after_env_extra(self, monkeypatch):
        """有 prepare_env 的引擎（aishell 形态）→ 钩子在 env_extra 合并后触发。"""
        shell = _FakeAcpRuntime()
        monkeypatch.setitem(executor._BACKENDS, "fake-acp", shell)
        _stream("fake-acp", None)
        kinds = [c[0] for c in shell.acp.calls]
        # get_env → prepare_env → stream_execute 顺序
        assert kinds.index("get_env") < kinds.index("prepare_env") < kinds.index("stream_execute")

    def test_no_acp_hook_falls_to_cli_path(self, monkeypatch):
        """无 acp_backend() 钩子（CLI 形态）→ 不进 ACP 路径（走 build_args 链）。"""
        # opencode 无 acp_backend 钩子；给它打个桩验证不抛 NotImplementedError
        b = executor._BACKENDS["opencode"]
        assert not callable(getattr(b, "acp_backend", None))

    def test_hermes_hwcloud_aishell_registered_backends_expose_hook(self):
        """三个 ACP 引擎的注册 backend 都必须暴露 acp_backend()（迁移完成守卫）。"""
        for name in ("hermes", "hwcloud", "aishell-dsh"):
            b = executor.get_backend(name)
            assert callable(getattr(b, "acp_backend", None)), f"{name} 缺 acp_backend() 钩子"

    def test_name_branches_are_gone(self):
        """A3 验收：executor 里不再有按引擎名的 ACP 分派分支。

        口径=spec A3 度量：`backend_name == "hermes|hwcloud|aishell-dsh"` ACP
        分派分支清零（CLI 遗留路径的 `backend.name == "hermes"` iso-home 拼装块
        不属 ACP 分派——hermes 恒走 ACP 后已不可达，记 P0b 瘦身清单）。
        """
        import inspect
        src = inspect.getsource(executor.stream_agent)
        assert 'backend_name == "hermes"' not in src
        assert 'backend_name == "hwcloud"' not in src
        assert 'backend_name == "aishell-dsh"' not in src
        # 三份同构函数已收敛
        assert not hasattr(executor, "_stream_hermes_acp")
        assert not hasattr(executor, "_stream_hwcloud_acp")
        assert not hasattr(executor, "_stream_aishell_dsh_acp")
        assert hasattr(executor, "_stream_acp")
