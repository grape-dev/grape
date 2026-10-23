"""Issue #441：worker hermes 探针 session/new 先行回归测试。

背景：_probe_hermes_acp 旧实现 sessionId 写死空字符串直接发 session/prompt，
hermes ACP 服务端对空/不存在 sessionId 的 prompt 返回 stopReason=refusal →
#423 把 refusal 独立标记后监控全量 refused。修复：prompt 前先 session/new
获取真实 sessionId（对齐 _probe_hwcloud_acp 三步先例）。

用 fake ACP CLI（stdin/stdout JSON-RPC 可执行脚本）端到端驱动探活，并把探针
发出的每个请求按序写入 sidecar 文件，断言请求序列与 prompt 携带真实 sessionId
（非空字符串）。覆盖：
- happy path：initialize → session/new(真实 sessionId) → prompt → ok
- session/new 无响应 / JSON-RPC error / 响应缺 sessionId → fail（不把空 sessionId 带进 prompt）
- stopReason=refusal → refused（#423 口径不软化）
- session/new 前大量通知 → 仍 ok（deadline 驱动不回归）
"""
import json
import os
import stat
import sys

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="fake CLI 依赖 posix shebang 可执行脚本")

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault("GITCODE_API_TOKEN", "test_token")

# fake ACP CLI：把收到的请求逐行记录到 REQUEST_LOG（method + id + params）
_FAKE_CLI_TEMPLATE = '''#!/usr/bin/env python3
import sys, json

REQUEST_LOG = {request_log!r}
NEW_NOTES = {new_notes}
PROMPT_NOTES = {prompt_notes}
STOP = {stop!r}
NEW_BEHAVIOR = {new_behavior!r}

def send(o):
    sys.stdout.write(json.dumps(o))
    sys.stdout.write("\\n")
    sys.stdout.flush()

def notes(k):
    for i in range(k):
        send({{"jsonrpc": "2.0", "method": "session/update",
              "params": {{"update": {{"sessionUpdate": "agent_status_chunk"}}}}}})

_log = open(REQUEST_LOG, "a")

for line in sys.stdin:
    try:
        msg = json.loads(line)
    except Exception:
        continue
    m = msg.get("method")
    _log.write(json.dumps({{"method": m, "id": msg.get("id"),
                            "params": msg.get("params")}}) + "\\n")
    _log.flush()
    if m == "initialize":
        send({{"jsonrpc": "2.0", "id": msg["id"], "result": {{"sessionId": "probe-s"}}}})
    elif m == "session/new":
        notes(NEW_NOTES)
        if NEW_BEHAVIOR == "ok":
            send({{"jsonrpc": "2.0", "id": msg["id"], "result": {{"sessionId": "s-1"}}}})
        elif NEW_BEHAVIOR == "missing_sid":
            send({{"jsonrpc": "2.0", "id": msg["id"], "result": {{"cwd": "/tmp"}}}})
        elif NEW_BEHAVIOR == "error":
            send({{"jsonrpc": "2.0", "id": msg["id"],
                   "error": {{"code": -32000, "message": "session create failed"}}}})
        # NEW_BEHAVIOR == "silent": 不回任何响应 → 探针 deadline 超时
    elif m == "session/prompt":
        notes(PROMPT_NOTES)
        send({{"jsonrpc": "2.0", "id": msg["id"], "result": {{"stopReason": STOP}}}})
_log.close()
'''


