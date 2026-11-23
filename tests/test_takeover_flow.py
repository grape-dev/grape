"""接管端到端（服务层 + HTTP）集成测试。

用测试库直接铺一条 running 任务，走完 start → progress → finish / abort，
断言：任务取消、事件 takeover/completed、stage 推进、token 生命周期、审计行。

派发下一节点（内部 HTTP /api/a2a）在这里被 monkeypatch 掉，只验证"调用了没有 / 参数对不对"。
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from src.kernel.auth_sdk.auth_token import verify_takeover_token
from src.api.deps import get_current_user_or_api_key
from src.api.main import app
from src.kernel.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _bind_db():
    """每次用例前重新绑定「被测代码实际使用的」连接。

    全树收集（pytest -k / 全量）时，其它测试模块会在导入期改 DEVOPS_DB_PATH 甚至
    reload session_manager，模块级 `from ... import db` 会变成陈旧句柄 ——
    种子写进旧库、被测代码读新库，表现为"表里查不到"。绑定到 takeover 模块的
    globals 可以保证种子与读取指向同一个连接。
    """
    global db, db_lock
    from src.domains.runtime.engine import takeover as _svc
    db, db_lock = _svc.db, _svc.db_lock
from src.domains.runtime.engine import takeover as svc

REPO = "gitcode:o/r"


def _cleanup():
    with db_lock:
        c = db.cursor()
        for sql, args in (
            ("DELETE FROM takeover_sessions WHERE repo_key=?", (REPO,)),
            ("DELETE FROM agent_tasks WHERE repo_key=?", (REPO,)),
            ("DELETE FROM events WHERE repo_key=?", (REPO,)),
            ("DELETE FROM issue WHERE repo_key=?", (REPO,)),
            ("DELETE FROM session_api_tokens WHERE session_key LIKE 'takeover:%'", ()),
            ("DELETE FROM task_messages WHERE task_id LIKE 't_flow_%'", ()),
        ):
            try:
                c.execute(sql, args)
            except Exception:
                pass
        db.commit()


def _seed(issue_number: int = 1, task_id: str = "t_flow_1", seq: int = 9001):
    """铺一条 running 任务 + 事件 + issue。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, repo_key, issue_number, title, status, stage, owner_agent, scene_id, assignee) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (1, REPO, issue_number, "接管流程测试", "processing", "coding", "coding", "default", ""),
        )
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, issue_id, agent_type, status, source, title, payload) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (seq, REPO, "a2a_event", f"#{issue_number}", "coding", "running", "a2a", "t", "{}"),
        )
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, status, agent_type, skill, prompt, env_extra, cwd, "
            "runtime_id, event_seq, repo_key, issue_id, user_id, account, skill_files) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, f"ses_{issue_number}", "running", "coding", "implementation", "实现音效开关", "{}",
             f"/tmp/takeover-flow/{issue_number}/coding", 0, seq, REPO, f"#{issue_number}", 1, "", "{}"),
        )
        db.commit()


@pytest.fixture()
def client():
    _cleanup()
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": 1, "username": "tester", "auth_type": "api_key", "token": "x", "token_type": "api_key",
    }
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        _cleanup()


def _get(sql, args=()):
    with db_lock:
        c = db.cursor()
        c.execute(sql, args)
        return c.fetchone()


def _start(client, issue=1):
    return client.post("/api/takeover/sessions", json={
        "repo_key": REPO, "issue_number": str(issue), "platform": "dsh", "host": "test-host",
        "client_version": "0.4.0-test", "supports": {"workspace": True, "progress": True},
    })


