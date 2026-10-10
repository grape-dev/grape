"""#282 修复：hermes ACP 全局超时对齐外层 RUNTIME_TASK_TIMEOUT。

旧逻辑 `_GLOBAL_TIMEOUT = max_exec_seconds * 2`（=1200s 写死倍率）导致 hermes 引擎
长时 coding 任务 20 分钟一到即 stopReason=timeout 硬切，即使输出持续正常滚动
（实测任务 9620：786 条消息、20 分 13 秒被切）。

修复：全局上限取 RUNTIME_TASK_TIMEOUT（与 src/runtime/agent.py 任务级看门狗同源，
默认 3600s），下限保护 max_exec_seconds；静默超时（_SILENT_TIMEOUT/_TEXT_SILENT_TIMEOUT）
语义不变。

覆盖用例（对照 Issue #282 验收标准）：
① 持续输出超过旧 1200s（倍率=2×max_exec）上限的任务不再被全局超时硬切；
② 无输出场景下静默超时（_SILENT_TIMEOUT）仍正常触发；
③ RUNTIME_TASK_TIMEOUT 环境变量配置可生效（含下限保护 / 非法值回退）。
"""
import os
import sys
import time

import pytest

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault("GITCODE_API_TOKEN", "test_token")

from src.runtime.agent import _backend_for_engine  # noqa: F401,E402  先入 runtime 打破循环导入
import src.agent.hermes_acp as ha  # noqa: E402


# ── 单元用例：_global_timeout_for 取值逻辑 ──────────────────────────────

class TestGlobalTimeoutValue:
    def test_default_aligns_with_runtime_task_timeout(self, monkeypatch):
        monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
        v = ha._global_timeout_for(600)
        assert v == 3600.0, "默认应与外层 RUNTIME_TASK_TIMEOUT 默认值 3600s 对齐"

    def test_no_longer_double_multiplier(self, monkeypatch):
        """旧逻辑 600*2=1200s 必死；新逻辑 ≥3600s，长任务获得与 opencode 一致的上限。"""
        monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
        assert ha._global_timeout_for(600) > 600 * 2

    def test_env_override_effective(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_TASK_TIMEOUT", "7200")
        assert ha._global_timeout_for(600) == 7200.0

    def test_floor_protection_keeps_global_above_silent_window(self, monkeypatch):
        """配置异常（env 小于 max_exec_seconds）时下限保护：全局上限 ≥ 静默超时窗口。"""
        monkeypatch.setenv("RUNTIME_TASK_TIMEOUT", "10")
        assert ha._global_timeout_for(600) == 600.0

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_TASK_TIMEOUT", "not-a-number")
        assert ha._global_timeout_for(600) == 3600.0


# ── 行为用例：fake hermes ACP 子进程走完整 stream_execute ────────────────

FAKE_HERMES_SCRIPT = r'''
import json, os, sys, threading, time

MODE = os.environ.get("FAKE_HERMES_MODE", "stream_then_end")
STREAM_SECONDS = float(os.environ.get("FAKE_HERMES_STREAM_SECONDS", "3"))

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def stream(prompt_id):
    end = time.time() + STREAM_SECONDS
    while time.time() < end:
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"text": "tick\n"}}}})
        time.sleep(0.05)
    if MODE == "stream_then_end":
        send({"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": "end_turn"}})

for line in iter(sys.stdin.readline, ""):
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    method = msg.get("method", "")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "sess_fake_282"}})
    elif method == "session/prompt":
        if MODE != "silent":
            threading.Thread(target=stream, args=(msg["id"],), daemon=True).start()
'''


class FakeTime:
    """可控时钟：sleep(x) 按倍率推进假时钟（静默超时 300s 无需真实等待）。"""

    def __init__(self, accel=100.0):
        self._t = time.time()
        self._accel = accel

    def time(self):
        return self._t

    def monotonic(self):
        return self._t

    def sleep(self, s):
        self._t += s * self._accel
        time.sleep(0.001)


def _run_backend(monkeypatch, tmp_path, mode, stream_seconds="3",
                 max_exec_seconds=600, env_extra=None):
    """启动 fake hermes 子进程跑完整 stream_execute，返回全部事件列表。"""
    script = tmp_path / "fake_hermes.py"
    script.write_text(FAKE_HERMES_SCRIPT)
    # stream_execute 会执行 `<agent_cmd> acp` → 用可执行 wrapper 起 fake 脚本（忽略 acp 参数）
    wrapper = tmp_path / "fake_hermes_bin"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script}\n")
    wrapper.chmod(0o755)

    monkeypatch.setattr(ha.shutil, "which", lambda b: str(wrapper))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    monkeypatch.setenv("FAKE_HERMES_MODE", mode)
    monkeypatch.setenv("FAKE_HERMES_STREAM_SECONDS", stream_seconds)
    monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
    for k, v in (env_extra or {}).items():
        monkeypatch.setenv(k, v)

    # agent_cmd 追加 "acp" 参数 → fake 脚本忽略 argv 即可
    backend = ha.HermesACPBackend()
    events = []
    gen = backend.stream_execute(
        "prompt", str(tmp_path), session_id="",
        max_exec_seconds=max_exec_seconds, agent_label="[test] ",
    )
    for evt_type, data in gen:
        events.append((evt_type, data))
        if evt_type == "done":
            break
    return events


