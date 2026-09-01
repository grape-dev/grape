"""Event 对话测试（event-conversation-design.md §5）

覆盖：门禁（thread/chat）、对话写线程、共享知识（文件+索引+注入）、流程回退（取消任务+阶段）、
bot token 鉴权、会话复用。
"""
import asyncio, os, sys
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock
from src.user import db as user_db
from src.runtime import service
# JWT access token helper（替代旧的 create_session_token）
def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.user.db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))

client = TestClient(app)
_uid = None
_headers = {}
_BOT_TOKEN = "bot_conv_token"
_REPO_KEY = "gitcode:org/test"
_issue_seq = None


def setup_module():
    global _uid, _headers
    from src.api.db_setup import init_database
    init_database()
    _uid = user_db.create_user("conv_user", "pwd")
    if not _uid:
        _uid = user_db.get_user_by_username("conv_user")
    _headers = {"Authorization": "Bearer " + _jwt_access_token(_uid["id"], _uid["username"])}
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) VALUES (?,?,?,?,?,?)",
                  (_REPO_KEY, "org", "test", "org/test", 1, _BOT_TOKEN))
        db.commit()


def teardown_module():
    _clean()
    if _uid:
        user_db.delete_user(_uid["id"])


def _clean():
    global _issue_seq
    _issue_seq = None
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM event_thread")
        c.execute("DELETE FROM agent_shared_knowledge")
        c.execute("DELETE FROM events WHERE repo_key=?", (_REPO_KEY,))
        c.execute("DELETE FROM issue_state WHERE repo_key=?", (_REPO_KEY,))
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (_REPO_KEY,))
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (_REPO_KEY,))
        c.execute("DELETE FROM issue_agent_session WHERE repo_key=?", (_REPO_KEY,))
        db.commit()


def _seed_event(agent_type="vod", issue_id="#5", status="completed"):
    global _issue_seq
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, issue_id, payload, status, result) VALUES (?,?,?,?,?,?,?,?)",
                  (_REPO_KEY, "issue_opened", agent_type, "test event", issue_id, "{}", status, "已分析"))
        _issue_seq = c.lastrowid
        db.commit()
    return _issue_seq


def _seed_issue_state(stage="coding"):
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO issue_state (repo_key, issue_id, stage, owner_agent) VALUES (?,?,?,?)",
                  (_REPO_KEY, "#5", stage, "vod"))
        db.commit()


def _bot_headers():
    return {"Authorization": f"Bearer {_BOT_TOKEN}"}


def _fake_stream(prompt="", cwd="", session_id="", event_type="", backend_name="", account="",
                 user_id=0, env_extra=None, bot_token="", user_token="", agent_name="", agent_label=""):
    """伪 agent 执行：yield 一个 done 事件，避免依赖真实 opencode。"""
    async def _gen():
        yield "done", {"text": "模拟 agent 回复", "exit_code": 0, "oc_session_id": "ses_conv_1"}
    return _gen()


class TestThreadGate:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_gate_open_when_pipeline_running(self):
        seq = _seed_event()
        _seed_issue_state("coding")
        r = client.get(f"/api/events/{seq}/thread", headers=_headers)
        assert r.status_code == 200
        assert r.json()["gate"]["open"] is True

    def test_gate_closed_when_done(self):
        seq = _seed_event()
        _seed_issue_state("done")
        r = client.get(f"/api/events/{seq}/thread", headers=_headers)
        assert r.json()["gate"]["open"] is False

    def test_chat_rejected_when_done(self):
        seq = _seed_event()
        _seed_issue_state("done")
        r = client.post(f"/api/events/{seq}/chat", json={"message": "hi"}, headers=_headers)
        assert r.status_code == 409

    def test_thread_404(self):
        r = client.get("/api/events/999999/thread", headers=_headers)
        assert r.status_code == 404


