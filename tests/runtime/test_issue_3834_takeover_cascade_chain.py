"""Issue #3834 回归：接管让渡后 cascade 补偿事件不得断掉 A2A 链。

复现形态（#60/#61）：
  start_takeover → cancel_task 把 coding 事件级联收敛为 REASON_SYNTHESIZED
  （is_agent_conclusion=false、task_status=cancelled）→ finish 交回真实结论后，
  旧实现只写 stage / structured_output，事件仍是「系统合成补偿」或（mark 失败时）
  停在 cancelled，下一节点（qa）永不派发，只能外部 trigger_pipeline 重跑整链。

验收覆盖：
  - finish 把补偿归因改判 REASON_BACKFILLED（is_agent_conclusion=true），携带 structured_output；
  - mark_task_takeover 未生效（事件停在 cancelled）时，接管 finish 仍能授权改写为 completed；
  - finish 后按场景链派发下一节点（qa），无需 trigger_pipeline；
  - 未接管的普通事件不产生补偿归因（不回归）。
"""

import json

import pytest
from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.api.main import app
from src.domains.runtime.engine import cascade

REPO = "gitcode:o/r3834"


@pytest.fixture(autouse=True)
def _bind_db():
    """绑定被测 takeover 模块实际使用的连接（与 test_takeover_flow 同口径）。"""
    global db, db_lock
    from src.domains.runtime.engine import takeover as _svc
    db, db_lock = _svc.db, _svc.db_lock


from src.domains.runtime.engine import takeover as svc  # noqa: E402
from src.kernel.session_manager import db, db_lock  # noqa: E402


def _cleanup():
    with db_lock:
        c = db.cursor()
        for sql, args in (
            ("DELETE FROM takeover_sessions WHERE repo_key=?", (REPO,)),
            ("DELETE FROM agent_tasks WHERE repo_key=?", (REPO,)),
            ("DELETE FROM events WHERE repo_key=?", (REPO,)),
            ("DELETE FROM issue WHERE repo_key=?", (REPO,)),
            ("DELETE FROM session_api_tokens WHERE session_key LIKE 'takeover:t_3834_%'", ()),
            ("DELETE FROM task_messages WHERE task_id LIKE 't_3834_%'", ()),
        ):
            try:
                c.execute(sql, args)
            except Exception:
                pass
        db.commit()


def _seed(issue_number: int = 3834, task_id: str = "t_3834_1", seq: int = 93834,
          event_status: str = "running"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, repo_key, issue_number, title, status, stage, owner_agent, scene_id, assignee) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (1, REPO, issue_number, "接管断链回归", "processing", "coding", "coding", "default", ""),
        )
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, issue_id, agent_type, status, source, title, payload) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (seq, REPO, "a2a_event", f"#{issue_number}", "coding", event_status,
             "a2a", "t", "{}"),
        )
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, status, agent_type, skill, prompt, env_extra, cwd, "
            "runtime_id, event_seq, repo_key, issue_id, user_id, account, skill_files) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, f"ses_{issue_number}", "running", "coding", "implementation", "实现",
             "{}", f"/tmp/issue-3834/{issue_number}/coding", 0, seq, REPO,
             f"#{issue_number}", 1, "", "{}"),
        )
        db.commit()


def _get(sql, args=()):
    with db_lock:
        c = db.cursor()
        c.execute(sql, args)
        return c.fetchone()


def _user():
    return {"id": 1, "username": "tester", "auth_type": "api_key",
            "token": "x", "token_type": "api_key"}


@pytest.fixture()
def client():
    _cleanup()
    app.dependency_overrides[get_current_user_or_api_key] = _user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        _cleanup()


def _start(client, issue=3834):
    r = client.post("/api/takeover/sessions", json={
        "repo_key": REPO, "issue_number": str(issue), "platform": "dsh", "host": "test-host",
        "client_version": "0.4.0-test", "supports": {"workspace": True, "progress": True},
    })
    assert r.status_code == 200, r.text
    return r.json()["takeover_id"]


def _mark_cascade_synthesized(seq: int):
    """模拟 0.3.8 实例让渡时的结构化补偿归因（合成、非 Agent 结论）。"""
    text = cascade.build_cascade_result(
        task_status="cancelled", prev_event_status="running",
        triggered_by=cascade.TRIGGER_UPDATE_TASK_STATUS)
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE events SET result=?, cascade_reason=?, report_missing=1, "
            "triggered_by=?, cascade_task_status='cancelled', cascade_prev_event_status='running' "
            "WHERE seq=?",
            (text, cascade.REASON_SYNTHESIZED, cascade.TRIGGER_UPDATE_TASK_STATUS, seq))
        db.commit()


