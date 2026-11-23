"""Issue #2546：build_general_chat_prompt 收敛为单一调用点——结构看护 + 五路径透传回归锁。

结构 seam（本切片可红看护）：全仓 src/ 直接调用 build_general_chat_prompt 的点
必须恰好 1 处，且位于 src/domains/runtime/routes/chat.py 的 _build_chat_prompt_for
内。收敛后 prompt 新增参数（runtime_id / installing_plugins 等）只改 helper 一处
即可覆盖全部 chat 端点，杜绝「改一处漏三处」（2026-11-14 chat 插件能力清单误报
的根因形态，见 Issue #2546）。

行为 seam（公开 HTTP 接口）：/api/chat/send（direct）、/send/stream、/submit、
/send/async、/send 队列模式（_enqueue_chat）五条路径经 TestClient 驱动，mock
prompt_builder 捕获组参，断言 message / skill / backend_name / user_info /
has_valid_session / task 分支 runtime_id 透传与收敛前一致；task 会话仍走
build_issue_chat_prompt。
"""

import ast
import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user, get_current_user_or_api_key
from src.kernel.session_manager import db, db_lock

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
CHAT_PY = "src/domains/runtime/routes/chat.py"

UID = 99025
USERNAME = "chat_prompt_lock"
REPO_KEY = "gitcode:org/lock"


def _direct_calls_of(name: str):
    """全仓 src/ 内对 name 的直接调用点（ast.Call；import/别名赋值不算）。"""
    hits = []
    for py in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Name) and func.id == name) or (
                isinstance(func, ast.Attribute) and func.attr == name
            ):
                hits.append((py, node))
    return hits


def _enclosing_toplevel_func(py: Path, node: ast.Call):
    """定位 ast.Call 所在的顶层函数名（按行号区间包含判定；不在任何顶层函数内则返回 None）。

    顶层函数互不重叠；模块级/类内调用不落在任何顶层函数区间 → None（断言判红）。
    """
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    for top in tree.body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = top.end_lineno or top.lineno
            if top.lineno <= node.lineno <= end:
                return top.name
    return None


class TestPromptSingleEntry:
    """Spec 验收 ①：全仓 build_general_chat_prompt 直接调用点仅 1 处（chat.py helper 内）。"""

    def test_build_general_chat_prompt_single_direct_call_site(self):
        calls = _direct_calls_of("build_general_chat_prompt")
        assert len(calls) == 1, (
            "全仓 src/ 中 build_general_chat_prompt 直接调用点必须收敛为 1 处，"
            f"实际 {len(calls)} 处：{[(str(p), n.lineno) for p, n in calls]}"
        )
        py, node = calls[0]
        assert str(py).endswith(CHAT_PY), f"唯一直接调用点应位于 {CHAT_PY}，实际 {py}"
        enclosing = _enclosing_toplevel_func(py, node)
        assert enclosing == "_build_chat_prompt_for", (
            f"唯一直接调用点应位于 _build_chat_prompt_for 内，实际所在函数：{enclosing}"
        )

    def test_helper_exists_in_chat_routes(self):
        import src.domains.runtime.routes.chat as chat_routes

        assert hasattr(chat_routes, "_build_chat_prompt_for"), (
            "chat.py 必须提供 _build_chat_prompt_for 统一构建入口（Issue #2546 Spec）"
        )


@pytest.fixture()
def client():
    """TestClient seam：沿用 test_chat_routes 的依赖覆盖 + 隔离清理。"""

    def _fake_user():
        return {"id": UID, "username": USERNAME}

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()
    with db_lock:
        c2 = db.cursor()
        c2.execute("DELETE FROM chat_conversations WHERE user_id=?", (UID,))
        c2.execute("DELETE FROM chat_agent_session WHERE user_id=?", (UID,))
        c2.execute("DELETE FROM agent_runtimes WHERE name LIKE 'chat_rt%'")
        c2.execute("DELETE FROM agent_runtimes WHERE display_name='chat_test_local'")
        c2.execute("DELETE FROM runtime_credentials WHERE name LIKE 'chat_cred%'")
        c2.execute("DELETE FROM unified_user_mapping WHERE user_id=?", (UID,))
        db.commit()


def _local_engine_id():
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT id FROM agent_runtimes WHERE credential_id=0 AND lower(name)='opencode' AND healthy=1").fetchone()
        if row:
            return row[0]
        c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, installed, healthy, status, scope, owner_id) "
                  "VALUES ('opencode','chat_test_local',0,1,1,'online','global','')")
        db.commit()
        return c.lastrowid


def _remote_engine_id():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'chat_cred%'")
        c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
                  "VALUES ('h%s', 'chat_cred', 'chat-dev', 'global', 'tester', 'active')" % uuid.uuid4().hex)
        cid = c.lastrowid
        c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
                  "VALUES ('chat_rt', 'chat_rt', ?, 'online', datetime('now'), 'global', '', 1, 1)", (cid,))
        db.commit()
        return c.lastrowid


def _fake_stream_agent(prompt, cwd, session_id="", **kw):
    yield ("step_start", "started")
    yield ("text", "联通成功")
    yield ("done", {"exit_code": 0, "text": "联通成功", "oc_session_id": "ses_x"})