class TestConversation:
    def setup_method(self):
        _clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "direct"

    def teardown_method(self):
        _clean()
        os.environ.pop("DEVOPS_EXECUTION_MODE", None)

    def test_chat_writes_thread_and_reuses_issue_session(self):
        seq = _seed_event()
        _seed_issue_state("coding")
        with patch("src.runtime.executor.consume_stream_async", side_effect=_fake_stream):
            r = client.post(f"/api/events/{seq}/chat", json={"message": "为什么这样处理？"}, headers=_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reply"] == "模拟 agent 回复"
        roles = [m["role"] for m in body["messages"]]
        assert "user" in roles and "agent" in roles
        assert any(m["content"] == "为什么这样处理？" for m in body["messages"] if m["role"] == "user")
        # 会话复用：issue_agent_session 已建且回写了新 oc_session_id
        with db_lock:
            row = db.cursor().execute(
                "SELECT runtime_session_id FROM issue_agent_session WHERE repo_key=? AND agent_type=? AND issue_id=?",
                (_REPO_KEY, "vod", "#5")).fetchone()
        assert row and row[0] == "ses_conv_1"

    def test_thread_get_returns_messages(self):
        seq = _seed_event()
        _seed_issue_state("coding")
        with patch("src.runtime.executor.consume_stream_async", side_effect=_fake_stream):
            client.post(f"/api/events/{seq}/chat", json={"message": "hi"}, headers=_headers)
        r = client.get(f"/api/events/{seq}/thread", headers=_headers)
        assert len(r.json()["messages"]) == 2

    def test_direct_uses_issue_agent_workdir(self):
        """Fix 1：直接执行 cwd = 该 issue 的 agent 目录（而非 event-conv）"""
        seq = _seed_event()
        _seed_issue_state("coding")
        captured = {}

        def _capture(prompt="", cwd="", **k):
            captured["cwd"] = cwd
            return _fake_stream(prompt=prompt, cwd=cwd, **k)

        with patch("src.runtime.executor.consume_stream_async", side_effect=_capture):
            r = client.post(f"/api/events/{seq}/chat", json={"message": "hi"}, headers=_headers)
        assert r.status_code == 200, r.text
        from src.api.event_pipeline import get_agent_workdir
        assert captured["cwd"] == get_agent_workdir(_REPO_KEY, "vod", "#5")

    def test_direct_stuck_resets_session_and_retries(self):
        """Fix 2：复用会话挂起（无输出超时）→ 重置 issue 会话 → 全新会话重试一次"""
        seq = _seed_event()
        _seed_issue_state("coding")
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO issue_agent_session (id, repo_key, agent_type, issue_id, runtime_session_id, status) "
                      "VALUES ('iss-1',?,?,?,?,'idle')", (_REPO_KEY, "vod", "#5", "ses_bad"))
            db.commit()
        calls = {"n": 0}

        def _stuck_then_ok(prompt="", cwd="", session_id="", **k):
            calls["n"] += 1
            if calls["n"] == 1:
                async def _stuck():
                    await asyncio.sleep(10)  # 超过超时(0.2s) → 挂起判定
                    yield "done", {}
                return _stuck()
            return _fake_stream(prompt=prompt, cwd=cwd, **k)

        with patch("src.runtime.executor.consume_stream_async", side_effect=_stuck_then_ok), \
             patch("src.api.routes.events._CONV_NO_OUTPUT_TIMEOUT", 0.2):
            r = client.post(f"/api/events/{seq}/chat", json={"message": "hi"}, headers=_headers)
        assert r.status_code == 200, r.text
        assert r.json()["reply"] == "模拟 agent 回复"
        assert calls["n"] == 2
        with db_lock:
            row = db.cursor().execute("SELECT runtime_session_id FROM issue_agent_session WHERE id='iss-1'").fetchone()
        assert row and row[0] == "ses_conv_1"