def _fake_dispatch(monkeypatch):
    called = {}

    def _dispatch(**kw):
        called.update(kw)
        return {"ok": True, "seq": 93999}

    monkeypatch.setattr(svc, "_advance_stage", lambda *a, **k: ("qa", False))
    monkeypatch.setattr(svc, "_dispatch_next", _dispatch)
    return called


def _patch_scene(monkeypatch, nodes):
    """替换场景配置，走真实的 _advance_stage / _next_agent_from_scene 解析。"""
    import src.api.pipeline_config as pc
    monkeypatch.setattr(pc, "get_pipeline_config", lambda *a, **k: {"nodes": nodes})
    monkeypatch.setattr(pc, "get_next_node", lambda *a, **k: None)


def _fake_dispatch_only(monkeypatch):
    called = {}

    def _dispatch(**kw):
        called.update(kw)
        return {"ok": True, "seq": 93999}

    monkeypatch.setattr(svc, "_dispatch_next", _dispatch)
    return called


def test_finish_backfills_cascade_event_and_dispatches_qa(client, monkeypatch):
    """让渡补偿 → finish 后事件改判 backfilled、携带 structured_output、QA 自动派发。"""
    _seed()
    sid = _start(client)
    seq = 93834
    assert _get("SELECT status FROM events WHERE seq=?", (seq,))[0] == "takeover"
    _mark_cascade_synthesized(seq)

    called = _fake_dispatch(monkeypatch)
    r = client.post(f"/api/takeover/sessions/{sid}/finish", json={
        "outcome": "completed",
        "summary": "coding 节点已交付，鉴权模块重构完成",
        "evidence": "pytest tests/auth -q 通过",
        "files_written": ["src/auth.py"],
        "actions": ["重构鉴权模块", "补回归用例"],
        "artifacts": ["PR #1"],
        "handoff": "a2a",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["handoff"]["ok"] is True

    row = _get(
        "SELECT status, result, cascade_reason, report_missing, triggered_by, "
        "cascade_prev_event_status, structured_output FROM events WHERE seq=?", (seq,))
    assert row[0] == "completed"
    assert row[1].startswith("coding 节点已交付")
    assert row[2] == cascade.REASON_BACKFILLED, "接管 finish 交回真实结论须改判为已回填"
    assert int(row[3]) == 1
    assert row[4] == svc.TRIGGER_TAKEOVER_FINISH
    # 复审④：原始现场字段保留 cancel 收敛时记录的值，不被改判覆盖
    assert row[5] == "running"
    so = json.loads(row[6])
    assert so["next_step"] == "qa"
    assert so["actions"] == ["重构鉴权模块", "补回归用例"]

    meta = cascade.display_meta(
        cascade_reason=row[2], report_missing=row[3], triggered_by=row[4],
        task_status="cancelled", prev_event_status=row[5], result_text=row[1])
    assert meta["is_agent_conclusion"] is True, "回填真实结论的事件须具备触发下一节点资格"

    # QA 派发：目标节点 / issue / 交接内容完整
    assert called.get("next_agent") == "qa"
    assert called.get("issue_number") == "3834"
    assert "coding 节点已交付" in called.get("context", "")
    assert _get("SELECT handoff_seq FROM takeover_sessions WHERE id=?", (sid,))[0] == 93999


def test_finish_revives_cancelled_event_when_mark_not_applied(client, monkeypatch):
    """mark_task_takeover 未生效（事件停在 cancelled）时，finish 仍能授权完成并派发。"""
    _seed()
    sid = _start(client)
    seq = 93834
    # 模拟旧实例/竞态：事件被 cancel 级联成 cancelled，且没有回到 takeover。
    _mark_cascade_synthesized(seq)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE events SET status='cancelled', completed_at='2026-11-23T00:00:00+00:00' "
                  "WHERE seq=?", (seq,))
        db.commit()

    called = _fake_dispatch(monkeypatch)
    r = client.post(f"/api/takeover/sessions/{sid}/finish", json={
        "outcome": "completed", "summary": "已交付", "handoff": "a2a",
    })
    assert r.status_code == 200, r.text
    row = _get("SELECT status, cascade_reason, structured_output FROM events WHERE seq=?", (seq,))
    assert row[0] == "completed", "接管 finish 是授权终态写入，cancelled 补偿事件须可改判"
    assert row[1] == cascade.REASON_BACKFILLED
    assert row[2], "structured_output 必须写入"
    assert called.get("next_agent") == "qa"


def test_plain_takeover_event_not_marked_as_compensation():
    """非补偿事件（普通接管）不写补偿归因，保持普通 Agent 结论口径。"""
    changed = svc.mark_backfilled_conclusion(
        93834, prev={"status": "takeover", "result": "正常进行中，无补偿", "cascade_reason": ""})
    assert changed is False, "非补偿事件不得被改判为回填补偿"
    meta = cascade.display_meta(result_text="正常进行中，无补偿")
    assert meta["system_cascade"] is False
    assert meta["is_agent_conclusion"] is True


def test_finish_falls_back_to_scene_chain_when_node_id_mismatch(client, monkeypatch):
    """id 对不上但 tag 命中：_advance_stage 解析不到时，真实兜底节点链应解析出下一节点。"""
    _seed()
    sid = _start(client)
    _patch_scene(monkeypatch, [
        {"id": "coding-node", "tag": "coding", "next": "qa"},
        {"id": "qa", "tag": "qa", "next": "done"},
    ])
    called = _fake_dispatch_only(monkeypatch)
    r = client.post(f"/api/takeover/sessions/{sid}/finish", json={
        "outcome": "completed", "summary": "兜底解析交付", "handoff": "a2a",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert called.get("next_agent") == "qa"
    assert body["next_agent"] == "qa"
    row = _get("SELECT stage, owner_agent FROM issue WHERE repo_key=? AND issue_number=?",
               (REPO, 3834))
    assert row[0] == "qa" and row[1] == "qa", "stage 与派发目标须同为兜底解析值"


def test_finish_marks_issue_done_when_fallback_hits_terminal_node(client, monkeypatch):
    """仅靠 tag 命中且 next=done 的末节点：兜底须落 stage=done、Issue 完成、不再派发。"""
    _seed()
    sid = _start(client)
    _patch_scene(monkeypatch, [{"id": "coding-node", "tag": "coding", "next": "done"}])
    called = _fake_dispatch_only(monkeypatch)
    r = client.post(f"/api/takeover/sessions/{sid}/finish", json={
        "outcome": "completed", "summary": "末节点交付", "handoff": "a2a",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["issue_stage"] == "done"
    assert body["handoff"].get("skipped") is True
    assert not called, "末节点不应再派发下一节点"
    row = _get("SELECT status, stage FROM issue WHERE repo_key=? AND issue_number=?",
               (REPO, 3834))
    assert row[0] == "completed" and row[1] == "done", "仅靠 tag 命中的末节点须落 done"


def test_finish_reports_unresolved_when_no_next_agent(client, monkeypatch):
    """场景链与请求都无目标时，finish 须显式返回 next_agent_unresolved 而非静默成功。"""
    _seed()
    sid = _start(client)
    _patch_scene(monkeypatch, [{"id": "coding", "tag": "coding", "next": ""}])
    called = _fake_dispatch_only(monkeypatch)
    r = client.post(f"/api/takeover/sessions/{sid}/finish", json={
        "outcome": "completed", "summary": "无下一节点", "handoff": "a2a",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["handoff"]["ok"] is False
    assert body["handoff"].get("reason") == "next_agent_unresolved"
    assert not called, "无目标时不得派发"

# ───────────────────────── #3834 复审补充：真实链路与边界对称 ─────────────────────────


def test_finish_dispatches_via_real_http_chain(client, monkeypatch):
    """复审②：不 mock _dispatch_next——本地起真实 HTTP 端点承接 /api/a2a，
    验证 finish → _advance_stage → _dispatch_next 全链真实走到 HTTP 层，
    且载荷（agent=qa、issue_number、context 含接管交回摘要）正确。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            received["body"] = json.loads(self.rfile.read(length) or b"{}")
            received["path"] = self.path
            received["auth"] = self.headers.get("authorization", "")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"seq": 93998}')

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _seed()
        sid = _start(client)
        seq = 93834
        _mark_cascade_synthesized(seq)

        # 场景走真实解析（coding→qa），仅隔离内部基址指向本地端点
        _patch_scene(monkeypatch, [{"id": "coding", "tag": "coding", "next": "qa"}])
        monkeypatch.setattr(svc, "_internal_base_url", lambda *a, **k: f"http://127.0.0.1:{port}")
        # 派发优先按 A2A 服务映射取址（_a2a_dispatch_base）：这里把映射也指向本地端点，
        # 否则部署配置里的 server_base_url 会先命中，真实 HTTP 链走不到本测试服务器。
        monkeypatch.setattr("src.kernel.prompt_builder.resolve_a2a_base",
                            lambda *a, **k: f"http://127.0.0.1:{port}")

        r = client.post(f"/api/takeover/sessions/{sid}/finish", json={
            "outcome": "completed",
            "summary": "真实链路验证：本地 HTTP 端点承接派发",
            "handoff": "a2a",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["backfilled"] is True
        assert body["handoff"]["ok"] is True
        assert body["handoff"]["seq"] == 93998
        assert body["issue_stage"] == "qa"

        # 真实 HTTP 载荷断言
        assert received["path"] == "/api/a2a"
        assert received["body"]["agent"] == "qa"
        assert received["body"]["payload"]["issue_number"] == "3834"
        assert "真实链路验证" in received["body"]["payload"]["context"]
    finally:
        server.shutdown()
        server.server_close()


def test_finish_failed_outcome_revives_cancelled_event(client, monkeypatch):
    """复审③：outcome=failed 对称——事件停在 cancelled（让渡补偿）时，
    failed 终态写入放行，库里口径与接口一致，不再留「cancelled+合成文案」。"""
    _seed()
    sid = _start(client)
    seq = 93834
    _mark_cascade_synthesized(seq)

    r = client.post(f"/api/takeover/sessions/{sid}/finish", json={
        "outcome": "failed",
        "summary": "本地开发环境缺失，节点失败",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_status"] == "failed"

    row = _get("SELECT status, cascade_reason FROM events WHERE seq=?", (seq,))
    assert row[0] == "failed", "cancelled 事件须被授权改写为 failed（口径一致）"
    assert row[1] == cascade.REASON_SYNTHESIZED, "failed 不改判归因（非成功交付）"


def test_backfill_preserves_original_cascade_fields(client, monkeypatch):
    """复审④：改判只动 cascade_reason/triggered_by/report_missing；
    cascade_task_status / cascade_prev_event_status 保留 cancel 收敛时的原始现场。"""
    _seed()
    sid = _start(client)
    seq = 93834
    _mark_cascade_synthesized(seq)  # 原始现场：task_status=cancelled, prev=running

    called = _fake_dispatch(monkeypatch)
    r = client.post(f"/api/takeover/sessions/{sid}/finish", json={
        "outcome": "completed", "summary": "归因保留验证", "handoff": "a2a",
    })
    assert r.status_code == 200, r.text

    row = _get("SELECT cascade_reason, cascade_task_status, cascade_prev_event_status, "
               "triggered_by FROM events WHERE seq=?", (seq,))
    assert row[0] == cascade.REASON_BACKFILLED
    assert row[1] == "cancelled", "cancel 收敛记录的 task_status 不得被覆盖"
    assert row[2] == "running", "cancel 收敛记录的 prev_event_status 不得被覆盖"
    assert row[3] == svc.TRIGGER_TAKEOVER_FINISH


def test_scene_fallback_rejects_unknown_next_agent(client, monkeypatch):
    """复审⑤：场景 next 指向不可派发的字符串时按未解析处理，不派发、不静默。"""
    _seed()
    sid = _start(client)
    seq = 93834

    # id 非标准但 tag 命中兜底；next 写了个不存在的角色
    _patch_scene(monkeypatch, [{"id": "coding-node", "tag": "coding",
                                "next": "nonexistent-role"}])
    called = _fake_dispatch_only(monkeypatch)

    r = client.post(f"/api/takeover/sessions/{sid}/finish", json={
        "outcome": "completed", "summary": "非法 next 验证", "handoff": "a2a",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # 未解析 → 显式落账，不派发
    assert body["next_agent"] in ("", None)
    assert body["handoff"].get("reason") == "next_agent_unresolved"
    assert body["handoff"].get("ok") is False


def test_finish_reports_backfilled_false_for_plain_event(client, monkeypatch):
    """复审⑥：普通接管（非补偿事件）finish 返回 backfilled=False，可观测区分。

    注：start_takeover 的 cancel 链路本身会把事件收敛成合成补偿（这正是 #3834 场景），
    所以"普通事件"须手工清除补偿标记，模拟老实例 mark_task_takeover 已生效、无补偿归因的接管。
    """
    _seed()
    sid = _start(client)
    seq = 93834
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE events SET result='', cascade_reason='', report_missing=0, "
            "triggered_by='', cascade_task_status='', cascade_prev_event_status='' "
            "WHERE seq=?", (seq,))
        db.commit()

    called = _fake_dispatch(monkeypatch)
    r = client.post(f"/api/takeover/sessions/{sid}/finish", json={
        "outcome": "completed", "summary": "普通接管", "handoff": "a2a",
    })
    assert r.status_code == 200, r.text
    assert r.json()["backfilled"] is False, "非补偿事件无需改判"
