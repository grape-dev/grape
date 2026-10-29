"""Issue #494：超时类失败必须有明确归因（errors/事件流/前端透出）。

覆盖：
① timeout_error_text / process_exit_attribution / failure_summary 单元行为；
② hermes ACP stopReason=timeout（静默超时）→ done.errors 含「执行超时（Ns）」；
③ 回归：非超时失败（exit!=0、stderr 空）归因不丢失（#309/#416 口径对齐）。

fake 子进程 harness 复用 tests/test_hermes_stderr_tail_and_init_retry.py 模式。
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
from src.agent.failure_attribution import (  # noqa: E402
    failure_summary,
    process_exit_attribution,
    timeout_error_text,
)


# ── ① 归因文案 helper 单元 ─────────────────────────────────────────────

class TestTimeoutErrorText:
    def test_contains_explicit_timeout_phrase(self):
        """验收口径：errors 含「执行超时（600s）」明确归因。"""
        msg = timeout_error_text(600, "hermes ACP stopReason=timeout")
        assert "执行超时（600s）" in msg
        assert "stopReason=timeout" in msg

    def test_seconds_rounded_and_optional_detail(self):
        assert timeout_error_text(300.4) == "执行超时（300s）"
        assert timeout_error_text(900) .endswith("（900s）")

    def test_invalid_seconds_falls_back_zero(self):
        assert "执行超时（0s）" in timeout_error_text("abc", "x")


class TestProcessExitAttribution:
    def test_returncode_124_is_timeout(self):
        """timeout 命令杀死（rc=124）→ 执行超时归因（Issue #494 实测形态）。"""
        attr = process_exit_attribution(124)
        assert attr and "执行超时" in attr and "124" in attr

    def test_sigterm_flagged_as_suspected_timeout(self):
        attr = process_exit_attribution(-15)
        assert attr and "SIGTERM" in attr

    def test_non_timeout_returns_none(self):
        """回归：非超时退出码不构造超时归因（#309 遗言补采口径不受影响）。"""
        assert process_exit_attribution(0) is None
        assert process_exit_attribution(1) is None
        assert process_exit_attribution(-9) is None


class TestFailureSummary:
    def test_summary_from_errors_with_exit_code(self):
        s = failure_summary(["执行超时（600s）：hermes ACP stopReason=timeout"], -1)
        assert "exit_code=-1" in s and "执行超时（600s）" in s

    def test_empty_errors_returns_empty(self):
        assert failure_summary([], 1) == ""
        assert failure_summary(None) == ""

    def test_truncates_and_notes_overflow(self):
        errs = [f"e{i}" for i in range(6)]
        s = failure_summary(errs, 1)
        assert "另有 3 条" in s and "e5" not in s.split("；")[-1]


# ── ② fake hermes ACP：静默超时 → errors 显式归因 ─────────────────────

FAKE_HERMES_SCRIPT = r'''
import json, os, sys

MODE = os.environ.get("FAKE_HERMES_MODE_494", "silent_hang")

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

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
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "sess_fake_494"}})
    elif method == "session/prompt":
        if MODE == "silent_hang":
            # 模拟模型流挂死：prompt 后永远不回 PromptResponse、无任何 update
            pass
        elif MODE == "non_timeout_die":
            # 回归：非超时失败——stderr 空直接退出 rc=1（#309/#416 形态）
            sys.exit(1)
'''


def _run_backend_494(monkeypatch, tmp_path, mode, env_extra=None):
    script = tmp_path / "fake_hermes_494.py"
    script.write_text(FAKE_HERMES_SCRIPT)
    wrapper = tmp_path / "fake_hermes_bin_494"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script}\n")
    wrapper.chmod(0o755)

    monkeypatch.setattr(ha.shutil, "which", lambda b: str(wrapper))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    monkeypatch.setenv("FAKE_HERMES_MODE_494", mode)
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
        max_exec_seconds=600, agent_label="[test494] ",
    ):
        if evt_type == "done":
            done = data
            break
    assert done is not None, "应产出 done 事件"
    return done


class TestHermesTimeoutAttribution:
    def test_silent_timeout_errors_contain_explicit_reason(self, monkeypatch, tmp_path):
        """#494 验收：stopReason=timeout 时 errors 非空且含「执行超时（Ns）」。"""
        done = _run_backend_494(monkeypatch, tmp_path, mode="silent_hang",
                                env_extra={"HERMES_SILENT_TIMEOUT": "2"})
        assert done["exit_code"] == -1
        assert done["errors"], "超时失败 errors 不得为空（旧缺陷：双空零归因）"
        assert any("执行超时" in str(e) and "（0s" not in str(e) for e in done["errors"]), \
            f"errors 应含明确超时归因，实际: {done['errors']}"

    def test_non_timeout_failure_attribution_not_lost(self, monkeypatch, tmp_path):
        """回归：非超时失败（rc=1、stderr 空）仍走 #309/#416 遗言归因，不被超时文案淹没。"""
        done = _run_backend_494(monkeypatch, tmp_path, mode="non_timeout_die",
                                env_extra={"HERMES_SILENT_TIMEOUT": "30"})
        assert done["exit_code"] != 0
        assert done["errors"], "非超时失败 errors 也不得为空"
        assert not any("执行超时" in str(e) for e in done["errors"]), \
            f"非超时失败不应误标超时归因，实际: {done['errors']}"
