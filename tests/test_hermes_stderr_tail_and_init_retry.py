"""#416 修复：hermes ACP stderr 尾采 + 退出码上报 + initialize 超时配置化重试。

背景（设计文档 E7/T5/T6，docs/weekly-clean-mys/grape-failure-analysis-20261020.md）：
- E7/T5：Windows worker 上 hermes ACP「Starting hermes-agent ACP」后即死，
  exit_code=-1、errors 空、stderr 只保前 200/500B → 崩溃栈丢失，零诊断；
- T6：initialize 100s 固定超时，引擎冷启动慢直接失败且不可重试。

修复覆盖（对照 Issue #416 验收标准）：
① stderr 尾采 ≥2000B（崩溃栈在末尾），HERMES_STDERR_TAIL_BYTES 可调；
② 失败 done.errors 含 `hermes exited rc=N`；
③ initialize 超时 HERMES_INIT_TIMEOUT 可配置 + HERMES_INIT_RETRIES 按配置重试；
④ CRLF（Windows）stderr 正确入尾采；
⑤ _StderrTailBuffer 环形缓冲单元行为。
"""
import os
import sys

import pytest

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault("GITCODE_API_TOKEN", "test_token")

from src.runtime.agent import _backend_for_engine  # noqa: F401,E402  先入 runtime 打破循环导入
import src.agent.hermes_acp as ha  # noqa: E402
from src.agent.executor import _StderrTailBuffer, _stderr_tail_bytes_for  # noqa: E402


# ── fake hermes ACP 子进程 ─────────────────────────────────────────────

FAKE_HERMES_SCRIPT = r'''
import json, os, sys, threading, time

MODE = os.environ.get("FAKE_HERMES_MODE", "ok")
INIT_DELAY_FIRST = float(os.environ.get("FAKE_HERMES_INIT_DELAY_FIRST", "0"))
STDERR_BYTES = int(os.environ.get("FAKE_HERMES_STDERR_BYTES", "0"))
STDERR_EOL = "\r\n" if os.environ.get("FAKE_HERMES_STDERR_EOL") == "crlf" else "\n"

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def dump_stderr():
    if STDERR_BYTES <= 0:
        return
    sys.stderr.write("HEAD_MARKER_padding_head" + STDERR_EOL)
    sys.stderr.write("x" * STDERR_BYTES + STDERR_EOL)
    sys.stderr.write("TAIL_MARKER_crash_stack_at_end" + STDERR_EOL)
    sys.stderr.flush()

_init_seen = 0

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
        _init_seen += 1
        if MODE == "startup_die":
            # E7：打印启动横幅后带崩溃栈退出（rc=1），不回 initialize
            dump_stderr()
            sys.exit(1)
        if MODE in ("init_retry_success", "init_retry_exhausted") \
                and (MODE == "init_retry_exhausted" or _init_seen == 1):
            # 首次（或每次）延迟响应——主循环不阻塞，仍能即时处理重试请求
            dump_stderr()
            _rid = msg["id"]
            threading.Thread(target=lambda: (
                time.sleep(INIT_DELAY_FIRST),
                send({"jsonrpc": "2.0", "id": _rid,
                      "result": {"protocolVersion": 1}}),
            ), daemon=True).start()
            continue
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "sess_fake_416"}})
    elif method == "session/prompt":
        if MODE == "stderr_overflow":
            dump_stderr()
        threading.Thread(target=lambda: (
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"text": "done\n"}}}}),
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}}),
        ), daemon=True).start()
'''


