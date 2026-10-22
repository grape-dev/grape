"""Issue #423：ACP 探活 _read_response deadline 驱动回归测试。

背景：三步探活（initialize → session/new → session/prompt）会话中，prompt 响应前
agent 持续输出 session/update 通知（实测 36 行）；旧版 _read_response 固定 10 行
上限在通知流中提前耗尽 → 误判「无响应」→ 引擎被误摘调度池。

用 fake ACP CLI（stdin/stdout JSON-RPC 的可执行脚本）端到端驱动探活，覆盖：
- 通知 ≥10 行（34+ 行 session/update）会话形态 → 不误报 fail
- stopReason=refusal → 独立标记 refused（与链路不通的 fail 区分）
- EOF / deadline 无响应 → fail
- 超过 _ACP_READ_MAX_LINES（500）防御上限 → 返回 None → fail
- hwcloud 三步全流程同类形态
"""
import json
import os
import stat
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="fake CLI 依赖 posix shebang 可执行脚本")

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault("GITCODE_API_TOKEN", "test_token")

_FAKE_CLI_TEMPLATE = '''#!/usr/bin/env python3
import sys, json, time

INIT_NOTES = {init_notes}
NEW_NOTES = {new_notes}
PROMPT_NOTES = {prompt_notes}
STOP = {stop!r}
EXIT_AFTER_INIT = {exit_after_init}
SILENT = {silent}
HALF_LINE = {half_line}
CHUNKED = {chunked}

def send(o):
    s = json.dumps(o)
    if CHUNKED:
        # 响应拆两块分批写（中间无换行）→ 校验探活侧行缓冲拼行不丢数据
        sys.stdout.write(s[:len(s) // 2])
        sys.stdout.flush()
        time.sleep(0.3)
        sys.stdout.write(s[len(s) // 2:])
    else:
        sys.stdout.write(s)
    sys.stdout.write("\\n")
    sys.stdout.flush()

if SILENT:
    for line in sys.stdin:
        pass
    sys.exit(0)

def notes(k):
    for i in range(k):
        send({{"jsonrpc": "2.0", "method": "session/update",
              "params": {{"update": {{"sessionUpdate": "agent_status_chunk"}}}}}})

for line in sys.stdin:
    try:
        msg = json.loads(line)
    except Exception:
        continue
    m = msg.get("method")
    if m == "initialize":
        notes(INIT_NOTES)
        send({{"jsonrpc": "2.0", "id": msg["id"], "result": {{"sessionId": "probe-s"}}}})
        if HALF_LINE:
            # QA 复核 Optional#1 复现形态：initialize 响应后写半行 JSON（无换行）永久停住
            sys.stdout.write('{{"jsonrpc": "2.0", "id": 2, "resu')
            sys.stdout.flush()
            time.sleep(3600)
        if EXIT_AFTER_INIT:
            sys.exit(0)
    elif m == "session/new":
        notes(NEW_NOTES)
        send({{"jsonrpc": "2.0", "id": msg["id"], "result": {{"sessionId": "s-1"}}}})
    elif m == "session/prompt":
        notes(PROMPT_NOTES)
        send({{"jsonrpc": "2.0", "id": msg["id"], "result": {{"stopReason": STOP}}}})
'''