class TestGlobalTimeoutBehavior:
    def test_continuous_output_survives_old_double_cap(self, monkeypatch, tmp_path):
        """① 持续输出 3s > 旧上限 2×max_exec（max_exec=1 → 旧 2s 必切），新逻辑正常完成。"""
        events = _run_backend(monkeypatch, tmp_path, mode="stream_then_end",
                              stream_seconds="3", max_exec_seconds=1)
        finishes = [d for t, d in events if t == "step_finish"]
        dones = [d for t, d in events if t == "done"]
        assert dones, "应产出 done 事件"
        assert "stopReason=timeout" not in (finishes[0] if finishes else ""), \
            "持续输出的长任务不应被全局超时硬切"
        assert dones[0]["exit_code"] == 0, "正常结束应为 exit_code=0"
        texts = [d for t, d in events if t == "text"]
        assert texts, "应收到持续 text 输出"

    def test_env_runtime_task_timeout_effective(self, monkeypatch, tmp_path):
        """③ RUNTIME_TASK_TIMEOUT 配置生效：env=2s + 持续输出 → 全局超时 2s 触发。"""
        events = _run_backend(monkeypatch, tmp_path, mode="stream_forever",
                              stream_seconds="60", max_exec_seconds=1,
                              env_extra={"RUNTIME_TASK_TIMEOUT": "2"})
        finishes = [d for t, d in events if t == "step_finish"]
        dones = [d for t, d in events if t == "done"]
        assert finishes and "stopReason=timeout" in finishes[0], \
            "超过 env 配置的全局上限应触发 timeout"
        assert dones and dones[0]["exit_code"] == -1

    def test_silent_timeout_still_fires(self, monkeypatch, tmp_path):
        """② 静默超时语义不变：无任何输出 → _SILENT_TIMEOUT 仍触发 timeout 终止。"""
        # 可控时钟加速：sleep(0.05)×100 倍 → 300s 静默窗口约 60 次循环内命中
        fake_time = FakeTime(accel=100.0)
        monkeypatch.setattr(ha, "time", fake_time)
        monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
        events = _run_backend(monkeypatch, tmp_path, mode="silent",
                              stream_seconds="0", max_exec_seconds=600)
        finishes = [d for t, d in events if t == "step_finish"]
        dones = [d for t, d in events if t == "done"]
        assert finishes and "stopReason=timeout" in finishes[0], \
            "完全无输出时应由静默超时兜底终止（非全局超时）"
        assert dones and dones[0]["exit_code"] == -1