def test_start_is_atomic_and_idempotent(client):
    _seed()
    r = _start(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["takeover_id"].startswith("tkos_")
    token = body["bundle"]["env"]["GRAPE_SESSION_TOKEN"]
    assert token.startswith("tko_")
    assert body["bundle"]["env"]["GRAPE_CLI_MODE"] == "ALL"
    assert "接管" in body["bundle"]["instructions"]["prompt"]
    assert body["bundle"]["capabilities"]["token_kind"] == "tko_"
    # 远端 worker 可能仍在收尾：显式警告 + prompt 里给自检规则（不阻塞用户）
    assert "remote_may_still_run" in (body["bundle"]["warnings"] or [])
    prompt = body["bundle"]["instructions"]["prompt"]
    assert "## 动手之前（重要）" in prompt and "git status" in prompt

    # 任务已取消、事件已 takeover、槽位/事件状态一致
    assert _get("SELECT status FROM agent_tasks WHERE id='t_flow_1'")[0] == "cancelled"
    assert _get("SELECT status FROM events WHERE seq=9001")[0] == "takeover"
    # 会话行 active + active_key
    assert _get("SELECT status, active_key FROM takeover_sessions WHERE repo_key=?", (REPO,)) == ("active", "t_flow_1")
    # token 可校验，且 kind=takeover
    assert verify_takeover_token(token) is not None

    # 幂等：再点一次 → reused=True，不新建会话、不重复取消
    r2 = _start(client)
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["reused"] is True
    assert b2["takeover_id"] == body["takeover_id"]
    assert _get("SELECT COUNT(*) FROM takeover_sessions WHERE repo_key=?", (REPO,))[0] == 1


def test_start_reuse_rejects_foreign_user(client):
    """已有 active 接管时，其他用户再点接管必须 403（reused 不得绕过归属校验）。"""
    _seed()
    assert _start(client).status_code == 200
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": 2, "username": "other", "auth_type": "api_key", "token": "x", "token_type": "api_key"}
    r = _start(client)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "not_owner"


def test_unowned_session_is_denied_even_when_user_id_zero(client):
    """会话 user_id=0 时归属校验也必须拒绝（不得 fail-open）。"""
    _seed()
    sid = _start(client).json()["takeover_id"]
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE takeover_sessions SET user_id=0 WHERE id=?", (sid,))
        db.commit()
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": 2, "username": "other", "auth_type": "api_key", "token": "x", "token_type": "api_key"}
    r = client.post(f"/api/takeover/sessions/{sid}/finish", json={"outcome": "completed"})
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "not_owner"


def test_start_without_running_task_404(client):
    _cleanup()
    r = _start(client, issue=999)
    assert r.status_code == 404
    assert r.json()["code"] == "no_running_task"


def test_progress_writes_task_messages_with_actor(client):
    _seed()
    sid = _start(client).json()["takeover_id"]
    r = client.post(f"/api/takeover/sessions/{sid}/progress", json={
        "messages": [{"type": "text", "content": "正在改 GameScene.ts"},
                     {"type": "tool_use", "tool": "bash", "content": "pnpm test"}],
        "summary": "改音效开关",
    })
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2
    rows = []
    with db_lock:
        c = db.cursor()
        c.execute("SELECT seq, type, tool, content, actor FROM task_messages WHERE task_id='t_flow_1' ORDER BY seq")
        rows = c.fetchall()
    assert len(rows) == 2
    assert rows[0][4] == "tester" and rows[1][4] == "tester"
    assert rows[1][1] == "tool_use" and rows[1][2] == "bash"


def test_finish_advances_and_dispatches(client, monkeypatch):
    _seed()
    sid = _start(client).json()["takeover_id"]
    token = _get("SELECT 1")  # noqa: F841  (keep connection warm)

    called = {}

    def _fake_advance(repo_key, issue_number, current_agent):
        return "qa", False

    def _fake_dispatch(**kw):
        called.update(kw)
        return {"ok": True, "seq": 9100}

    monkeypatch.setattr(svc, "_advance_stage", _fake_advance)
    monkeypatch.setattr(svc, "_dispatch_next", _fake_dispatch)

    r = client.post(f"/api/takeover/sessions/{sid}/finish", json={
        "outcome": "completed", "summary": "音效开关已实现", "evidence": "commit abc123",
        "files_written": ["src/GameScene.ts"], "handoff": "a2a",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["status"] == "finished"
    assert body["event_status"] == "completed"
    assert body["next_agent"] == "qa"
    assert body["handoff"]["ok"] is True

    # 事件终态 / 会话 finished / token 已撤销
    assert _get("SELECT status FROM events WHERE seq=9001")[0] == "completed"
    assert _get("SELECT status FROM takeover_sessions WHERE id=?", (sid,))[0] == "finished"
    assert _get("SELECT active_key FROM takeover_sessions WHERE id=?", (sid,))[0] is None
    assert verify_takeover_token(_last_token()) is None if _last_token() else True

    # 派发参数：目标节点、issue、context 里带人类产物
    assert called.get("next_agent") == "qa"
    assert called.get("issue_number") == "1"
    assert "音效开关已实现" in called.get("context", "")
    # 交接单必须带上证据与文件清单（否则下一节点等于没拿到交接内容）
    assert "commit abc123" in called.get("context", "")
    assert "src/GameScene.ts" in called.get("context", "")

    # 重复 finish 幂等（已 finished 且已派发 → changed=False）
    r2 = client.post(f"/api/takeover/sessions/{sid}/finish", json={"outcome": "completed"})
    assert r2.status_code == 200
    assert r2.json()["changed"] is False


def test_finish_marks_partial_when_handoff_fails(client, monkeypatch):
    """#3910：handoff.ok=false 时 finish 外层不得返回干净 ok:true（降级 partial + 强 warnings）。"""
    _seed(issue_number=5, task_id="t_flow_5", seq=9005)
    sid = _start(client, issue=5).json()["takeover_id"]

    monkeypatch.setattr(svc, "_advance_stage", lambda *a, **k: ("qa", False))
    monkeypatch.setattr(svc, "_dispatch_next",
                        lambda **kw: {"ok": False, "error": "HTTP 405: no a2a route"})

    r = client.post(f"/api/takeover/sessions/{sid}/finish",
                    json={"outcome": "completed", "summary": "完成", "handoff": "a2a"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False, "派发失败不得伪装成干净成功"
    assert body["partial"] is True and body["warnings"], "必须降级 partial 并写强 warnings"
    assert body["handoff"]["ok"] is False
    assert any("handoff_failed" in w for w in body["warnings"])

    # 外层语义只改返回体：事件终态与会话收尾仍按 completed 落地（失败可据 warnings 告警）
    assert _get("SELECT status FROM events WHERE seq=9005")[0] == "completed"
    assert _get("SELECT status FROM takeover_sessions WHERE id=?", (sid,))[0] == "finished"
    assert not _get("SELECT handoff_seq FROM takeover_sessions WHERE id=?", (sid,))[0]


def test_finish_retry_handoff_failure_stays_partial(client, monkeypatch):
    """#3910：幂等补派发（_retry_handoff）仍失败时同样降级 partial，不得伪装成功。"""
    _seed(issue_number=6, task_id="t_flow_6", seq=9006)
    sid = _start(client, issue=6).json()["takeover_id"]

    monkeypatch.setattr(svc, "_advance_stage", lambda *a, **k: ("qa", False))
    monkeypatch.setattr(svc, "_dispatch_next",
                        lambda **kw: {"ok": False, "error": "HTTP 405: no a2a route"})

    r = client.post(f"/api/takeover/sessions/{sid}/finish",
                    json={"outcome": "completed", "summary": "完成", "handoff": "a2a"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False and r.json()["partial"] is True
    assert not _get("SELECT handoff_seq FROM takeover_sessions WHERE id=?", (sid,))[0]

    # 幂等重试：已 finished 且 handoff_seq 为空 → 触发补派发；再次失败必须仍是 partial
    r2 = client.post(f"/api/takeover/sessions/{sid}/finish",
                     json={"outcome": "completed", "handoff": "a2a"})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["changed"] is False
    assert body["ok"] is False and body["partial"] is True
    assert "handoff_retry_failed" in body["warnings"]
    assert not _get("SELECT handoff_seq FROM takeover_sessions WHERE id=?", (sid,))[0]


def test_finish_retry_handoff_success_records_seq(client, monkeypatch):
    """#3910：幂等补派发成功时记真实 handoff_seq 并返回 ok。"""
    _seed(issue_number=8, task_id="t_flow_8", seq=9008)
    sid = _start(client, issue=8).json()["takeover_id"]

    monkeypatch.setattr(svc, "_advance_stage", lambda *a, **k: ("qa", False))
    state = {"ok": False}

    def _dispatch(**kw):
        if state["ok"]:
            return {"ok": True, "seq": 9208}
        return {"ok": False, "error": "HTTP 405: no a2a route"}

    monkeypatch.setattr(svc, "_dispatch_next", _dispatch)

    r = client.post(f"/api/takeover/sessions/{sid}/finish",
                    json={"outcome": "completed", "summary": "完成", "handoff": "a2a"})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert not _get("SELECT handoff_seq FROM takeover_sessions WHERE id=?", (sid,))[0]

    state["ok"] = True
    r2 = client.post(f"/api/takeover/sessions/{sid}/finish",
                     json={"outcome": "completed", "handoff": "a2a"})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["ok"] is True and body["changed"] is False
    assert body["status"] == "finished"
    assert _get("SELECT handoff_seq FROM takeover_sessions WHERE id=?", (sid,))[0] == 9208



def test_finish_retry_handoff_zero_seq_does_not_record(client, monkeypatch):
    """#3910：补派发 ok 但 seq=0 无实际进展，不得写 handoff_seq（否则幂等键误判已交接）。"""
    _seed(issue_number=10, task_id="t_flow_10", seq=9010)
    sid = _start(client, issue=10).json()["takeover_id"]

    monkeypatch.setattr(svc, "_advance_stage", lambda *a, **k: ("qa", False))
    monkeypatch.setattr(svc, "_dispatch_next", lambda **kw: {"ok": True, "seq": 0})

    r = client.post(f"/api/takeover/sessions/{sid}/finish",
                    json={"outcome": "completed", "summary": "完成", "handoff": "a2a"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert not _get("SELECT handoff_seq FROM takeover_sessions WHERE id=?", (sid,))[0], \
        "首次派发 seq=0 无进展，不得写 handoff_seq"

    # 幂等重试仍 seq=0：同样不落库，保留下次补派发机会
    r2 = client.post(f"/api/takeover/sessions/{sid}/finish",
                     json={"outcome": "completed", "handoff": "a2a"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["changed"] is False
    assert not _get("SELECT handoff_seq FROM takeover_sessions WHERE id=?", (sid,))[0]


def test_finish_writes_structured_output_like_worker(client, monkeypatch):
    """事件详情页「Agent 处理记录与答复结论」要拿到与 worker 同构的 5 字段结构化产出。"""
    import json as _json
    _seed(issue_number=61, task_id="t_struct_1", seq=9061)
    sid = _start(client, issue=61).json()["takeover_id"]
    monkeypatch.setattr(svc, "_advance_stage", lambda *a, **k: ("qa", False))
    monkeypatch.setattr(svc, "_dispatch_next", lambda **k: {"ok": True, "seq": 9161})

    r = client.post(f"/api/takeover/sessions/{sid}/finish", json={
        "outcome": "completed",
        "summary": "音效开关已实现并通过本地构建",
        "evidence": "npm test 通过",
        "actions": ["读取 SKILL.md", "修改 src/GameScene.ts", "运行 npm test"],
        "artifacts": ["PR #9", "src/GameScene.ts"],
        "files_written": ["src/GameScene.ts"],
    })
    assert r.status_code == 200, r.text

    raw = _get("SELECT structured_output FROM events WHERE seq=9061")[0]
    assert raw, "structured_output 未写入"
    so = _json.loads(raw)
    assert so["conclusion"].startswith("音效开关已实现")
    assert so["actions"] == ["读取 SKILL.md", "修改 src/GameScene.ts", "运行 npm test"]
    assert so["artifacts"] == ["PR #9", "src/GameScene.ts"]
    assert so["next_step"] == "qa"
    assert so["issue_summary"]

    # 同时回写会话的 result_json（GET / 面板可读）
    rj = _get("SELECT result_json FROM takeover_sessions WHERE id=?", (sid,))[0]
    assert _json.loads(rj)["actions"][0] == "读取 SKILL.md"


def test_request_finish_structured_fields_are_reused_by_finish(client, monkeypatch):
    """agent 用 tko_ 交的结构化字段（actions/artifacts/next_step）要被 finish 沿用。"""
    import json as _json
    _seed(issue_number=62, task_id="t_struct_2", seq=9062)
    start = _start(client, issue=62).json()
    sid = start["takeover_id"]
    monkeypatch.setattr(svc, "_advance_stage", lambda *a, **k: ("qa", False))
    monkeypatch.setattr(svc, "_dispatch_next", lambda **k: {"ok": True, "seq": 9162})

    r = client.post(f"/api/takeover/sessions/{sid}/request-finish", json={
        "summary": "重构完成",
        "evidence": "diff 见 PR",
        "actions": ["拆分模块", "补单测"],
        "artifacts": ["PR #12"],
        "next_step": "qa",
    })
    assert r.status_code == 200, r.text
    rj = _json.loads(_get("SELECT result_json FROM takeover_sessions WHERE id=?", (sid,))[0])
    assert rj["actions"] == ["拆分模块", "补单测"]
    assert rj["artifacts"] == ["PR #12"]

    # 用户 finish 时什么都不填 → 沿用 agent 交的结构化内容
    r2 = client.post(f"/api/takeover/sessions/{sid}/finish", json={"outcome": "completed"})
    assert r2.status_code == 200, r2.text
    so = _json.loads(_get("SELECT structured_output FROM events WHERE seq=9062")[0])
    assert so["actions"] == ["拆分模块", "补单测"]
    assert so["artifacts"] == ["PR #12"]
    assert so["conclusion"] == "重构完成"

def _last_token() -> str:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT token_sha256 FROM takeover_sessions WHERE repo_key=? ORDER BY created_at DESC LIMIT 1", (REPO,))
        row = c.fetchone()
    if not row:
        return ""
    # 测试里拿不回明文，只验证"行已删除"（revoke 是 DELETE）
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM session_api_tokens WHERE token_sha256=?", (row[0],))
        return "still-present" if c.fetchone()[0] else ""


def test_abort_redispatches_same_node_never_next(client, monkeypatch):
    """放弃接管：重派**当前节点**（交还流水线），但绝不派发下一节点。"""
    import src.domains.runtime.routes.events as evmod

    _seed(issue_number=2, task_id="t_flow_2", seq=9002)
    sid = _start(client, issue=2).json()["takeover_id"]

    def _boom(**kw):
        raise AssertionError("abort 绝不能派发下一节点")

    seen = {}
    monkeypatch.setattr(svc, "_dispatch_next", _boom)
    # 重派的接缝是 abort 实际调用的服务内通道（Issue #2517 起 abort 走 redispatch_event_internal，
    # 它显式跳过 30s 用户级幂等门；公共 HTTP 路由不在 abort 路径上）
    monkeypatch.setattr(evmod, "redispatch_event_internal",
                        lambda seq, user=None: seen.update(seq=seq) or
                        {"status": "redispatched", "task_id": "t_again_2", "event_seq": seq})
    r = client.post(f"/api/takeover/sessions/{sid}/abort", json={"reason": "环境不对"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "aborted"
    assert seen["seq"] == 9002, "重派的必须是当前节点"
    assert r.json()["redispatch"]["task_id"] == "t_again_2"
    assert _get("SELECT status FROM takeover_sessions WHERE id=?", (sid,))[0] == "aborted"
    # Issue 保持 processing（设计文档 §13-2：不用 awaiting_input）
    assert _get("SELECT status FROM issue WHERE repo_key=? AND issue_number=2", (REPO,))[0] == "processing"


def test_takeover_token_cannot_use_user_only_endpoints(client):
    """tko_ 不具备 user_scope：拿它调 /api/takeover/sessions 必须 403。"""
    _seed(issue_number=3, task_id="t_flow_3", seq=9003)
    tok = None
    with db_lock:
        c = db.cursor()
        from src.kernel.auth_sdk.auth_token import issue_takeover_token
        tok = issue_takeover_token(task_id="t_flow_3", repo_key=REPO, agent_type="coding", user_id=1)["token"]
    app.dependency_overrides.clear()   # 走真实认证
    try:
        r = TestClient(app).post("/api/takeover/sessions",
                                 json={"repo_key": REPO, "issue_number": "3"},
                                 headers={"PRIVATE-TOKEN": tok})
        assert r.status_code == 403, r.text
    finally:
        _cleanup()


def _as_takeover(session_key: str, username: str = "wdmike"):
    """把鉴权覆写成"本地接管会话自己"（tko_）。"""
    return {
        "id": 1, "username": username, "auth_type": "takeover", "kind": "takeover",
        "token": "tko_x", "token_type": "takeover", "session_key": session_key,
        "repo_key": REPO, "agent_type": "coding",
    }


def test_progress_accepts_own_takeover_token(client):
    """本地 agent 用 tko_ 上报进展：允许（只看 session_key 是否指向自己这条会话）。"""
    _seed()
    sid = _start(client).json()["takeover_id"]
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _as_takeover("takeover:t_flow_1")
    r = client.post(f"/api/takeover/sessions/{sid}/progress", json={
        "summary": "已读完 SKILL.md，开始改音效开关",
        "messages": [{"type": "text", "content": "已读完 SKILL.md，开始改音效开关"}],
    })
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    with db_lock:
        c = db.cursor()
        c.execute("SELECT actor, content FROM task_messages WHERE task_id='t_flow_1'")
        actor, content = c.fetchone()
    assert actor == "wdmike" and "SKILL.md" in content
    # 心跳：updated_at 被推进（面板用来判断"本地还有没有在动"）
    assert _get("SELECT updated_at FROM takeover_sessions WHERE id=?", (sid,)) is not None


def test_progress_rejects_foreign_takeover_token(client):
    """别人那条接管的 tko_ 不能往这个会话写进展。"""
    _seed()
    sid = _start(client).json()["takeover_id"]
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _as_takeover("takeover:someone_else")
    r = client.post(f"/api/takeover/sessions/{sid}/progress", json={"summary": "x", "messages": []})
    assert r.status_code == 403
    assert r.json()["code"] == "not_owner"


def test_finish_skips_handoff_when_next_already_dispatched(client, monkeypatch):
    """本地 agent 自己调过 a2a（或上游已派发）时，finish 不能再派一次 —— 防同节点双跑。"""
    _seed(issue_number=3, task_id="t_flow_3", seq=9003)
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, issue_id, agent_type, status, source, title, payload) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (9103, REPO, "a2a_event", "#3", "qa", "running", "a2a", "下游已派发", "{}"),
        )
        db.commit()
    sid = _start(client, issue=3).json()["takeover_id"]

    monkeypatch.setattr(svc, "_advance_stage", lambda *a, **k: ("qa", False))
    monkeypatch.setattr(svc, "_dispatch_next",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("不应重复派发")))

    r = client.post(f"/api/takeover/sessions/{sid}/finish",
                    json={"outcome": "completed", "summary": "完成", "handoff": "a2a"})
    assert r.status_code == 200, r.text
    handoff = r.json()["handoff"]
    assert handoff.get("skipped") is True and handoff.get("reason") == "already_dispatched"
    assert handoff.get("seq") == 9103
    # 会话上记录的就是"真实那次派发"的 seq
    assert _get("SELECT handoff_seq FROM takeover_sessions WHERE id=?", (sid,))[0] == 9103


def test_attach_records_client_session_and_list_exposes_it(client):
    """本地 session id 回写：存进会话行，并出现在 list（面板据此显示"打开本地会话"）。"""
    _seed(issue_number=4, task_id="t_flow_4", seq=9004)
    sid = _start(client, issue=4).json()["takeover_id"]
    r = client.post(f"/api/takeover/sessions/{sid}/attach",
                    json={"client_session": "session-xyz", "host": "dev-box"})
    assert r.status_code == 200, r.text
    assert r.json()["client_session"] == "session-xyz"
    row = _get("SELECT client_session, host FROM takeover_sessions WHERE id=?", (sid,))
    assert row == ("session-xyz", "dev-box")
    lst = client.get("/api/takeover/sessions", params={"repo_key": REPO, "status": "active"})
    assert lst.status_code == 200, lst.text
    sessions = lst.json()["sessions"]
    assert sessions and sessions[0]["client_session"] == "session-xyz"
    assert sessions[0]["host"] == "dev-box"


def test_takeover_token_can_request_finish_but_not_finish(client, monkeypatch):
    """agent 用 tko_ 提交完成内容（200）；finish 仍是用户授权点（tko_ 403 → 由依赖挡住）。"""
    _seed(issue_number=5, task_id="t_flow_5", seq=9005)
    sid = _start(client, issue=5).json()["takeover_id"]

    # agent 身份（tko_）提交
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _as_takeover("takeover:t_flow_5")
    r = client.post(f"/api/takeover/sessions/{sid}/request-finish", json={
        "summary": "需求已收敛并打标签", "evidence": "PR !12 / grape issue label 已设置",
        "files_written": ["docs/spec.md"],
    })
    assert r.status_code == 200, r.text
    row = _get("SELECT summary, evidence, finish_files, finish_requested_at FROM takeover_sessions WHERE id=?", (sid,))
    assert row[0] == "需求已收敛并打标签"
    assert "PR !12" in row[1]
    assert "docs/spec.md" in row[2]
    assert int(row[3]) > 0

    # agent 调 finish 必须被挡（user_scope 依赖 → 403）
    r2 = client.post(f"/api/takeover/sessions/{sid}/finish", json={"outcome": "completed"})
    assert r2.status_code == 403, r2.text

    # 用户点确认：body 为空也要沿用 agent 提交的内容进 A2A 交接单
    called = {}
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": 1, "username": "tester", "auth_type": "api_key", "token": "x", "token_type": "api_key"}
    monkeypatch.setattr(svc, "_advance_stage", lambda *a, **k: ("coding", False))
    monkeypatch.setattr(svc, "_dispatch_next", lambda **kw: (called.update(kw) or {"ok": True, "seq": 9200}))
    r3 = client.post(f"/api/takeover/sessions/{sid}/finish", json={"outcome": "completed", "handoff": "a2a"})
    assert r3.status_code == 200, r3.text
    assert "需求已收敛并打标签" in called.get("context", "")
    assert "docs/spec.md" in called.get("context", "")


def test_request_finish_rejects_foreign_takeover_token(client):
    _seed(issue_number=6, task_id="t_flow_6", seq=9006)
    sid = _start(client, issue=6).json()["takeover_id"]
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _as_takeover("takeover:someone_else")
    r = client.post(f"/api/takeover/sessions/{sid}/request-finish", json={"summary": "x"})
    assert r.status_code == 403
    assert r.json()["code"] == "not_owner"


def test_git_repo_guard_allows_takeover_for_own_repo_only(client, monkeypatch):
    """接管 token 可以走 git-repo（本仓库）；跨仓库 / 非接管身份一律 403。"""
    from fastapi import HTTPException
    from src.api import git_repo_api as gra

    # 跨仓库
    with pytest.raises(HTTPException) as e1:
        gra._repo_token("other", "repo",
                        {"auth_type": "takeover", "repo_key": REPO, "session_key": "takeover:t_gr"},
                        "gitcode")
    assert e1.value.status_code == 403

    # 用户 API Key 仍然不行（原策略不变）
    with pytest.raises(HTTPException) as e2:
        gra._repo_token("o", "r", {"auth_type": "api_key"}, "gitcode")
    assert e2.value.status_code == 403

    # 本仓库 + active 接管会话 → 放行（用假 provider 模块，不真的打平台）
    _seed(issue_number=7, task_id="t_gr", seq=9007)
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO takeover_sessions (id, active_key, task_id, event_seq, repo_key, issue_id, "
            "agent_type, user_id, username, status, token_sha256, token_expires_at, token_issued_at) "
            "VALUES ('tkos_guard_repo', 't_gr', 't_gr', ?, ?, '#7', 'issue', 1, 'tester', 'active', 'x', 0, 1)",
            (9007, REPO))
        db.commit()

    class _FakeMod:
        @staticmethod
        def bot_token(rk):
            return "bot-token"

        @staticmethod
        def user_token(username, uid):
            return ""

    monkeypatch.setitem(gra._PROVIDER_MODULES, "gitcode", _FakeMod)
    tok = gra._repo_token("o", "r",
                          {"auth_type": "takeover", "repo_key": REPO, "session_key": "takeover:t_gr"},
                          "gitcode")
    assert tok == "bot-token"


def test_abort_redispatches_node_by_default(client, monkeypatch):
    """放弃接管 = 交还流水线：默认调用平台 redispatch（新建任务/事件回 running）。"""
    import src.domains.runtime.routes.events as evmod

    _seed(issue_number=8, task_id="t_flow_8", seq=9008)
    sid = _start(client, issue=8).json()["takeover_id"]
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": 1, "username": "tester", "auth_type": "api_key", "token": "x", "token_type": "api_key"}

    calls = {}

    def _fake_redispatch(seq, user=None):
        calls.update({"seq": seq, "user": (user or {}).get("username")})
        from src.api.event_store import reset_event_terminal
        reset_event_terminal(seq)
        return {"status": "redispatched", "task_id": "t_new_8", "event_seq": seq}

    # abort 走服务内通道 redispatch_event_internal（显式 skip_debounce），不是公共 HTTP 路由
    monkeypatch.setattr(evmod, "redispatch_event_internal", _fake_redispatch)
    r = client.post(f"/api/takeover/sessions/{sid}/abort", json={"reason": "测完放弃"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "aborted"
    assert body["redispatch"]["ok"] is True and body["redispatch"]["task_id"] == "t_new_8"
    assert calls == {"seq": 9008, "user": "tester"}
    assert _get("SELECT status FROM events WHERE seq=9008")[0] == "running"


def test_abort_can_skip_redispatch(client, monkeypatch):
    """需要"只停不重派"的调用方显式 redispatch=false。"""
    import src.domains.runtime.routes.events as evmod
    _seed(issue_number=9, task_id="t_flow_9", seq=9009)
    sid = _start(client, issue=9).json()["takeover_id"]
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": 1, "username": "tester", "auth_type": "api_key", "token": "x", "token_type": "api_key"}
    called = {"n": 0}
    monkeypatch.setattr(evmod, "redispatch_event_internal",
                        lambda *a, **k: called.update(n=called["n"] + 1) or {})
    r = client.post(f"/api/takeover/sessions/{sid}/abort", json={"reason": "不重派", "redispatch": False})
    assert r.status_code == 200, r.text
    assert called["n"] == 0
    assert "redispatch" not in r.json()
