"""Issue #976：hermes ACP tool_call 入参（rawInput / content 块）保真 + 占位事件 + 裁剪契约。

根因：hermes acp_adapter 对 write_file/patch 等 polished tools 的 tool_call 不携带
rawInput（hermes 0.9-0.20 实测 rawInput=None），grape 侧 hermes_acp.py 只读
rawInput/input/parameters → 任务日志 write/patch 入参恒为空。

本文件验证：
1. 57KB content 序列化保真（rawInput 路径 + content diff 块路径）
2. name/title 皆空时产出占位 tool_use 事件（不静默丢弃）
3. 端到端：tool_call 事件 → report_messages 落库 → GET /api/task-logs/{id}/messages
   取回完整入参
4. 超限裁剪带 truncated/original_bytes/sha256_prefix 元数据
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.runtime.registry  # noqa: E402,F401 先触发（绕开 src.agent.__init__ 循环导入）
from src.agent.hermes_acp import (  # noqa: E402
    _ACPSimpleClient,
    _clip_tool_input,
    _extract_tool_call_input,
    _input_from_content_blocks,
)
from src.session_manager import db, db_lock  # noqa: E402
from src.runtime import service  # noqa: E402


BIG_CONTENT = ("line of file content for fidelity check\n" * 2100)[:57 * 1024]
assert len(BIG_CONTENT) == 57 * 1024


class _FakeStdin:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass


class _FakeStdout:
    pass


def _make_client():
    messages = []
    client = _ACPSimpleClient(stdin=_FakeStdin(), stdout=_FakeStdout(), label="[test] ",
                              on_message=lambda t, c: messages.append((t, c)))
    return client, messages


def _tool_call_line(update: dict) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": "session/update",
                       "params": {"sessionId": "s1", "update": update}})


# ══════════════════════════════════════════════
# 1. 入参提取保真（rawInput / input / content 块）
# ══════════════════════════════════════════════

class TestInputExtraction:
    def test_rawinput_57kb_write_fidelity(self):
        u = {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "write_file",
             "rawInput": {"path": "docs/a.md", "content": BIG_CONTENT}}
        out = _extract_tool_call_input(u)
        assert out == {"path": "docs/a.md", "content": BIG_CONTENT}
        assert len(out["content"]) == 57 * 1024

    def test_rawinput_patch_old_new(self):
        u = {"rawInput": {"path": "a.py", "old": "foo", "new": "bar"}}
        assert _extract_tool_call_input(u) == {"path": "a.py", "old": "foo", "new": "bar"}

    def test_input_and_parameters_fallback(self):
        assert _extract_tool_call_input({"input": {"path": "x"}}) == {"path": "x"}
        assert _extract_tool_call_input({"parameters": {"command": "ls"}}) == {"command": "ls"}
        assert _extract_tool_call_input({"input": json.dumps({"path": "x"})}) == {"path": "x"}

    def test_content_diff_block_write_fidelity_57kb(self):
        """hermes 0.9-0.20 polished tools 无 rawInput，content diff 块是唯一载体。"""
        u = {"content": [{"type": "diff", "path": "docs/a.md",
                          "oldText": None, "newText": BIG_CONTENT}]}
        out = _extract_tool_call_input(u)
        assert out == {"path": "docs/a.md", "content": BIG_CONTENT}

    def test_content_diff_block_patch(self):
        u = {"content": [{"type": "diff", "path": "a.py", "oldText": "foo", "newText": "bar"}]}
        assert _extract_tool_call_input(u) == {"path": "a.py", "old": "foo", "new": "bar"}

    def test_content_text_block_fallback(self):
        u = {"content": [{"type": "text", "text": "$ ls -la"}]}
        assert _extract_tool_call_input(u) == {"text": "$ ls -la"}

    def test_empty_yields_empty_dict(self):
        assert _extract_tool_call_input({}) == {}
        assert _input_from_content_blocks(None) == {}
        assert _input_from_content_blocks([]) == {}
        assert _input_from_content_blocks([{"type": "terminal", "terminalId": "x"}]) == {}

    def test_rawinput_preferred_over_content(self):
        u = {"rawInput": {"path": "real.md", "content": "raw"},
             "content": [{"type": "text", "text": "decorated preview"}]}
        assert _extract_tool_call_input(u) == {"path": "real.md", "content": "raw"}

    def test_serialized_wire_roundtrip_57kb(self):
        """完整 JSON-RPC 行（wire 格式）→ handle_line → tool_use 事件保真。"""
        client, messages = _make_client()
        client.handle_line(_tool_call_line({
            "sessionUpdate": "tool_call", "toolCallId": "t1", "title": "write_file",
            "rawInput": {"path": "docs/a.md", "content": BIG_CONTENT},
        }))
        assert len(messages) == 1
        etype, data = messages[0]
        assert etype == "tool_use"
        assert data["tool"] == "write_file"
        assert data["input"]["content"] == BIG_CONTENT
        assert len(data["input"]["content"]) == 57 * 1024
        assert data["tool_call_id"] == "t1"

    def test_hermes_engine_build_tool_start_roundtrip(self):
        """若本机装有 hermes 引擎，用真实 build_tool_start 验证 57KB 经
        content diff 块序列化后仍可被 grape 完整还原（实测 hermes 0.20.0：
        write_file 的 rawInput=None，content diff 块为唯一入参载体）。"""
        if "acp_adapter" not in sys.modules:
            for _p in ("/usr/local/lib/hermes-agent",
                       "/usr/local/lib/hermes-agent/venv/lib/python3.11/site-packages"):
                if os.path.isdir(_p) and _p not in sys.path:
                    sys.path.append(_p)
        if "acp_adapter" not in sys.modules:
            try:
                import acp_adapter  # noqa: F401
            except ImportError:
                pytest.skip("hermes engine acp_adapter unavailable")
        try:
            from acp_adapter.tools import build_tool_start
            from acp_adapter.edit_approval import build_edit_proposal
        except ImportError:
            pytest.skip("hermes acp_adapter tools unavailable")
        proposal = build_edit_proposal("write_file", {"path": "docs/a.md", "content": BIG_CONTENT})
        start = build_tool_start("tc-1", "write_file",
                                 {"path": "docs/a.md", "content": BIG_CONTENT},
                                 edit_diff=proposal)
        u = json.loads(start.model_dump_json(by_alias=True))
        assert u.get("rawInput") is None  # hermes polished tools 不携带 rawInput（本 Issue 根因）
        out = _extract_tool_call_input(u)
        assert out.get("path") == "docs/a.md"
        assert out.get("content") == BIG_CONTENT


# ══════════════════════════════════════════════
# 2. 占位事件（name/title 皆空不丢弃）
# ══════════════════════════════════════════════

class TestPlaceholderEvent:
    def test_no_name_no_title_yields_placeholder(self):
        client, messages = _make_client()
        client.handle_line(_tool_call_line({
            "sessionUpdate": "tool_call", "toolCallId": "t9",
            "rawInput": {"path": "a.md", "content": "x"},
        }))
        assert len(messages) == 1
        etype, data = messages[0]
        assert etype == "tool_use"
        assert data["tool"] == "unknown"
        assert data["input"] == {"path": "a.md", "content": "x"}
        assert data["title"] == ""
        assert data["tool_call_id"] == "t9"

    def test_checkmark_title_yields_placeholder_with_title(self):
        """_tool_name_from_title("✅ ...") 返回空 → 占位事件，保留 title。"""
        client, messages = _make_client()
        client.handle_line(_tool_call_line({
            "sessionUpdate": "tool_call", "title": "✅ Tool 3",
            "content": [{"type": "text", "text": "done"}],
        }))
        assert len(messages) == 1
        etype, data = messages[0]
        assert etype == "tool_use"
        assert data["tool"] == "unknown"
        assert data["title"] == "✅ Tool 3"
        assert data["input"] == {"text": "done"}

    def test_normal_title_still_resolves_tool_name(self):
        client, messages = _make_client()
        client.handle_line(_tool_call_line({
            "sessionUpdate": "tool_call", "title": "write_file",
            "rawInput": {"path": "a.md", "content": "x"},
        }))
        assert messages[0][1]["tool"] == "write_file"


# ══════════════════════════════════════════════
# 3. 裁剪契约（truncated / original_bytes / sha256_prefix）
# ══════════════════════════════════════════════

class TestClipContract:
    def test_under_limit_untouched(self):
        inp = {"path": "a", "content": "x" * 1024}
        assert _clip_tool_input(inp) is inp

    def test_over_limit_carries_metadata(self):
        import hashlib
        big = {"path": "a.bin", "content": "y" * (300 * 1024)}
        out = _clip_tool_input(big)
        payload = json.dumps(big, ensure_ascii=False, default=str).encode("utf-8")
        assert out["truncated"] is True
        assert out["original_bytes"] == len(payload)
        assert out["sha256_prefix"] == hashlib.sha256(payload).hexdigest()[:16]
        assert out["preview"]

    def test_env_overridable_limit(self, monkeypatch):
        monkeypatch.setenv("HERMES_TOOL_INPUT_MAX_BYTES", "64")
        import importlib
        import src.agent.hermes_acp as mod
        importlib.reload(mod)
        try:
            out = mod._clip_tool_input({"path": "a", "content": "z" * 1024})
            assert out["truncated"] is True and out["original_bytes"] > 64
        finally:
            monkeypatch.delenv("HERMES_TOOL_INPUT_MAX_BYTES")
            importlib.reload(mod)


# ══════════════════════════════════════════════
# 4. 端到端：tool_call → task_messages → /api/task-logs/{id}/messages
# ══════════════════════════════════════════════

TASK_ID = "t_test_976"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM task_messages WHERE task_id=?", (TASK_ID,))
        c.execute("DELETE FROM agent_tasks WHERE id=?", (TASK_ID,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM task_messages WHERE task_id=?", (TASK_ID,))
        c.execute("DELETE FROM agent_tasks WHERE id=?", (TASK_ID,))
        db.commit()


def _make_task():
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR IGNORE INTO agent_tasks (id, agent_session_id, runtime_id, prompt, status) "
                  "VALUES (?, 's1', 1, 'p', 'running')", (TASK_ID,))
        db.commit()


class TestEndToEnd:
    def test_write_tool_call_reaches_task_log_messages(self):
        _make_task()
        client, messages = _make_client()
        # 1) hermes ACP 事件流：write_file tool_call（57KB content，rawInput 路径）
        client.handle_line(_tool_call_line({
            "sessionUpdate": "tool_call", "toolCallId": "t1", "title": "write_file",
            "rawInput": {"path": "docs/a.md", "content": BIG_CONTENT},
        }))
        # 2) hermes ACP 事件流：write_file tool_call（57KB content，content diff 块路径）
        client.handle_line(_tool_call_line({
            "sessionUpdate": "tool_call", "toolCallId": "t2", "title": "write_file",
            "content": [{"type": "diff", "path": "docs/b.md",
                         "oldText": None, "newText": BIG_CONTENT}],
        }))
        assert len(messages) == 2
        # 3) worker 上报 → task_messages 落库（同 agent.py 批量上报路径）
        service.report_messages(TASK_ID, [
            {"type": "tool_use", "data": {"tool": t, "input": i, "tool_call_id": tc}}
            for (_, (t, i, tc)) in [(m[0], (m[1]["tool"], m[1]["input"], m[1].get("tool_call_id", "")))
                                     for m in messages]
        ])
        # 4) API 取回完整入参
        from fastapi.testclient import TestClient
        api = TestClient(__import__("src.api.main", fromlist=["app"]).app)
        from src.api.jwt_utils import create_access_token
        from src.auth.grape_account_db import get_token_version
        import uuid
        from src.auth import grape_account_db
        u = grape_account_db.create_grape_account(f"hermes976_{uuid.uuid4().hex[:8]}",
                                                  email=None, password="pw123456")
        token = create_access_token(u["id"], u["username"], get_token_version(u["id"]))
        r = api.get(f"/api/task-logs/{TASK_ID}/messages",
                    headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        msgs = r.json()
        tool_uses = [m for m in msgs if m.get("type") == "tool_use"]
        assert len(tool_uses) == 2
        for m in tool_uses:
            assert m["tool"] == "write_file"
            assert m["input"]["path"] in ("docs/a.md", "docs/b.md")
            assert m["input"]["content"] == BIG_CONTENT
            assert len(m["input"]["content"]) == 57 * 1024

    def test_service_list_returns_full_input(self):
        _make_task()
        service.report_messages(TASK_ID, [
            {"type": "tool_use", "data": {"tool": "patch", "input": {
                "path": "a.py", "old": "foo", "new": "bar"}, "tool_call_id": "t3"}},
        ])
        msgs = service.list_task_messages(TASK_ID)
        assert msgs[0]["type"] == "tool_use"
        assert msgs[0]["tool"] == "patch"
        assert msgs[0]["input"] == {"path": "a.py", "old": "foo", "new": "bar"}
