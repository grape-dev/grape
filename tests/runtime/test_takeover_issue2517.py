"""Issue #2517 修复回归测试（被测代码在 src/domains/runtime/engine/takeover.py）：

A. abort 的内部重派通道绕过 30s 幂等门（公共 API 防抖不变）
B. abort 重派结果顶层披露（成功 redispatched=True / 失败 redispatched=False + redispatch_error）
C. start 竞态检查：同 issue 存在更新 a2a_event → 放行并披露（warnings 追加稳定 code
   node_already_advanced、warning_details 给出最新 seq/agent_type），不拒接；
   存在更新非 a2a 事件（如 admin_action）→ 不告警也不误拦。

另一侧契约来源：本用例按 tests/README.md「跨域场景用例归到主被测域，并在文件 docstring
写明另一侧的契约来源」执行，经 /api/takeover/* 驱动，断言的是 mgmt 侧
src/domains/mgmt/routes/takeover.py 的顶层 warnings / warning_details 回执契约
（start 回执字段定义见该文件 start_takeover）。
"""

import time

import pytest
from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.api.main import app
from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import takeover as svc

REPO = "gitcode:o/i2517"
USER = {"id": 1, "username": "tester", "auth_type": "api_key", "token": "x", "token_type": "api_key"}


def _cleanup():
    with db_lock:
        c = db.cursor()
        for sql, args in (
            ("DELETE FROM takeover_sessions WHERE repo_key=?", (REPO,)),
            ("DELETE FROM agent_tasks WHERE repo_key=?", (REPO,)),
            ("DELETE FROM events WHERE repo_key=?", (REPO,)),
            ("DELETE FROM issue WHERE repo_key=?", (REPO,)),
            ("DELETE FROM session_api_tokens WHERE session_key LIKE 'takeover:%'", ()),
            ("DELETE FROM task_messages WHERE task_id LIKE 't_i2517_%'", ()),
            # 30s 幂等键也逐用例清：不清会把「刚重派过」带到后续用例（域内同文件按顺序执行）
            ("DELETE FROM meta WHERE key LIKE 'redispatch:%'", ()),
        ):
            try:
                c.execute(sql, args)
            except Exception:
                pass
        db.commit()


def _seed(issue_number=1, task_id="t_i2517_1", seq=995201):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, repo_key, issue_number, title, status, stage, owner_agent, scene_id, assignee) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (1, REPO, issue_number, "i2517 回归", "processing", "coding", "coding", "default", ""),
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
            (task_id, f"ses_{issue_number}", "running", "coding", "implementation", "i2517", "{}",
             f"/tmp/i2517/{issue_number}/coding", 0, seq, REPO, f"#{issue_number}", 1, "", "{}"),
        )
        db.commit()


def _get(sql, args=()):
    with db_lock:
        c = db.cursor()
        c.execute(sql, args)
        return c.fetchone()


@pytest.fixture()
def client():
    _cleanup()
    app.dependency_overrides[get_current_user_or_api_key] = lambda: USER
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        _cleanup()


def _start(client, issue=1):
    return client.post("/api/takeover/sessions", json={
        "repo_key": REPO, "issue_number": str(issue), "platform": "grape-kit",
        "host": "test-host", "client_version": "0.3.2-test",
    })


# ---------------------------------------------------------------- A. 内部通道绕防抖

def test_public_channel_still_guarded_and_internal_bypasses(client):
    """同一 seq 的 30s 幂等键刚写入：公共通道 409（含伪造 skip_debounce），内部通道真重派成功。"""
    from src.domains.runtime.routes import events as events_mod
    from src.domains.mgmt.user.db import set_meta_value

    _seed()
    set_meta_value("redispatch:995201", str(time.time()))  # 刚重派过（<30s）

    # 公共通道（HTTP）：防抖仍生效 —— 用户级 API 行为不变。
    # 特权位是服务内函数参数，不在路由签名里：query 里塞 skip_debounce 一律无效（#2678 观察 6）
    for qs in ("", "?skip_debounce=true", "?skip_debounce=1"):
        r = client.post(f"/api/events/995201/redispatch{qs}")
        assert r.status_code == 409, (qs, r.text)
        assert "已重新派发过" in r.text

    # 内部通道：显式跳过防抖后必须真正重派成功 —— 回执字段逐个断言，
    # 不再用「except Exception 只断言文案」的写法（整改前该写法任何其它失败同样通过）
    res = events_mod.redispatch_event_internal(995201, USER)
    assert res["event_seq"] == 995201
    assert res["status"] == "redispatched"
    assert res["task_id"] == "t_i2517_1"


# ---------------------------------------------------------------- B. abort 顶层披露