def _make_fake_cli(tmp_path, *, init_notes=0, new_notes=0, prompt_notes=0,
                   stop="end_turn", exit_after_init=False, silent=False,
                   half_line=False, chunked=False):
    path = tmp_path / "fake-acp-cli"
    path.write_text(
        _FAKE_CLI_TEMPLATE.format(
            init_notes=init_notes, new_notes=new_notes, prompt_notes=prompt_notes,
            stop=stop, exit_after_init=exit_after_init, silent=silent,
            half_line=half_line, chunked=chunked,
        )
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


class TestHermesProbeReadResponse:
    """hermes ACP 探活（_probe_hermes_acp）。"""

    def test_ok_with_34_notifications_before_prompt(self, tmp_path):
        """Issue #423 核心回归：prompt 响应前 34 行 session/update 通知（三步会话实测形态）→ ok。

        旧版 for _ in range(10) 在第 10 行耗尽额度返回 None → 误报「无响应」。
        """
        from src.runtime.agent import _probe_hermes_acp
        cli = _make_fake_cli(tmp_path, prompt_notes=34, stop="end_turn")
        result = _probe_hermes_acp(cli)
        assert result["status"] == "ok"
        assert "stopReason=end_turn" in result["output"]

    def test_ok_with_exactly_10_notifications(self, tmp_path):
        """10 行（旧上限边界）通知 + 响应 → ok。"""
        from src.runtime.agent import _probe_hermes_acp
        cli = _make_fake_cli(tmp_path, prompt_notes=10, stop="end_turn")
        result = _probe_hermes_acp(cli)
        assert result["status"] == "ok"

    def test_refusal_marked_refused_not_fail(self, tmp_path):
        """stopReason=refusal → 独立 refused 标记，与链路不通的 fail 区分。"""
        from src.runtime.agent import _probe_hermes_acp
        cli = _make_fake_cli(tmp_path, prompt_notes=5, stop="refusal")
        result = _probe_hermes_acp(cli)
        assert result["status"] == "refused"
        assert result["error"] == ""

    def test_eof_after_initialize_reports_fail(self, tmp_path):
        """EOF（进程退出）→ 立即返回 None → fail（不空等到 deadline）。"""
        from src.runtime.agent import _probe_hermes_acp
        cli = _make_fake_cli(tmp_path, exit_after_init=True)
        result = _probe_hermes_acp(cli)
        assert result["status"] == "fail"
        assert "no response" in result["error"]

    def test_defensive_line_cap_returns_none(self, tmp_path):
        """超过 _ACP_READ_MAX_LINES（500）防御上限 → None → fail（防失控流保险丝）。"""
        from src.runtime.agent import _probe_hermes_acp
        cli = _make_fake_cli(tmp_path, prompt_notes=600, stop="end_turn")
        result = _probe_hermes_acp(cli)
        assert result["status"] == "fail"
        assert "no response" in result["error"]

    def test_deadline_no_response_reports_fail(self, tmp_path, monkeypatch):
        """无任何响应 → deadline 驱动超时返回 None → fail（缩短超时避免慢测）。"""
        from src.runtime import agent as _agent_mod
        monkeypatch.setattr(_agent_mod, "_ACP_PROBE_TIMEOUT", 1)
        monkeypatch.setattr(_agent_mod, "_ACP_PROMPT_TIMEOUT", 1)
        result = _agent_mod._probe_hermes_acp(_make_fake_cli(tmp_path, silent=True))
        assert result["status"] == "fail"
        assert "no response" in result["error"]

    def test_half_line_no_newline_returns_at_deadline(self, tmp_path, monkeypatch):
        """QA 复核 Optional#1（升级修复）：select 就绪后 readline 遇半行未换行数据
        会阻塞在 read 系统调用——deadline 只约束 select、管不到 readline。

        旧实现（readline）在此形态实测 >10s 不返回（deadline=2s），探测线程永久
        停摆；修复后（非阻塞 fd + os.read 小块读）必须在 deadline 附近返回 fail。
        """
        from src.runtime import agent as _agent_mod
        monkeypatch.setattr(_agent_mod, "_ACP_PROBE_TIMEOUT", 1)
        monkeypatch.setattr(_agent_mod, "_ACP_PROMPT_TIMEOUT", 1)
        cli = _make_fake_cli(tmp_path, half_line=True)
        _t0 = time.monotonic()
        result = _agent_mod._probe_hermes_acp(cli)
        _elapsed = time.monotonic() - _t0
        assert result["status"] == "fail"
        assert "no response" in result["error"]
        # deadline=2s（init 1s + prompt 1s）；留裕量：必须在 3s 内返回，不得挂起
        assert _elapsed < 3.0, f"探活在半行数据上挂起/超时未按 deadline 返回: {_elapsed:.1f}s"

    def test_chunked_response_lines_assembled(self, tmp_path):
        """行缓冲拼行回归：响应分两块到达（中间无换行）→ 拼行正确、不丢数据。

        os.read 小块读改造后，一行可能跨多个 read chunk；行缓冲必须完整拼行。
        """
        from src.runtime.agent import _probe_hermes_acp
        cli = _make_fake_cli(tmp_path, chunked=True, stop="end_turn")
        result = _probe_hermes_acp(cli)
        assert result["status"] == "ok"
        assert "stopReason=end_turn" in result["output"]


class TestHwcloudProbeReadResponse:
    """hwcloud ACP 探活（_probe_hwcloud_acp）同步审查回归。"""

    def test_three_step_ok_with_notifications(self, tmp_path):
        """三步全流程（initialize → session/new → prompt）含 35 行通知 → ok。"""
        from src.runtime.agent import _probe_hwcloud_acp
        cli = _make_fake_cli(tmp_path, init_notes=1, new_notes=34, prompt_notes=0,
                             stop="end_turn")
        result = _probe_hwcloud_acp(cli)
        assert result["status"] == "ok"
        assert "stopReason=end_turn" in result["output"]

    def test_refusal_marked_refused_not_fail(self, tmp_path):
        """stopReason=refusal → refused 独立标记（与 hermes 同构）。"""
        from src.runtime.agent import _probe_hwcloud_acp
        cli = _make_fake_cli(tmp_path, new_notes=12, stop="refusal")
        result = _probe_hwcloud_acp(cli)
        assert result["status"] == "refused"
        assert result["error"] == ""

    def test_defensive_line_cap_returns_none(self, tmp_path):
        """防御上限（500 行）对 hwcloud 同样生效。"""
        from src.runtime.agent import _probe_hwcloud_acp
        cli = _make_fake_cli(tmp_path, new_notes=600, stop="end_turn")
        result = _probe_hwcloud_acp(cli)
        assert result["status"] == "fail"

    def test_half_line_no_newline_returns_at_deadline(self, tmp_path, monkeypatch):
        """QA 复核 Optional#1 同构修复：hwcloud readline 同样存在半行阻塞暴露面。

        修复后（非阻塞 fd + os.read）半行悬置数据必须在 deadline 附近返回 fail，
        不得阻塞探测线程。
        """
        from src.runtime import agent as _agent_mod
        monkeypatch.setattr(_agent_mod, "_ACP_PROBE_TIMEOUT", 1)
        monkeypatch.setattr(_agent_mod, "_ACP_PROMPT_TIMEOUT", 1)
        cli = _make_fake_cli(tmp_path, half_line=True)
        _t0 = time.monotonic()
        result = _agent_mod._probe_hwcloud_acp(cli)
        _elapsed = time.monotonic() - _t0
        assert result["status"] == "fail"
        assert "no response" in result["error"]
        assert _elapsed < 3.0, f"hwcloud 探活半行挂起未按 deadline 返回: {_elapsed:.1f}s"

    def test_chunked_response_lines_assembled(self, tmp_path):
        """行缓冲拼行回归（hwcloud 同构）：分块响应 → 拼行正确。"""
        from src.runtime.agent import _probe_hwcloud_acp
        cli = _make_fake_cli(tmp_path, chunked=True, stop="end_turn")
        result = _probe_hwcloud_acp(cli)
        assert result["status"] == "ok"
        assert "stopReason=end_turn" in result["output"]


class TestRefusedNotCountedAsRemoval:
    """refused 语义：服务端心跳落库对非 ok/fail 状态不计入连败摘除（既有逻辑，回归锚定）。"""

    def test_read_max_lines_constant(self):
        from src.runtime.agent import _ACP_READ_MAX_LINES
        assert _ACP_READ_MAX_LINES == 500

    def test_refused_is_neither_ok_nor_fail(self, tmp_path):
        """refused 必须既非 ok 也非 fail——保证走服务端 else 分支（不动 healthy）。"""
        from src.runtime.agent import _probe_hermes_acp
        cli = _make_fake_cli(tmp_path, stop="refusal")
        result = _probe_hermes_acp(cli)
        assert result["status"] not in ("ok", "fail")
        assert json.dumps(result)  # 可序列化上报