def _new_conv(c, repo_key=REPO_KEY):
    r = c.post("/api/chat/conversations", json={"repo_key": repo_key})
    assert r.status_code == 200, r.text
    return r.json()["conversation_id"]


def _task_conv(cid):
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE chat_conversations SET conv_type='task', issue_number=5 WHERE id=?", (cid,))
        db.commit()


def _sse_done_text(body):
    for line in body.splitlines():
        if line.startswith("data: "):
            evt = json.loads(line[len("data: "):])
            if evt.get("type") == "done":
                return evt.get("text", "")
    return ""


class TestPromptPassthroughAcrossPaths:
    """Spec 验收 ②⑤：五条路径统一走 helper，透传 backend_name / user_info / has_valid_session，
    task 会话仍走 build_issue_chat_prompt（runtime_id 语义分 cfg 默认 / enqueue 解析值两源）。"""

    CFG = {"backend": "opencode", "skill": "codereview", "skill_id": 77}

    def test_send_direct_general_prompt_passthrough(self, client):
        rid = _local_engine_id()
        cid = _new_conv(client)
        cfg = {**self.CFG, "runtime_id": rid}
        with patch("src.domains.runtime.routes.chat._get_chat_config", return_value=cfg), \
             patch("src.domains.runtime.engine.executor.stream_chat_agent", side_effect=_fake_stream_agent), \
             patch("src.domains.runtime.routes.chat.build_general_chat_prompt", return_value="P") as m_build:
            r = client.post("/api/chat/send", json={"message": "hi", "conversation_id": cid})
        assert r.status_code == 200 and r.json()["reply"] == "联通成功"
        m_build.assert_called_once()
        args, kwargs = m_build.call_args
        assert args[0] == "hi"
        assert args[1] == REPO_KEY
        assert isinstance(args[2], list)
        assert args[3] == "codereview" and args[4] == 77
        assert kwargs["backend_name"] == "opencode"
        assert USERNAME in kwargs["user_info"]
        assert kwargs["has_valid_session"] is False

    def test_send_stream_general_prompt_passthrough(self, client):
        rid = _local_engine_id()
        cid = _new_conv(client)
        cfg = {**self.CFG, "runtime_id": rid}
        with patch("src.domains.runtime.routes.chat._get_chat_config", return_value=cfg), \
             patch("src.domains.runtime.engine.executor.stream_chat_agent", side_effect=_fake_stream_agent), \
             patch("src.domains.runtime.routes.chat.build_general_chat_prompt", return_value="P") as m_build:
            r = client.post("/api/chat/send/stream", json={"message": "hi", "conversation_id": cid})
        assert r.status_code == 200
        assert "联通成功" in _sse_done_text(r.text)
        m_build.assert_called_once()
        args, kwargs = m_build.call_args
        assert args[0] == "hi" and args[3] == "codereview" and args[4] == 77
        assert kwargs["backend_name"] == "opencode"
        assert USERNAME in kwargs["user_info"]
        assert kwargs["has_valid_session"] is False

    def test_submit_general_prompt_passthrough(self, client):
        rid = _local_engine_id()
        cid = _new_conv(client)
        cfg = {**self.CFG, "runtime_id": rid}
        with patch("src.domains.runtime.routes.chat._get_chat_config", return_value=cfg), \
             patch("src.api.chat_task_manager.ChatTaskManager.submit") as m_ctm, \
             patch("src.domains.runtime.routes.chat.build_general_chat_prompt", return_value="P") as m_build:
            r = client.post("/api/chat/submit", json={"message": "hi", "conversation_id": cid})
        assert r.status_code == 200 and r.json()["status"] == "running"
        m_ctm.assert_called_once()
        m_build.assert_called_once()
        args, kwargs = m_build.call_args
        assert args[0] == "hi" and args[3] == "codereview" and args[4] == 77
        assert kwargs["backend_name"] == "opencode"
        assert USERNAME in kwargs["user_info"]
        assert kwargs["has_valid_session"] is False

    def test_send_async_general_prompt_passthrough(self, client):
        rid = _local_engine_id()
        cid = _new_conv(client)
        cfg = {**self.CFG, "runtime_id": rid}
        with patch("src.domains.runtime.routes.chat._get_chat_config", return_value=cfg), \
             patch("src.domains.runtime.routes.chat.build_general_chat_prompt", return_value="P") as m_build:
            r = client.post("/api/chat/send/async", json={"message": "hi", "conversation_id": cid})
        assert r.status_code == 200 and r.json()["status"] == "fallback_stream"
        m_build.assert_called_once()
        args, kwargs = m_build.call_args
        assert args[0] == "hi" and args[3] == "codereview" and args[4] == 77
        assert kwargs["backend_name"] == "opencode"
        assert USERNAME in kwargs["user_info"]
        assert kwargs["has_valid_session"] is False

    def test_queue_mode_general_prompt_passthrough(self, client):
        rid = _remote_engine_id()
        cid = _new_conv(client)
        cfg = {**self.CFG, "runtime_id": rid}
        with patch("src.domains.runtime.routes.chat._get_chat_config", return_value=cfg), \
             patch("src.domains.runtime.engine.service.enqueue_task", return_value="t1") as m_enq, \
             patch("src.domains.runtime.routes.chat.build_general_chat_prompt", return_value="P") as m_build:
            r = client.post("/api/chat/send", json={"message": "hi", "conversation_id": cid})
        assert r.status_code == 200 and r.json()["queued"] is True
        m_enq.assert_called_once()
        assert m_enq.call_args.kwargs["prompt"] == "P"
        # 队列路径为既有双构建形态：send_message 先构建一次（被丢弃，backend_name=
        # opencode），_enqueue_chat 再构建一次（真正入队，不注入引擎名 → 空串）
        assert m_build.call_count == 2
        first_args, first_kwargs = m_build.call_args_list[0]
        assert first_args[0] == "hi" and first_args[3] == "codereview" and first_args[4] == 77
        assert first_kwargs["backend_name"] == "opencode"
        args, kwargs = m_build.call_args_list[-1]
        assert args[0] == "hi" and args[3] == "codereview" and args[4] == 77
        # _enqueue_chat 原路径不注入引擎名 → 有效值为空串（收敛前后一致）
        assert kwargs.get("backend_name", "") == ""
        assert USERNAME in kwargs["user_info"]
        assert kwargs["has_valid_session"] is False

    def test_send_valid_session_id_propagates_has_valid_session(self, client):
        rid = _local_engine_id()
        cid = _new_conv(client)
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE chat_conversations SET runtime_session_id='ses_live_1' WHERE id=?", (cid,))
            db.commit()
        cfg = {**self.CFG, "runtime_id": rid}
        with patch("src.domains.runtime.routes.chat._get_chat_config", return_value=cfg), \
             patch("src.domains.runtime.engine.executor.stream_chat_agent", side_effect=_fake_stream_agent), \
             patch("src.domains.runtime.routes.chat.build_general_chat_prompt", return_value="P") as m_build:
            r = client.post("/api/chat/send", json={"message": "hi", "conversation_id": cid})
        assert r.status_code == 200
        assert m_build.call_args.kwargs["has_valid_session"] is True

    def test_task_conv_send_routes_to_issue_prompt_with_cfg_runtime_id(self, client):
        rid = _local_engine_id()
        cid = _new_conv(client)
        _task_conv(cid)
        cfg = {**self.CFG, "runtime_id": rid}
        with patch("src.domains.runtime.routes.chat._get_chat_config", return_value=cfg), \
             patch("src.domains.runtime.engine.executor.stream_chat_agent", side_effect=_fake_stream_agent), \
             patch("src.kernel.prompt_builder.build_issue_chat_prompt", return_value="task-prompt") as m_issue, \
             patch("src.domains.runtime.routes.chat.build_general_chat_prompt", return_value="P") as m_build:
            r = client.post("/api/chat/send", json={"message": "hi", "conversation_id": cid})
        assert r.status_code == 200
        m_issue.assert_called_once()
        m_build.assert_not_called()
        args, kwargs = m_issue.call_args
        assert args[0] == "hi"
        assert kwargs["runtime_id"] == rid
        assert kwargs["account"] == USERNAME
        assert kwargs["has_valid_session"] is False

    def test_task_conv_queue_routes_to_issue_prompt_with_resolved_runtime_id(self, client):
        rid = _remote_engine_id()
        # task 会话 + 空 repo_key：绕开 send_message 的 task-token 闸（无 token 早退），
        # 直达 _enqueue_chat 的 task 分支
        cid = _new_conv(client, repo_key="")
        _task_conv(cid)
        cfg = {**self.CFG, "runtime_id": rid}
        with patch("src.domains.runtime.routes.chat._get_chat_config", return_value=cfg), \
             patch("src.domains.runtime.engine.service.enqueue_task", return_value="t1") as m_enq, \
             patch("src.kernel.prompt_builder.build_issue_chat_prompt", return_value="task-prompt") as m_issue, \
             patch("src.domains.runtime.routes.chat.build_general_chat_prompt", return_value="P") as m_build:
            r = client.post("/api/chat/send", json={"message": "hi", "conversation_id": cid})
        assert r.status_code == 200 and r.json()["queued"] is True
        m_enq.assert_called_once()
        assert m_enq.call_args.kwargs["prompt"] == "task-prompt"
        m_build.assert_not_called()
        # 队列路径既有双构建形态：send_message 的 task 构建（runtime_id=cfg 指针）
        # + _enqueue_chat 的 task 构建（runtime_id=解析值）——两源均须命中 cfg 引擎
        assert m_issue.call_count == 2
        first_kwargs = m_issue.call_args_list[0].kwargs
        last_kwargs = m_issue.call_args_list[-1].kwargs
        assert first_kwargs["runtime_id"] == rid
        assert first_kwargs["account"] == USERNAME
        # _enqueue_chat 的 task 分支注入解析后的 runtime_id（cfg 指针命中的远端引擎）
        assert last_kwargs["runtime_id"] == rid
        assert last_kwargs["account"] == USERNAME
