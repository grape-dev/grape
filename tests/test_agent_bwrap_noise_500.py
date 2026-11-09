"""Issue #500：bwrap wrap failed 告警噪音治理回归测试。

背景：worker 日志被 `hermes ACP bwrap wrap failed, running unsandboxed:
cannot import name 'build_bwrap_cmd' from 'src.agent.sandbox'` 刷屏——
部署环境 sandbox 为陈旧包布局（sandbox/__init__.py 无导出），三个调用点
（hermes_acp / hwcloud_acp / executor）不看配置先 try-import 再逐条告警。

三态契约（每调用点）：
- 关闭（GRAPE_AGENT_SANDBOX=off）：即使 sandbox 导入链路损坏（复现部署症状），
  0 条 wrap failed 告警、命令不包装、不进入导入路径；
- 开启 + 包装成功：命令带 bwrap 前缀、info 提示、无告警（不回归）；
- 开启 + 包装/导入失败：命令降级 unsandboxed，WARNING 恰 1 条（log-once），
  后续降 DEBUG（不刷屏、不静默）。

另守卫根因修复：src.agent.sandbox 必须以包布局发布且导出 build_bwrap_cmd
（部署按文件覆盖时覆写陈旧 __init__.py）。
"""
import json
import logging
import os
import sys

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="fake CLI 依赖 posix shebang 可执行脚本")

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault("GITCODE_API_TOKEN", "test_token")

from src.runtime.agent import _backend_for_engine  # noqa: F401,E402  先入 runtime 打破循环导入
import src.config  # noqa: E402
import src.agent.hermes_acp as ha  # noqa: E402
import src.agent.hwcloud_acp as hwa  # noqa: E402
import src.agent.executor as ex  # noqa: E402
from src.runtime import stream_runtime  # noqa: E402

_BWRAP_PREFIX = ["/usr/bin/bwrap", "--die-with-parent", "--unshare-all", "--share-net", "--"]
_FAIL_SUBSTR = "bwrap wrap failed, running unsandboxed"
_WRAP_OK_SUBSTR = "sandboxed via bwrap"


def _bwrap_fail_records(records):
    return [r for r in records if _FAIL_SUBSTR in r.getMessage()]


# ── fake ACP CLI（hermes/hwcloud 共用：initialize → session/new → session/prompt）──

FAKE_ACP_SCRIPT = r'''
import json, sys, threading

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
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "sess_fake_500"}})
    elif method == "session/prompt":
        threading.Thread(target=lambda: (
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"text": "done\n"}}}}),
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}}),
        ), daemon=True).start()
'''