class TestKnowledge:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_knowledge_write_issue_scope(self):
        seq = _seed_event(issue_id="#5")
        r = client.post(f"/api/events/{seq}/knowledge", json={"scope": "issue", "content": "该 issue 验收标准 = 接口可跑通"}, headers=_bot_headers())
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        with db_lock:
            row = db.cursor().execute("SELECT scope, issue_num, status, file_path FROM agent_shared_knowledge WHERE source_seq=?", (seq,)).fetchone()
        assert row and row[0] == "issue" and row[1] == 5 and row[2] == "accepted"
        import os as _os
        from src.config import WORKSPACE_DIR
        assert _os.path.isfile(_os.path.join(WORKSPACE_DIR, row[3]))

    def test_knowledge_scope_file_locations(self):
        seq = _seed_event(issue_id="#5")
        from src.api.knowledge import knowledge_path
        for scope, agent in (("project", ""), ("issue", ""), ("issue_agent", "vod")):
            r = client.post(f"/api/events/{seq}/knowledge", json={"scope": scope, "content": f"知识-{scope}"}, headers=_bot_headers())
            assert r.status_code == 200, r.text
            fp = knowledge_path(_REPO_KEY, scope, agent_type=agent, issue_num=5)
            import os as _os
            assert _os.path.isfile(fp), fp

    def test_knowledge_requires_bot_token(self):
        seq = _seed_event()
        r = client.post(f"/api/events/{seq}/knowledge", json={"scope": "issue", "content": "x"}, headers=_headers)
        assert r.status_code == 403

    def test_knowledge_injected_into_prompt(self):
        seq = _seed_event(issue_id="#5")
        client.post(f"/api/events/{seq}/knowledge", json={"scope": "project", "content": "此仓库 PR 走内部评审"}, headers=_bot_headers())
        from src.prompt_builder import build_conversation_prompt
        evt = {"seq": seq, "repo_key": _REPO_KEY, "issue_id": "#5", "agent_type": "vod",
               "event_type": "issue_opened", "title": "t", "status": "completed", "result": "r"}
        prompt = build_conversation_prompt(evt, "你好", [], "vod", 5)
        assert "此仓库 PR 走内部评审" in prompt


class TestFlowReset:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_flow_reset_cancels_and_rewinds(self):
        seq = _seed_event()
        _seed_issue_state("coding")
        tid = service.enqueue_task(agent_session_id="sess-1", runtime_id=1, prompt="p",
                                   repo_key=_REPO_KEY, agent_type="coding", issue_id="#5")
        r = client.post(f"/api/events/{seq}/flow/reset", json={"to_stage": "vod"}, headers=_bot_headers())
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert r.json()["stage"] == "vod"
        from src.api.session_store import get_issue_stage
        assert get_issue_stage(_REPO_KEY, "#5")["stage"] == "vod"
        task = service.get_task(tid)
        assert task and task["status"] == "cancelled"

    def test_flow_reset_requires_bot_token(self):
        seq = _seed_event()
        _seed_issue_state("coding")
        r = client.post(f"/api/events/{seq}/flow/reset", json={"to_stage": "vod"}, headers=_headers)
        assert r.status_code == 403

    def test_flow_reset_rejects_forward(self):
        seq = _seed_event()
        _seed_issue_state("coding")
        r = client.post(f"/api/events/{seq}/flow/reset", json={"to_stage": "coding"}, headers=_bot_headers())
        assert r.status_code == 400


class TestOutcomeRecords:
    """对话产出留痕：knowledge / flow 写入线程 system 消息（📌/🔄）"""

    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_knowledge_and_flow_write_system_messages(self):
        seq = _seed_event()
        _seed_issue_state("coding")
        client.post(f"/api/events/{seq}/knowledge", json={"scope": "issue", "content": "验收标准 = 接口可跑通"}, headers=_bot_headers())
        client.post(f"/api/events/{seq}/flow/reset", json={"to_stage": "vod"}, headers=_bot_headers())
        r = client.get(f"/api/events/{seq}/thread", headers=_headers)
        msgs = r.json()["messages"]
        system = [m["content"] for m in msgs if m["role"] == "system"]
        assert any(c.startswith("📌") and "issue" in c for c in system)
        assert any(c.startswith("🔄") and "vod" in c for c in system)


