"""Issue #378: 看门狗静默杀死阈值针对 coding/qa 放宽为 1200s 的单元测试。

覆盖：
1. src/runtime/agent.py _AGENT_SILENT_TIMEOUTS 映射表 + setdefault 注入语义
2. src/agent/hermes_acp.py _silent_timeout_for(env) 读 env 默认值/覆盖/非法回退
3. src/agent/executor.py _silent_window 优先读任务级 env dict（AGENT_SILENT_WINDOW）
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.runtime import agent as ra
import src.agent.hermes_acp as ha


# ══════════════════════════════════════════════════════════════════════
# 1. agent.py 映射表 + 注入语义
# ══════════════════════════════════════════════════════════════════════

class TestAgentSilentTimeoutsMapping:
    """_AGENT_SILENT_TIMEOUTS 映射表值校验。"""

    def test_coding_is_1200(self):
        assert ra._AGENT_SILENT_TIMEOUTS.get("coding") == 1200

    def test_qa_is_1200(self):
        assert ra._AGENT_SILENT_TIMEOUTS.get("qa") == 1200

    def test_other_agents_not_in_mapping(self):
        for _t in ("vod", "issue", "sre", "chat", ""):
            assert _t not in ra._AGENT_SILENT_TIMEOUTS, \
                f"{_t} 不应在差异化映射中（保持默认 300s）"

    def test_default_silent_window_is_300(self):
        assert ra._DEFAULT_SILENT_WINDOW == 300


class TestAgentSilentTimeoutInjection:
    """_execute_task 中 setdefault 注入逻辑（不启动完整 async 流程，直接验证语义）。"""

    def test_coding_injects_all_three_envs(self):
        """coding agent_type → env 注入 AGENT_SILENT_WINDOW/HERMES_SILENT_TIMEOUT/RUNTIME_TOOL_SILENT_TIMEOUT=1200。"""
        env = {}
        _ag_silent = ra._AGENT_SILENT_TIMEOUTS.get("coding")
        if _ag_silent:
            env.setdefault("AGENT_SILENT_WINDOW", str(_ag_silent))
            env.setdefault("HERMES_SILENT_TIMEOUT", str(_ag_silent))
            env.setdefault("RUNTIME_TOOL_SILENT_TIMEOUT", str(_ag_silent))
        assert env["AGENT_SILENT_WINDOW"] == "1200"
        assert env["HERMES_SILENT_TIMEOUT"] == "1200"
        assert env["RUNTIME_TOOL_SILENT_TIMEOUT"] == "1200"

    def test_qa_injects_all_three_envs(self):
        env = {}
        _ag_silent = ra._AGENT_SILENT_TIMEOUTS.get("qa")
        if _ag_silent:
            env.setdefault("AGENT_SILENT_WINDOW", str(_ag_silent))
            env.setdefault("HERMES_SILENT_TIMEOUT", str(_ag_silent))
            env.setdefault("RUNTIME_TOOL_SILENT_TIMEOUT", str(_ag_silent))
        assert env["AGENT_SILENT_WINDOW"] == "1200"
        assert env["HERMES_SILENT_TIMEOUT"] == "1200"
        assert env["RUNTIME_TOOL_SILENT_TIMEOUT"] == "1200"

    def test_other_agent_type_no_injection(self):
        """vod/issue/sre 等 agent_type 不命中映射 → env 不含静默超时键。"""
        for _t in ("vod", "issue", "sre", "chat", ""):
            env = {}
            _ag_silent = ra._AGENT_SILENT_TIMEOUTS.get(_t)
            if _ag_silent:
                env.setdefault("AGENT_SILENT_WINDOW", str(_ag_silent))
            assert "AGENT_SILENT_WINDOW" not in env, \
                f"{_t} 不应注入 AGENT_SILENT_WINDOW"

    def test_setdefault_does_not_override_existing(self):
        """上层已显式设置（手动调参）时 setdefault 不覆盖。"""
        env = {"AGENT_SILENT_WINDOW": "600", "HERMES_SILENT_TIMEOUT": "180",
               "RUNTIME_TOOL_SILENT_TIMEOUT": "900"}
        _ag_silent = ra._AGENT_SILENT_TIMEOUTS.get("coding")
        if _ag_silent:
            env.setdefault("AGENT_SILENT_WINDOW", str(_ag_silent))
            env.setdefault("HERMES_SILENT_TIMEOUT", str(_ag_silent))
            env.setdefault("RUNTIME_TOOL_SILENT_TIMEOUT", str(_ag_silent))
        assert env["AGENT_SILENT_WINDOW"] == "600"
        assert env["HERMES_SILENT_TIMEOUT"] == "180"
        assert env["RUNTIME_TOOL_SILENT_TIMEOUT"] == "900"

    def test_case_insensitive_agent_type(self):
        """agent_type 大小写不敏感（Coding/CODING/QA 均命中）。"""
        for _t in ("Coding", "CODING", "QA", "Qa"):
            assert ra._AGENT_SILENT_TIMEOUTS.get(_t.lower()) == 1200, \
                f"{_t}.lower() 应命中映射"


# ══════════════════════════════════════════════════════════════════════
# 2. hermes_acp.py _silent_timeout_for(env)
# ══════════════════════════════════════════════════════════════════════

class TestHermesSilentTimeoutEnv:
    """hermes _silent_timeout_for 读 HERMES_SILENT_TIMEOUT env。"""

    def test_default_is_300(self, monkeypatch):
        monkeypatch.delenv("HERMES_SILENT_TIMEOUT", raising=False)
        assert ha._silent_timeout_for(None) == 300.0
        assert ha._silent_timeout_for({}) == 300.0

    def test_env_dict_overrides_to_1200(self, monkeypatch):
        """任务级 env dict 优先（_execute_task 注入路径）。"""
        monkeypatch.delenv("HERMES_SILENT_TIMEOUT", raising=False)
        assert ha._silent_timeout_for({"HERMES_SILENT_TIMEOUT": "1200"}) == 1200.0

    def test_os_environ_fallback(self, monkeypatch):
        """env dict 未设置时读 os.environ（手动调参/测试路径）。"""
        monkeypatch.setenv("HERMES_SILENT_TIMEOUT", "600")
        assert ha._silent_timeout_for(None) == 600.0
        assert ha._silent_timeout_for({}) == 600.0

    def test_env_dict_takes_priority_over_os_environ(self, monkeypatch):
        """env dict 优先于 os.environ（任务级覆盖全局）。"""
        monkeypatch.setenv("HERMES_SILENT_TIMEOUT", "600")
        assert ha._silent_timeout_for({"HERMES_SILENT_TIMEOUT": "1200"}) == 1200.0

    def test_invalid_value_falls_back_to_300(self, monkeypatch):
        monkeypatch.delenv("HERMES_SILENT_TIMEOUT", raising=False)
        assert ha._silent_timeout_for({"HERMES_SILENT_TIMEOUT": "abc"}) == 300.0

    def test_invalid_os_environ_falls_back_to_300(self, monkeypatch):
        monkeypatch.setenv("HERMES_SILENT_TIMEOUT", "not-a-number")
        assert ha._silent_timeout_for(None) == 300.0

    def test_zero_value_accepted(self, monkeypatch):
        monkeypatch.delenv("HERMES_SILENT_TIMEOUT", raising=False)
        assert ha._silent_timeout_for({"HERMES_SILENT_TIMEOUT": "0"}) == 0.0


# ══════════════════════════════════════════════════════════════════════
# 3. executor.py _silent_window 优先读任务级 env dict
# ══════════════════════════════════════════════════════════════════════

class _AliveSilentProc:
    """模拟"进程存活但无输出"的子进程：poll 返回 None、reader 持续阻塞。"""
    def __init__(self, *a, **kw):
        import io
        self.stdout = _BlockingStdout()
        self.stderr = io.BytesIO()
        self.stdin = None
        self.returncode = -9

    def poll(self):
        return None

    def wait(self, timeout=None):
        import subprocess
        raise subprocess.TimeoutExpired(cmd="opencode", timeout=timeout)

    def terminate(self):
        pass

    def kill(self):
        pass


class _BlockingStdout:
    """短阻塞 stdout：1s 后返回 EOF（控制测试时长，> 默认静默+extend 上限）。"""
    def readline(self):
        import threading
        threading.Event().wait(1)
        return b""


def _force_kill(proc, label="", grace_seconds=10.0):
    proc.returncode = -9
    return None


class TestExecutorSilentWindowEnvDict:
    """executor _silent_window 优先读 env dict（env_extra 注入路径）。

    Issue #378 关键修复点：原代码读 os.environ，改为 env.get(..., os.environ.get(...))。
    行为判定（AGENT_SILENT_EXTEND_CAP=0.1）：
    - 默认 _silent_window=min(300,0.1)=0.1 → 静默超时在 0.2s（0.1+extend 0.1）触发 → exit -9；
    - AGENT_SILENT_WINDOW=1200 → 静默超时不触发（1200s >> 1s stdout EOF）→ 由 stdout EOF 路径收尾 exit -1。
    """

    def test_env_extra_agent_silent_window_picked_up(self, monkeypatch):
        """env_extra 含 AGENT_SILENT_WINDOW=1200 → _silent_window=1200 → 不被静默超时杀（exit != -9）。"""
        from src.runtime.executor import stream_agent
        import src.agent.executor as _ex
        import subprocess

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _AliveSilentProc())
        monkeypatch.setattr(_ex, "_terminate_proc", _force_kill)
        monkeypatch.delenv("AGENT_SILENT_WINDOW", raising=False)
        monkeypatch.setenv("AGENT_SILENT_EXTEND_CAP", "0.1")

        out = []
        for evt_type, data in stream_agent(
            "prompt", "/tmp", session_id="", backend_name="opencode",
            max_exec_seconds=0.1,
            env_extra={"AGENT_SILENT_WINDOW": "1200"},
        ):
            out.append((evt_type, data))
        dones = [d for t, d in out if t == "done"]
        assert dones, "应产出 done"
        # _silent_window=1200 → 静默超时不触发（1200s >> 1s stdout EOF）；
        # 由 stdout EOF 路径收尾（exit -1），非静默超时强杀（exit -9）。
        assert dones[0]["exit_code"] != -9, \
            "AGENT_SILENT_WINDOW=1200 时不应被静默超时强杀（-9）；应由 stdout EOF 路径收尾"

    def test_default_silent_window_kills_fast(self, monkeypatch):
        """对比：默认 _silent_window=min(300,0.1)=0.1 → 0.2s 静默超时强杀 exit -9。"""
        from src.runtime.executor import stream_agent
        import src.agent.executor as _ex
        import subprocess

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _AliveSilentProc())
        monkeypatch.setattr(_ex, "_terminate_proc", _force_kill)
        monkeypatch.delenv("AGENT_SILENT_WINDOW", raising=False)
        monkeypatch.setenv("AGENT_SILENT_EXTEND_CAP", "0.1")

        out = []
        for evt_type, data in stream_agent(
            "prompt", "/tmp", session_id="", backend_name="opencode",
            max_exec_seconds=0.1,
            env_extra={},
        ):
            out.append((evt_type, data))
        dones = [d for t, d in out if t == "done"]
        assert dones, "应产出 done"
        assert dones[0]["exit_code"] == -9, \
            "默认 _silent_window=0.1s → 静默超时强杀（-9）"

    def test_os_environ_fallback_when_env_extra_empty(self, monkeypatch):
        """env_extra 不含 AGENT_SILENT_WINDOW → 回退读 os.environ（兼容手动调参）。"""
        from src.runtime.executor import stream_agent
        import src.agent.executor as _ex
        import subprocess

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _AliveSilentProc())
        monkeypatch.setattr(_ex, "_terminate_proc", _force_kill)
        monkeypatch.setenv("AGENT_SILENT_WINDOW", "1200")
        monkeypatch.setenv("AGENT_SILENT_EXTEND_CAP", "0.1")

        out = []
        for evt_type, data in stream_agent(
            "prompt", "/tmp", session_id="", backend_name="opencode",
            max_exec_seconds=0.1,
            env_extra={},
        ):
            out.append((evt_type, data))
        dones = [d for t, d in out if t == "done"]
        assert dones, "应产出 done（os.environ fallback 生效）"
        # os.environ AGENT_SILENT_WINDOW=1200 → 同 env_extra 路径，不被静默超时杀
        assert dones[0]["exit_code"] != -9, \
            "os.environ AGENT_SILENT_WINDOW=1200 时不应被静默超时强杀"