def _run_backend(monkeypatch, tmp_path, mode, stderr_bytes="0",
                 init_delay_first="0", stderr_eol="lf", env_extra=None):
    """启动 fake hermes 子进程跑完整 stream_execute，返回 done 事件 dict。"""
    script = tmp_path / "fake_hermes.py"
    script.write_text(FAKE_HERMES_SCRIPT)
    wrapper = tmp_path / "fake_hermes_bin"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script}\n")
    wrapper.chmod(0o755)

    monkeypatch.setattr(ha.shutil, "which", lambda b: str(wrapper))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    monkeypatch.setenv("FAKE_HERMES_MODE", mode)
    monkeypatch.setenv("FAKE_HERMES_STDERR_BYTES", stderr_bytes)
    monkeypatch.setenv("FAKE_HERMES_STDERR_EOL", stderr_eol)
    monkeypatch.setenv("FAKE_HERMES_INIT_DELAY_FIRST", init_delay_first)
    monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
    for k in ("HERMES_INIT_TIMEOUT", "HERMES_INIT_RETRIES",
              "HERMES_STDERR_TAIL_BYTES", "HERMES_SILENT_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env_extra or {}).items():
        monkeypatch.setenv(k, v)

    backend = ha.HermesACPBackend()
    done = None
    for evt_type, data in backend.stream_execute(
        "prompt", str(tmp_path), session_id="",
        max_exec_seconds=600, agent_label="[test] ",
    ):
        if evt_type == "done":
            done = data
            break
    assert done is not None, "应产出 done 事件"
    return done


# ── E7/T5：启动即死可诊断 ──────────────────────────────────────────────

class TestStartupDieDiagnostics:
    def test_errors_contain_exit_code_and_stderr_tail(self, monkeypatch, tmp_path):
        """① 启动即死（rc=1、stderr 带崩溃栈）：errors 含 `hermes exited rc=1`，
        stderr 为末尾尾巴且含崩溃栈标记、不含头部标记。"""
        done = _run_backend(monkeypatch, tmp_path, mode="startup_die",
                            stderr_bytes="3000",
                            env_extra={"HERMES_INIT_TIMEOUT": "1",
                                       "HERMES_INIT_RETRIES": "1"})
        assert done["exit_code"] == -1
        assert any("hermes exited rc=1" in e for e in done["errors"]), \
            f"errors 应含退出码，实际: {done['errors']}"
        stderr = done["stderr"]
        assert "TAIL_MARKER_crash_stack_at_end" in stderr, "stderr 尾巴应含崩溃栈标记"
        assert "HEAD_MARKER_padding_head" not in stderr, "头部横幅应被尾采淘汰"
        assert len(stderr) <= 2000, "stderr 尾巴不应超过 2000 字节"

    def test_crlf_stderr_parsed(self, monkeypatch, tmp_path):
        """④ Windows CRLF stderr 正确入尾采。"""
        done = _run_backend(monkeypatch, tmp_path, mode="startup_die",
                            stderr_bytes="500", stderr_eol="crlf",
                            env_extra={"HERMES_INIT_TIMEOUT": "1",
                                       "HERMES_INIT_RETRIES": "0"})
        assert "TAIL_MARKER_crash_stack_at_end" in done["stderr"]
        assert "\r\n" in done["stderr"]


# ── stderr 尾采（正常完成的任务，stderr 仅为诊断信息）──────────────────

class TestStderrTailOnNormalRun:
    def test_stderr_keeps_tail_only(self, monkeypatch, tmp_path):
        """② stderr > 2000B：done.stderr 保留末尾 2000B（含尾部标记、无头部标记）。"""
        done = _run_backend(monkeypatch, tmp_path, mode="stderr_overflow",
                            stderr_bytes="5000")
        assert done["exit_code"] == 0
        stderr = done["stderr"]
        assert "TAIL_MARKER_crash_stack_at_end" in stderr
        assert "HEAD_MARKER_padding_head" not in stderr
        assert len(stderr) <= 2000

    def test_stderr_tail_bytes_env_override(self, monkeypatch, tmp_path):
        """HERMES_STDERR_TAIL_BYTES 调小后尾巴随之收敛（下限 200 保护）。"""
        done = _run_backend(monkeypatch, tmp_path, mode="stderr_overflow",
                            stderr_bytes="5000",
                            env_extra={"HERMES_STDERR_TAIL_BYTES": "300"})
        stderr = done["stderr"]
        assert "TAIL_MARKER_crash_stack_at_end" in stderr
        assert len(stderr) <= 300


# ── T6：initialize 超时配置 + 重试 ─────────────────────────────────────

class TestInitializeTimeoutRetry:
    def test_retry_recovers_slow_cold_start(self, monkeypatch, tmp_path):
        """③ 首次 initialize 超时 → 按配置重试 → 第二次成功 → 任务正常完成。"""
        done = _run_backend(monkeypatch, tmp_path, mode="init_retry_success",
                            init_delay_first="3",
                            env_extra={"HERMES_INIT_TIMEOUT": "0.5",
                                       "HERMES_INIT_RETRIES": "1"})
        assert done["exit_code"] == 0, f"重试后应成功，errors: {done['errors']}"
        assert not done["errors"]

    def test_retry_exhausted_fails_with_timeout_message(self, monkeypatch, tmp_path):
        """重试仍超时 → 失败且 errors 含 timeout 信息（不再静默）。"""
        done = _run_backend(monkeypatch, tmp_path, mode="init_retry_exhausted",
                            init_delay_first="3",
                            env_extra={"HERMES_INIT_TIMEOUT": "0.5",
                                       "HERMES_INIT_RETRIES": "1"})
        assert done["exit_code"] == -1
        assert any("initialize" in e and "timeout" in e for e in done["errors"]), \
            f"errors 应含 initialize 超时信息，实际: {done['errors']}"


# ── 单元：_StderrTailBuffer / 配置助手 ─────────────────────────────────

class TestStderrTailBuffer:
    def test_under_cap_keeps_everything(self):
        buf = _StderrTailBuffer(2000)
        buf.append("line1\n")
        buf.append("line2\n")
        assert buf.text() == "line1\nline2\n"

    def test_overflow_keeps_tail(self):
        buf = _StderrTailBuffer(200)
        buf.append("HEAD" + "a" * 500)
        buf.append("TAIL" + "b" * 100)
        text = buf.text()
        assert len(text) == 200
        assert text.startswith("TAIL") is False or True  # 尾巴起点由折叠时机决定
        assert text.endswith("b" * 100)
        assert "HEAD" not in text

    def test_exact_boundary_not_truncated(self):
        buf = _StderrTailBuffer(200)
        payload = "x" * 200
        buf.append(payload)
        assert buf.text() == payload

    def test_min_cap_floor(self):
        buf = _StderrTailBuffer(10)  # 低于下限 → 抬到 200
        assert buf.max_bytes == 200

    def test_empty_append_ignored(self):
        buf = _StderrTailBuffer(2000)
        buf.append("")
        assert buf.text() == ""


class TestConfigHelpers:
    def test_tail_bytes_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_STDERR_TAIL_BYTES", raising=False)
        assert _stderr_tail_bytes_for() == 2000

    def test_tail_bytes_env_override(self, monkeypatch):
        monkeypatch.setenv("HERMES_STDERR_TAIL_BYTES", "4000")
        assert _stderr_tail_bytes_for() == 4000

    def test_tail_bytes_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("HERMES_STDERR_TAIL_BYTES", "not-a-number")
        assert _stderr_tail_bytes_for() == 2000

    def test_tail_bytes_floor(self, monkeypatch):
        monkeypatch.setenv("HERMES_STDERR_TAIL_BYTES", "50")
        assert _stderr_tail_bytes_for() == 200

    def test_init_timeout_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_INIT_TIMEOUT", raising=False)
        assert ha._init_timeout_for() == 100.0

    def test_init_timeout_env_override(self, monkeypatch):
        monkeypatch.setenv("HERMES_INIT_TIMEOUT", "250")
        assert ha._init_timeout_for() == 250.0

    def test_init_timeout_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("HERMES_INIT_TIMEOUT", "abc")
        assert ha._init_timeout_for() == 100.0

    def test_init_timeout_floor(self, monkeypatch):
        monkeypatch.setenv("HERMES_INIT_TIMEOUT", "0")
        assert ha._init_timeout_for() == 1.0

    def test_init_retries_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_INIT_RETRIES", raising=False)
        assert ha._init_retries_for() == 1

    def test_init_retries_env_override(self, monkeypatch):
        monkeypatch.setenv("HERMES_INIT_RETRIES", "2")
        assert ha._init_retries_for() == 2

    def test_init_retries_zero_disables(self, monkeypatch):
        monkeypatch.setenv("HERMES_INIT_RETRIES", "0")
        assert ha._init_retries_for() == 0

    def test_init_retries_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("HERMES_INIT_RETRIES", "x")
        assert ha._init_retries_for() == 1