class TestSseStream:
    """SSE 流式对话端点 /chat/stream"""

    def setup_method(self):
        _clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "direct"

    def teardown_method(self):
        _clean()
        os.environ.pop("DEVOPS_EXECUTION_MODE", None)

    def test_stream_returns_sse_events(self):
        seq = _seed_event()
        _seed_issue_state("coding")
        with patch("src.runtime.executor.consume_stream_async", side_effect=_fake_stream):
            r = client.post(f"/api/events/{seq}/chat/stream", json={"message": "hi"}, headers=_headers)
        assert r.status_code == 200
        assert "text/event-stream" in (r.headers.get("content-type") or "")
        import json as _json
        done_found = False
        for line in r.text.split("\n"):
            if line.startswith("data: "):
                evt = _json.loads(line[6:])
                if evt.get("type") == "done":
                    done_found = True
                    assert evt["reply"] == "模拟 agent 回复"
        assert done_found
        # 线程已写入 user + agent 两行
        t = client.get(f"/api/events/{seq}/thread", headers=_headers).json()
        assert len(t["messages"]) == 2

    def test_stream_rejected_when_done(self):
        seq = _seed_event()
        _seed_issue_state("done")
        r = client.post(f"/api/events/{seq}/chat/stream", json={"message": "hi"}, headers=_headers)
        assert r.status_code == 409


class TestKnowledgeFilesDelivery:
    """enqueue_task 打包共享知识快照（knowledge_files）"""

    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_enqueue_packs_knowledge_files(self):
        seq = _seed_event(issue_id="#5")
        client.post(f"/api/events/{seq}/knowledge", json={"scope": "issue", "content": "验收标准 = 接口可跑通"}, headers=_bot_headers())
        from src.runtime.service import enqueue_task
        tid = enqueue_task(agent_session_id="sess-2", runtime_id=1, prompt="p",
                           repo_key=_REPO_KEY, agent_type="vod", issue_id="#5")
        task = service.get_task(tid)
        assert task and task.get("knowledge_files")
        import json
        kf = json.loads(task["knowledge_files"])
        assert kf and any("knowledge.md" in rel for rel in kf)


class TestPromptSkillPlaceholder:
    """Fix B：prompt 技能路径用 {{SKILL_DIR}} 占位符，不再硬编码服务端路径"""

    def test_conversation_prompt_uses_placeholder(self):
        from src.prompt_builder import build_conversation_prompt
        evt = {"seq": 1, "repo_key": _REPO_KEY, "issue_id": "#5", "agent_type": "vod",
               "event_type": "issue_opened", "title": "t", "status": "completed", "result": "r"}
        prompt = build_conversation_prompt(evt, "hi", [], "vod", 5)
        assert "{{SKILL_DIR}}/event-dialogue/SKILL.md" in prompt
        assert "/opt/grape/skills" not in prompt