def test_abort_happy_path_discloses_redispatched(client, monkeypatch):
    from src.domains.runtime.routes import events as events_mod
    _seed()
    sid = _start(client).json()["takeover_id"]

    monkeypatch.setattr(
        events_mod, "redispatch_event_internal",
        lambda seq, user: {"status": "redispatched", "task_id": "t_new_1", "event_seq": seq})

    res = svc.abort_takeover(session_id=sid, user=USER, reason="回归-成功路径")
    assert res["ok"] and res["status"] == "aborted"
    assert res["redispatched"] is True
    assert res["redispatch_status"] == "redispatched"
    assert res["redispatch"]["task_id"] == "t_new_1"


def test_abort_redispatch_failure_disclosed_top_level(client, monkeypatch):
    from fastapi import HTTPException
    from src.domains.runtime.routes import events as events_mod
    _seed()
    sid = _start(client).json()["takeover_id"]

    def _boom(seq, user):
        raise HTTPException(status_code=409, detail={"code": 409, "message": "已重新派发过，请 30s 后再试"})

    monkeypatch.setattr(events_mod, "redispatch_event_internal", _boom)

    res = svc.abort_takeover(session_id=sid, user=USER, reason="回归-失败路径")
    assert res["ok"] and res["status"] == "aborted"
    # 顶层披露：调用方不挖嵌套也能发现链未恢复
    assert res["redispatched"] is False
    assert res["redispatch_status"] == "failed"
    assert "redispatch_error" in res and "409" in res["redispatch_error"]


def test_abort_no_redispatch_reports_false(client):
    _seed()
    sid = _start(client).json()["takeover_id"]
    res = svc.abort_takeover(session_id=sid, user=USER, reason="只停不重派", redispatch=False)
    assert res["redispatched"] is False
    # 四态披露（#2678 复审观察 5）：redispatched=False 与「未请求重派」原本同值，端侧无法区分
    assert res["redispatch_status"] == "not_requested"
    assert "redispatch_error" not in res


def test_abort_redispatch_without_event_reports_no_event(client):
    """请求了重派但会话没有 event_seq（:1674 整块跳过）→ 必须与「重派失败」可区分。"""
    _seed(seq=0)
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        db.commit()
    sid = _start(client).json()["takeover_id"]
    res = svc.abort_takeover(session_id=sid, user=USER, reason="无事件可重派")
    assert res["ok"] and res["status"] == "aborted"
    assert res["redispatched"] is False
    assert res["redispatch_status"] == "no_event"
    assert "redispatch_error" not in res


# ---------------------------------------------------------------- C. start 竞态检查

def test_start_warns_when_node_advanced(client):
    """同 issue 存在更新的 a2a_event（链已交棒）→ 放行（平台 already_dispatched 语义）
    但 bundle.warnings 必须披露 node_already_advanced（#2517 补充发现 #2）。"""
    _seed()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, issue_id, agent_type, status, source, title, payload) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (995202, REPO, "a2a_event", "#1", "qa", "running", "a2a", "t", "{}"),
        )
        db.commit()
    r = _start(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"]
    warnings = body.get("warnings") or []
    assert any("node_already_advanced" in str(w) for w in warnings), warnings
    # warning 契约（#2678 复审观察 1）：元素是纯稳定 code，端侧按 code 映射文案；
    # seq/agent_type 落结构化字段，不混进 code 串里
    assert "node_already_advanced" in warnings, warnings
    assert body.get("warning_details", {}).get("node_already_advanced") == {
        "seq": 995202, "agent_type": "qa"}, body.get("warning_details")
    # 会话持久化里也带该警告（重连/面板可见）
    import json as _j
    wj, bj = _get("SELECT warnings_json, bundle_json FROM takeover_sessions WHERE id=?",
                  (body["takeover_id"],))
    assert "node_already_advanced" in (wj or "")
    assert _j.loads(bj or "{}").get("warning_details", {}).get("node_already_advanced", {}).get("seq") == 995202


def test_start_detects_advance_landing_during_cancel(client, monkeypatch):
    """竞态探测必须覆盖 cancel 之后的窗口（#2678 复审 Spec 可疑 1）。

    复现 trace：#19 里下游 a2a 是在 cancel 之后 564ms 才入库的。这里用 cancel_task 钩子
    把「晚到的 a2a 事件」塞进真实时序（cancel 期间入库），断言 start 回执仍披露
    node_already_advanced —— 探测若留在 cancel 之前，该窗口永远检不出。
    """
    from src.domains.runtime.engine import service as rt

    _seed()
    real_cancel = rt.cancel_task

    def _cancel_then_advance(task_id):
        out = real_cancel(task_id)
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO events (seq, repo_key, event_type, issue_id, agent_type, status, source, title, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (995302, REPO, "a2a_event", "#1", "qa", "running", "a2a", "t", "{}"),
            )
            db.commit()
        return out

    monkeypatch.setattr(rt, "cancel_task", _cancel_then_advance)
    r = _start(client)
    assert r.status_code == 200, r.text
    warnings = r.json().get("warnings") or []
    assert any("node_already_advanced" in str(w) for w in warnings), warnings


def test_reconnect_keeps_node_already_advanced_warning(client):
    """重连（复用会话）后披露不得消失（#2678 复审 Spec 可疑 2）。

    _rebuild_for_session 用 build_bundle 新产出的 warnings 覆盖 warnings_json，
    reused 分支也不带该警告 —— 整改前重连即丢失，与 PR 正文「含持久化 warnings_json」不符。
    """
    _seed()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, issue_id, agent_type, status, source, title, payload) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (995202, REPO, "a2a_event", "#1", "qa", "running", "a2a", "t", "{}"),
        )
        db.commit()

    first = _start(client).json()
    assert first["reused"] is False
    assert any("node_already_advanced" in str(w) for w in first.get("warnings") or [])

    again = _start(client).json()
    assert again["reused"] is True
    assert any("node_already_advanced" in str(w) for w in again.get("warnings") or []), again.get("warnings")
    wj, bj = _get("SELECT warnings_json, bundle_json FROM takeover_sessions WHERE id=?",
                  (again["takeover_id"],))
    assert "node_already_advanced" in (wj or "")
    assert "node_already_advanced" in (bj or "")