def _run_acp_backend(mod, backend_cls, monkeypatch, tmp_path, popen_cmds):
    """fake ACP CLI 端到端驱动 stream_execute 至 done；记录每次 agent 子进程 argv。"""
    script = tmp_path / "fake_acp_cli.py"
    script.write_text(FAKE_ACP_SCRIPT)
    wrapper = tmp_path / "fake_acp_bin"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script}\n")
    wrapper.chmod(0o755)

    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))
    monkeypatch.setattr(mod.shutil, "which", lambda b: str(wrapper))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fake_hermes_home"))
    monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
    for k in ("HERMES_INIT_TIMEOUT", "HERMES_INIT_RETRIES",
              "HERMES_STDERR_TAIL_BYTES", "HERMES_SILENT_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)

    real_popen = mod.subprocess.Popen

    def rec_popen(cmd, *a, **k):
        popen_cmds.append(list(cmd))
        return real_popen(cmd, *a, **k)

    monkeypatch.setattr(mod.subprocess, "Popen", rec_popen)

    backend = backend_cls()
    done = None
    for evt_type, data in backend.stream_execute(
        "prompt", str(tmp_path), session_id="",
        max_exec_seconds=120, agent_label="[t] ",
    ):
        if evt_type == "done":
            done = data
            break
    assert done is not None, "应产出 done 事件"
    return done


def _agent_cmds(popen_cmds):
    """过滤出 agent 子进程 argv（排除探活等其它 spawn）。"""
    return [c for c in popen_cmds if c and (c[-1] in ("acp", "--acp"))]


def _assert_three_states(runner, extract_cmds, cmd_tail_check, monkeypatch, tmp_path, caplog):
    """三态共用断言；runner(popen_cmds) 驱动一次执行。"""
    # log-once 标志为模块级状态：每个测试统一复位，防跨测试串扰
    for _mod in (ha, hwa, ex):
        monkeypatch.setattr(_mod, "_bwrap_fail_notified", False, raising=False)

    def run(sandbox_on, break_import):
        popen_cmds = []
        caplog.clear()
        monkeypatch.setattr(src.config, "GRAPE_AGENT_SANDBOX", sandbox_on)
        if break_import:
            monkeypatch.delattr("src.agent.sandbox.build_bwrap_cmd", raising=False)
        else:
            monkeypatch.setattr(
                "src.agent.sandbox.build_bwrap_cmd",
                lambda cwd, backend_name="": list(_BWRAP_PREFIX), raising=False)
        runner(popen_cmds)
        return extract_cmds(popen_cmds), list(caplog.records)

    # ① 关闭：即使 sandbox 导入链路损坏，0 告警、不包装
    cmds, records = run(False, True)
    assert cmds, "应捕获 agent 子进程 argv"
    for c in cmds:
        assert c[0] != _BWRAP_PREFIX[0], f"关闭态不得包装: {c}"
        cmd_tail_check(c)
    assert not _bwrap_fail_records(records), \
        f"关闭态应 0 条 wrap failed 告警，实得 {[r.getMessage() for r in _bwrap_fail_records(records)]}"

    # ② 开启 + 成功：包装前缀 + info 提示、无告警
    cmds, records = run(True, False)
    assert cmds and all(c[:len(_BWRAP_PREFIX)] == _BWRAP_PREFIX for c in cmds), \
        f"开启态命令应带 bwrap 前缀: {cmds}"
    assert not _bwrap_fail_records(records), "开启成功不应有 wrap failed 告警"
    assert any(_WRAP_OK_SUBSTR in r.getMessage() for r in records), \
        "开启成功应有 sandboxed via bwrap 提示"

    # ③ 开启 + 失败（导入链路损坏复现部署症状）：降级执行 + WARNING 恰 1 条
    popen_cmds = []
    caplog.clear()
    monkeypatch.setattr(src.config, "GRAPE_AGENT_SANDBOX", True)
    monkeypatch.delattr("src.agent.sandbox.build_bwrap_cmd", raising=False)
    runner(popen_cmds)
    runner(popen_cmds)
    cmds = extract_cmds(popen_cmds)
    assert len(cmds) >= 2, "应执行两次以验证 log-once"
    for c in cmds:
        assert c[0] != _BWRAP_PREFIX[0], f"失败态应降级 unsandboxed: {c}"
        cmd_tail_check(c)
    warns = [r for r in _bwrap_fail_records(caplog.records) if r.levelno == logging.WARNING]
    assert len(warns) == 1, \
        f"失败告警应恰好 1 条（log-once），实得 {len(warns)}: {[r.getMessage() for r in warns]}"
    debugs = [r for r in _bwrap_fail_records(caplog.records) if r.levelno == logging.DEBUG]
    assert len(debugs) >= 1, "重复失败应降为 DEBUG，不得静默"


# ── hermes ACP 三态 ────────────────────────────────────────────────────

class TestHermesAcpBwrapNoise:

    def test_three_states(self, monkeypatch, tmp_path, caplog):
        caplog.set_level(logging.DEBUG, logger="devops-bot")

        def runner(popen_cmds):
            _run_acp_backend(ha, ha.HermesACPBackend, monkeypatch, tmp_path, popen_cmds)

        def cmd_tail_check(c):
            assert c[-1] == "acp", f"hermes ACP 命令尾应为 acp: {c}"

        _assert_three_states(runner, _agent_cmds, cmd_tail_check, monkeypatch, tmp_path, caplog)


# ── hwcloud ACP 三态 ───────────────────────────────────────────────────

class TestHwcloudAcpBwrapNoise:

    def test_three_states(self, monkeypatch, tmp_path, caplog):
        caplog.set_level(logging.DEBUG, logger="devops-bot")

        def runner(popen_cmds):
            _run_acp_backend(hwa, hwa.HwcloudACPBackend, monkeypatch, tmp_path, popen_cmds)

        def cmd_tail_check(c):
            assert c[-2:] == ["serve", "--acp"], f"hwcloud ACP 命令尾应为 serve --acp: {c}"

        _assert_three_states(runner, _agent_cmds, cmd_tail_check, monkeypatch, tmp_path, caplog)


# ── executor（stream_runtime）三态 ─────────────────────────────────────

def _run_executor_once(popen_cmds):
    """FakePopen 驱动 stream_runtime 至 done（沿 test_agent_executor 先例）。"""
    from unittest.mock import patch

    fake_lines = [
        json.dumps({"type": "step_start", "sessionID": "s1", "part": {"step": 1}}),
        json.dumps({"type": "text", "part": {"text": "ok", "type": "text"}}),
        json.dumps({"type": "step_finish", "part": {"tokens": {"total": 1}}}),
    ]

    class FakeProc:
        returncode = 0

        def __init__(self, *a, **k):
            self._lines = list(fake_lines)
            self._i = 0
            self.stdout = self
            self.stderr = self

        def readline(self):
            if self._i < len(self._lines):
                line = self._lines[self._i]
                self._i += 1
                return line.encode() + b"\n"
            return b""

        def wait(self, timeout=None):
            return 0

        def read(self):
            return b""

    def fake_popen(cmd, *a, **k):
        popen_cmds.append(list(cmd))
        return FakeProc(cmd, *a, **k)

    with patch("src.agent.executor.subprocess.Popen", side_effect=fake_popen), \
         patch("src.runtime.os_adapter.find_cli", return_value="/usr/bin/opencode"):
        events = list(stream_runtime("p", os.getcwd(), "", "issue"))
    assert [d for et, d in events if et == "done"], "应产出 done 事件"


class TestExecutorBwrapNoise:

    def test_three_states(self, monkeypatch, tmp_path, caplog):
        caplog.set_level(logging.DEBUG, logger="devops-bot")

        def runner(popen_cmds):
            _run_executor_once(popen_cmds)

        def cmd_tail_check(c):
            assert "run" in c, f"executor 命令应含 run: {c}"

        _assert_three_states(runner, lambda cmds: cmds, cmd_tail_check, monkeypatch, tmp_path, caplog)


# ── 根因守卫：sandbox 包布局 + 导出 ────────────────────────────────────

class TestSandboxPackage:

    def test_exports_and_package_layout(self):
        """#500 根因：sandbox 必须是包且 __init__ 导出 build_bwrap_cmd。

        部署环境曾以陈旧 sandbox/__init__.py（无导出）遮蔽模块文件，导致
        cannot import name；包布局使部署按文件覆盖时必然覆写该 __init__。
        """
        import src.agent.sandbox as sbx
        from src.agent.sandbox import build_bwrap_cmd, sandbox_enabled
        assert callable(build_bwrap_cmd)
        assert callable(sandbox_enabled)
        assert sbx.__file__.endswith(os.path.join("sandbox", "__init__.py")), \
            f"应为包布局 sandbox/__init__.py: {sbx.__file__}"