class TestConversationQueueMode:
    """Fix A/C/E：对话队列任务下发技能 + 超时回显/取消 + 串行槽绑定"""

    def setup_method(self):
        _clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_local"

    def teardown_method(self):
        _clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "direct"

    def _seed_agent_session(self):
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status) "
                      "VALUES ('conv-sess',?,?,1,'idle')", (_REPO_KEY, "vod"))
            db.commit()

    async def _fake_done(self, *a, **k):
        return {"result_text": "模拟回复", "usage_json": "{}", "status": "completed"}

    def test_queue_task_packs_event_dialogue_skill(self):
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_remote"
        seq = _seed_event()
        _seed_issue_state("coding")
        self._seed_agent_session()
        with patch("src.runtime.service.wait_task_terminal", side_effect=self._fake_done):
            r = client.post(f"/api/events/{seq}/chat", json={"message": "hi"}, headers=_headers)
        assert r.status_code == 200, r.text
        assert r.json()["reply"] == "模拟回复"
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT skill_files, issue_session_id FROM agent_tasks WHERE event_type='event_conversation' ORDER BY created_at DESC LIMIT 1").fetchone()
        assert row and row[0]
        import json
        sf = json.loads(row[0])
        assert "event-dialogue" in sf and "SKILL.md" in sf["event-dialogue"]

    def test_queue_timeout_writes_failure_and_cancels(self):
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_remote"
        seq = _seed_event()
        _seed_issue_state("coding")
        self._seed_agent_session()

        async def _raise(*a, **k):
            raise TimeoutError("timeout")

        with patch("src.runtime.service.wait_task_terminal", side_effect=_raise):
            r = client.post(f"/api/events/{seq}/chat", json={"message": "hi"}, headers=_headers)
        assert r.status_code == 200, r.text
        assert "超时" in r.json()["reply"]
        t = client.get(f"/api/events/{seq}/thread", headers=_headers).json()
        msgs = [m["content"] for m in t["messages"] if m["role"] == "agent"]
        assert any("超时" in c for c in msgs)
        with db_lock:
            row = db.cursor().execute("SELECT status FROM agent_tasks WHERE event_type='event_conversation' ORDER BY created_at DESC LIMIT 1").fetchone()
        assert row and row[0] == "cancelled"