def test_node_advanced_detail_picks_newest_downstream_event(client):
    """多条更新 a2a_event 时披露最新一条（#2678 复审观察 4：LIMIT 1 无 ORDER BY）。

    无 ORDER BY 时返回行不确定（sqlite 按扫描顺序给最早那条），披露的 seq/agent_type
    与「链现在在哪个节点」对不上。
    """
    _seed()
    with db_lock:
        c = db.cursor()
        for _seq, _agent in ((995202, "qa"), (995204, "sre")):
            c.execute(
                "INSERT INTO events (seq, repo_key, event_type, issue_id, agent_type, status, source, title, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (_seq, REPO, "a2a_event", "#1", _agent, "running", "a2a", "t", "{}"),
            )
        db.commit()
    body = _start(client).json()
    assert body.get("warning_details", {}).get("node_already_advanced") == {
        "seq": 995204, "agent_type": "sre"}, body.get("warning_details")


def test_start_allows_when_only_non_a2a_newer_event(client):
    """更新的非 a2a 事件（如 admin_action）不触发竞态告警——避免误拦正常接管。"""
    _seed()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, issue_id, agent_type, status, source, title, payload) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (995203, REPO, "admin_action", "#1", "", "", "admin", "t", "{}"),
        )
        db.commit()
    r = _start(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and not body.get("reused")
    # 控制例必须断言「不含」该 code：删掉查询里的 event_type='a2a_event' 过滤同样会通过，
    # 否则证明不了「非 a2a 更新事件不误触发」（#2678 复审 Spec 可疑 3）
    assert "node_already_advanced" not in (body.get("warnings") or []), body.get("warnings")
    assert "node_already_advanced" not in (body.get("warning_details") or {}), body.get("warning_details")


# ---------------------------------------------------------------- D. 端到端（AC1 / AC4）

def test_abort_end_to_end_restores_chain_despite_debounce(client, monkeypatch):
    """#2517 AC1 端到端：预置 redispatch:{seq}（<30s）后走真实 abort → 链仍被恢复。

    整改前这条路径被 30s 幂等门拦成 409，abort 回执照样报成功、事件停在 cancelled
    （AC1 此前只由直调内部函数证明，缺真实端到端）。同时验 AC4：会话置 aborted、
    接管 token 已吊销（可人工 redispatch 恢复）。
    """
    from src.domains.mgmt.user.db import set_meta_value
    from src.kernel.auth_sdk.auth_token import verify_takeover_token

    _seed()
    start = _start(client).json()
    sid = start["takeover_id"]
    tok = start["bundle"]["env"]["GRAPE_SESSION_TOKEN"]
    assert verify_takeover_token(tok), "start 应签发一把可用的接管 token"

    # 引擎可用性是环境态（同域其它用例会改 agent_runtimes / plugin 模式）：本用例要证的是
    # 「abort 的内部重派没被 30s 幂等门挡住」，不是引擎选择，因此只钉住选机这一步
    from src.domains.runtime.engine import engine_select
    monkeypatch.setattr(engine_select, "pick_engine", lambda *a, **k: 77)

    set_meta_value("redispatch:995201", str(time.time()))  # 原缺陷触发条件：30s 幂等键刚写过

    r = client.post(f"/api/takeover/sessions/{sid}/abort",
                    json={"reason": "E2E-让渡", "redispatch": True})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] and res["status"] == "aborted"
    assert res["redispatched"] is True
    assert res["redispatch_status"] == "redispatched"
    # AC4：token 已吊销、会话已结束
    assert verify_takeover_token(tok) is None
    assert _get("SELECT status FROM takeover_sessions WHERE id=?", (sid,))[0] == "aborted"