def _make_fake_cli(tmp_path, *, request_log, new_notes=0, prompt_notes=0,
                   stop="end_turn", new_behavior="ok"):
    path = tmp_path / "fake-acp-cli-441"
    path.write_text(
        _FAKE_CLI_TEMPLATE.format(
            request_log=str(request_log), new_notes=new_notes, prompt_notes=prompt_notes,
            stop=stop, new_behavior=new_behavior,
        )
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _read_requests(request_log):
    with open(request_log) as f:
        return [json.loads(line) for line in f if line.strip()]


class TestHermesProbeSessionNew:
    """Issue #441：_probe_hermes_acp prompt 前先 session/new 拿真实 sessionId。"""

    def test_ok_and_prompt_uses_real_session_id(self, tmp_path):
        """T1 happy path：请求序列 initialize → session/new → session/prompt，
        且 prompt 的 sessionId == session/new 返回的真实值（非空字符串）。"""
        from src.runtime.agent import _probe_hermes_acp
        log = tmp_path / "requests.jsonl"
        cli = _make_fake_cli(tmp_path, request_log=log, stop="end_turn")
        result = _probe_hermes_acp(cli)
        assert result["status"] == "ok", result
        assert "stopReason=end_turn" in result["output"]
        reqs = _read_requests(log)
        methods = [r["method"] for r in reqs]
        assert methods == ["initialize", "session/new", "session/prompt"], methods
        prompt_req = reqs[-1]
        assert prompt_req["params"]["sessionId"] == "s-1"  # session/new 返回的真实 sessionId
        assert prompt_req["params"]["prompt"] == [{"type": "text", "text": "hi"}]

    def test_session_new_silent_reports_fail(self, tmp_path, monkeypatch):
        """T2 session/new 无响应（引擎异常）→ fail，错误前缀 ACP session/new。"""
        from src.runtime import agent as _agent_mod
        monkeypatch.setattr(_agent_mod, "_ACP_PROBE_TIMEOUT", 1)
        log = tmp_path / "requests.jsonl"
        cli = _make_fake_cli(tmp_path, request_log=log, new_behavior="silent")
        result = _agent_mod._probe_hermes_acp(cli)
        assert result["status"] == "fail"
        assert "session/new" in result["error"]
        # prompt 不应被发出（session/new 未成功）
        methods = [r["method"] for r in _read_requests(log)]
        assert "session/prompt" not in methods

    def test_session_new_error_reports_fail(self, tmp_path):
        """T3 session/new 返回 JSON-RPC error → fail（引擎不可用正确归类）。"""
        from src.runtime.agent import _probe_hermes_acp
        log = tmp_path / "requests.jsonl"
        cli = _make_fake_cli(tmp_path, request_log=log, new_behavior="error")
        result = _probe_hermes_acp(cli)
        assert result["status"] == "fail"
        assert "session/new" in result["error"]
        assert "session create failed" in result["error"]

    def test_session_new_missing_session_id_reports_fail(self, tmp_path):
        """T4 session/new 成功但响应缺 sessionId → fail（不把空 sessionId 带进 prompt）。"""
        from src.runtime.agent import _probe_hermes_acp
        log = tmp_path / "requests.jsonl"
        cli = _make_fake_cli(tmp_path, request_log=log, new_behavior="missing_sid")
        result = _probe_hermes_acp(cli)
        assert result["status"] == "fail"
        assert "missing sessionId" in result["error"]
        methods = [r["method"] for r in _read_requests(log)]
        assert "session/prompt" not in methods

    def test_refusal_still_marked_refused(self, tmp_path):
        """T5 stopReason=refusal → refused（#423 口径保持，不软化）。"""
        from src.runtime.agent import _probe_hermes_acp
        log = tmp_path / "requests.jsonl"
        cli = _make_fake_cli(tmp_path, request_log=log, stop="refusal")
        result = _probe_hermes_acp(cli)
        assert result["status"] == "refused"
        assert result["error"] == ""
        # 真实 sessionId 路径下出现的 refusal 才是真「模型拒绝」而非协议误伤
        reqs = _read_requests(log)
        assert reqs[-1]["params"]["sessionId"] == "s-1"

    def test_new_notes_stream_still_ok(self, tmp_path):
        """T6 session/new 前后 34 行通知 → 仍 ok（#423 deadline 驱动不回归）。"""
        from src.runtime.agent import _probe_hermes_acp
        log = tmp_path / "requests.jsonl"
        cli = _make_fake_cli(tmp_path, request_log=log, new_notes=34, prompt_notes=5,
                             stop="end_turn")
        result = _probe_hermes_acp(cli)
        assert result["status"] == "ok"