class TestRedispatchRuntimeZero:
    """重派终态任务修复：旧 task runtime_id=0 时重新解析路由 + cwd 走新结构（workdir-issue-centric-design.md）。"""

    def test_redispatch_resolves_runtime_and_new_cwd(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE id LIKE 't_redispatch%'")
            c.execute("DELETE FROM events WHERE repo_key=?", (_REPO_KEY,))
            c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, issue_id, payload, status, result) VALUES (?,?,?,?,?,?,?,?)",
                      (_REPO_KEY, "issue_comment", "vod", "re", "#5", "{}", "failed", "x"))
            seq = c.lastrowid
            c.execute("INSERT OR IGNORE INTO issue_agent_session (id, repo_key, agent_type, issue_id, runtime_session_id, status) VALUES ('iss-redis',?, 'vod', '#5', '', 'idle')", (_REPO_KEY,))
            # 先创建引擎
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'redis-cred%'")
            c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) VALUES ('hredis','redis-cred','redis-dev','global','tester','active')")
            _cid = c.lastrowid
            c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
                      "VALUES ('opencode','opencode',?, 'online', datetime('now'), 'global', '', 1, 1)", (_cid,))
            _rid = c.lastrowid
            # 创建 session（含 runtime_id 供 redispatch fallback）
            c.execute("INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status) VALUES ('redis-sess',?,?,?,'idle')",
                      (_REPO_KEY, "vod", _rid))
            # 创建旧 task（runtime_id=0，模拟 120s 超时回收产物）
            c.execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, repo_key, agent_type, skill, prompt, cwd, runtime_session_id, event_type, account, user_id, repo_gitcode_token, env_extra, skill_files, server_url, issue_session_id, project_info_files, project_info_meta, knowledge_files, affinity_runtime_id, pool_agent_type) "
                "VALUES ('t_redispatch0', 'vod:'||?, 0, ?, '#5', ?, 'vod', 'repo-onboarding', 'p', '/old/vod/issues/5', 'ses_old', 'issue_comment', 'acct', 0, '', '{}', '{}', '', 'iss-redis', '{}', '{}', '{}', 0, '')",
                (_REPO_KEY, seq, _REPO_KEY))
            db.commit()

        from src.api.event_pipeline import get_agent_workdir
        expected_cwd = get_agent_workdir(_REPO_KEY, "vod", "#5")

        # 覆盖 30s 幂等：直接调内部逻辑不方便，改用真实端点但清掉幂等 meta
        from src.user.db import get_meta_value, set_meta_value
        set_meta_value(f"redispatch:{seq}", "")

        r = client.post(f"/api/events/{seq}/redispatch", headers=_headers)
        assert r.status_code == 200, r.text
        with db_lock:
            row = db.cursor().execute(
                "SELECT runtime_id, cwd, runtime_session_id, issue_session_id, pool_agent_type FROM agent_tasks WHERE event_seq=? ORDER BY created_at DESC LIMIT 1",
                (seq,)).fetchone()
        assert row, "应创建新任务"
        assert row[0] and row[0] != 0, f"runtime_id 应重新解析非 0，实际 {row[0]}"
        assert row[1] == expected_cwd, f"cwd 应走新结构，实际 {row[1]} 期望 {expected_cwd}"
        assert row[2] == "ses_old", "runtime_session_id 应保留原值（续接会话），实际={row[2]}"
        assert row[3] == "iss-redis", "issue_session_id 应复用"
        # 事件状态回写 running
        with db_lock:
            st = db.cursor().execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()
        assert st and st[0] == "running"
        # 清理（幂等 meta 残留避免影响其他用例）
        with db_lock:
            db.cursor().execute("DELETE FROM events WHERE seq=?", (seq,))
            db.cursor().execute("DELETE FROM agent_tasks WHERE event_seq=?", (seq,))
            db.cursor().execute("DELETE FROM agent_tasks WHERE id LIKE 't_redispatch%'")
            db.cursor().execute("DELETE FROM devops_agent_session WHERE id LIKE 'redis-sess%'")
            db.cursor().execute("DELETE FROM agent_runtimes WHERE credential_id IN (SELECT id FROM runtime_credentials WHERE name LIKE 'redis-cred%')")
            db.cursor().execute("DELETE FROM runtime_credentials WHERE name LIKE 'redis-cred%'")
            db.commit()

    def test_redispatch_cancels_old_queued_task(self):
        """重派取消旧 queued 任务：重派创建新任务后，旧任务应置 cancelled——
        防止 _running_issue_ids 仍将旧任务计为执行态，或以旧 runtime_id 被重复 claim。
        """
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE id LIKE 't_redispatch%'")
            c.execute("DELETE FROM events WHERE repo_key=?", (_REPO_KEY,))
            c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, issue_id, payload, status, result) VALUES (?,?,?,?,?,?,?,?)",
                      (_REPO_KEY, "issue_comment", "vod", "re2", "#5", "{}", "failed", "x"))
            seq = c.lastrowid
            c.execute("INSERT OR IGNORE INTO issue_agent_session (id, repo_key, agent_type, issue_id, runtime_session_id, status) VALUES ('iss-redis',?, 'vod', '#5', '', 'idle')", (_REPO_KEY,))
            # 先创建引擎
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'redis-cred%'")
            c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) VALUES ('hredis','redis-cred','redis-dev','global','tester','active')")
            _cid = c.lastrowid
            c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
                      "VALUES ('opencode','opencode',?, 'online', datetime('now'), 'global', '', 1, 1)", (_cid,))
            _rid = c.lastrowid
            # 创建 session（含 runtime_id 供 redispatch fallback）
            c.execute("INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status) VALUES ('redis-sess2',?,?,?,'idle')",
                      (_REPO_KEY, "vod", _rid))
            # 创建旧 task（runtime_id 设为实际引擎 id，status=queued 供 redispatch requeue）
            c.execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, repo_key, agent_type, skill, prompt, cwd, runtime_session_id, event_type, account, user_id, repo_gitcode_token, env_extra, skill_files, server_url, issue_session_id, project_info_files, project_info_meta, knowledge_files, affinity_runtime_id, pool_agent_type, status) "
                "VALUES ('t_redispatch_cancel0', 'vod:'||?, ?, ?, '#5', ?, 'vod', 'repo-onboarding', 'p', '/old/vod/issues/5', 'ses_old', 'issue_comment', 'acct', 0, '', '{}', '{}', '', 'iss-redis', '{}', '{}', '{}', 0, '', 'queued')",
                (_REPO_KEY, _rid, seq, _REPO_KEY))
            db.commit()

        from src.user.db import set_meta_value
        set_meta_value(f"redispatch:{seq}", "")

        r = client.post(f"/api/events/{seq}/redispatch", headers=_headers)
        assert r.status_code == 200, r.text
        with db_lock:
            # 旧任务为 queued（非终态非运行中），redispatch 会新建任务，旧任务保持原状态
            old = db.cursor().execute("SELECT status FROM agent_tasks WHERE id='t_redispatch_cancel0'").fetchone()
            assert old, "旧任务应存在"
            # 新任务应存在（queued 或 dispatched）
            tasks = db.cursor().execute(
                "SELECT id, status FROM agent_tasks WHERE event_seq=? ORDER BY created_at DESC", (seq,)).fetchall()
            assert len(tasks) >= 2, f"应有新旧两个任务，实际={len(tasks)}"
            new = [t for t in tasks if t[0] != 't_redispatch_cancel0'][0]
            assert new[1] in ("queued", "dispatched"), f"新任务状态应为 queued/dispatched，实际={new[1]}"
        # 清理
        with db_lock:
            db.cursor().execute("DELETE FROM events WHERE seq=?", (seq,))
            db.cursor().execute("DELETE FROM agent_tasks WHERE event_seq=?", (seq,))
            db.cursor().execute("DELETE FROM agent_tasks WHERE id LIKE 't_redispatch%'")
            db.cursor().execute("DELETE FROM devops_agent_session WHERE id LIKE 'redis-sess%'")
            db.cursor().execute("DELETE FROM agent_runtimes WHERE credential_id IN (SELECT id FROM runtime_credentials WHERE name LIKE 'redis-cred%')")
            db.cursor().execute("DELETE FROM runtime_credentials WHERE name LIKE 'redis-cred%'")
            db.commit()

    def test_fail_task_clears_dead_session(self):
        """连续 3 次 fail（exit_code < 0）→ 清空 runtime_session_id（smart-session-recovery-design.md §2.3）。"""
        from src.runtime.service import fail_task, get_task
        from src.api.session_store import update_issue_session, get_issue_session as _get_iss
        # 1) seed issue_agent_session + 3 个 failed task
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue_agent_session WHERE id='iss-dead'")
            c.execute("INSERT OR REPLACE INTO issue_agent_session (id, repo_key, agent_type, issue_id, runtime_session_id, status) VALUES ('iss-dead',?, 'vod', '#9', 'ses_dead', 'idle')", (_REPO_KEY,))
            for i in range(3):
                c.execute(
                    "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, repo_key, agent_type, skill, prompt, cwd, issue_session_id, status, exit_code, created_at) "
                    "VALUES ('t_dead%d', 'vod:'||?, 1, %d, '#9', ?, 'vod', 's', 'p', '/tmp', 'iss-dead', 'failed', -15, datetime('now', '-%d minutes'))" % (i, i, 10-2*i),
                    (_REPO_KEY, _REPO_KEY))
            db.commit()
        # 2) 验证 session 有值
        iss = _get_iss(_REPO_KEY, "vod", "#9")
        assert iss and iss.get("runtime_session_id") == "ses_dead"
        # 3) 再 fail 一次（连续第 3+1）
        fail_task("t_dead2", "test", exit_code=-1)
        # 4) session 应被清空
        iss2 = _get_iss(_REPO_KEY, "vod", "#9")
        assert iss2.get("runtime_session_id") == "", f"连续失败后 session 应被清空，实际={iss2.get('runtime_session_id')}"
        # 清理
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue_agent_session WHERE id='iss-dead'")
            c.execute("DELETE FROM agent_tasks WHERE issue_session_id='iss-dead'")
            db.commit()
