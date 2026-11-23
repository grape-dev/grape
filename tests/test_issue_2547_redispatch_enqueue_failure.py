"""Issue #2547 回归测试：redispatch 终态重建分支 enqueue_task 失败不吞 + 回执真实 task_id。

Spec（Issue #2547 评论 200769100）三段式验收：
- 失败不吞：enqueue_task 抛 ValueError（路由缺失/空池守卫）→ 409；其它异常 → 500；
  事件 result 落可读失败原因 + meta redispatch:error:{seq}；
  reset_event_terminal 不被调用（事件保持原终态，无悬挂 running）。
- 回执真实：成功回执 = agent_tasks 真实 task_id，回执中不再出现 "(new)" 占位；
  入队返回空 / 假 id（后置校验不通过）→ 500 + 事件失败原因。
- local_only 同机重试分支同款失败语义。
- 占位清理：onboarding / PR 订阅 / A2A / run_pipeline 分支回执 task_id=None + note。
- 审计一致：成功记 redispatch（带真实 task_id），失败记 redispatch_failed。

Seam：直接调用路由函数 redispatch_event(seq, local_only, user)（公开接口），
内存 SQLite + monkeypatch 模块属性构造 enqueue 桩——与 test_issue_521 同款。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

import pytest

RK = "gitcode:test/i2547r"


@pytest.fixture()
def setup_db(redispatch_mem_db):
    """共享脚手架别名（DDL/接线单一来源见 tests/conftest.py redispatch_mem_db，
    Issue #2547 QA 复审收敛——原 50 行同款 DDL 与 monkeypatch 接线提取）。"""
    yield redispatch_mem_db


def _mk_engine(c, rid, name, tag, cred=63):
    import datetime as _dt
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, installed, credential_id, "
              "last_seen_at, busy_slots, max_concurrent, probe_status) VALUES (?,?, 'online', 1, 1, ?, ?, 0, 4, 'ok')",
              (rid, name, cred, _dt.datetime.now(_dt.timezone.utc).isoformat()))
    c.execute("INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, enabled) VALUES (?,?,4,1)",
              (rid, tag))
    c.execute("INSERT INTO runtime_credentials (id, scope) VALUES (?, 'global')", (cred,))


def _mk_task(c, tid, status, runtime_id=0, seq=9401, prompt="do work", agent="coding"):
    c.execute(
        "INSERT INTO agent_tasks (id, status, runtime_id, "
        "agent_session_id, issue_session_id, created_at, agent_type, event_seq, repo_key, "
        "issue_id, prompt, skill, event_type, account) "
        "VALUES (?,?,?, 's1', 'is', datetime('now'), ?, ?, ?, '5', ?, 'skillx', 'push', 'tester')",
        (tid, status, runtime_id, agent, seq, RK, prompt))


def _seed_event(c, seq, status="failed", result="旧失败", event_type="push"):
    c.execute(
        "INSERT INTO events (seq, repo_key, event_type, source, issue_id, title, summary, actor, "
        "payload, agent_type, runtime, skill, status, agent_logs, result, created_at, started_at) "
        "VALUES (?,?,?,'webhook','#7','t','s','tester','{}','coding','opencode','skillx',?, '[]', ?, "
        "datetime('now'), datetime('now'))",
        (seq, RK, event_type, status, result))


def _get_event_row(c, seq):
    return c.execute("SELECT status, result FROM events WHERE seq=?", (seq,)).fetchone()


def _common_patches(monkeypatch, plugin, enqueue_mock, seq, event_type="push",
                    issue_id="5", scene_id=""):
    """与 test_issue_521 同款 seam 桩；meta 用 dict 记录以断言 redispatch:error 写入。"""
    from unittest.mock import MagicMock
    from src.domains.runtime.routes import events as ev
    from src.domains.runtime.pipeline import event_pipeline as epl
    from src.api import event_store as est
    from src.api import deps as _deps
    from src.domains.mgmt.user import db as udb
    from src.domains.runtime.engine import service as svc
    import src.api.session_store as sst
    from src.domains.runtime.engine import engine_select as esel

    monkeypatch.setattr(ev, "_load_event", lambda s: {
        "seq": s, "repo_key": RK, "issue_id": issue_id, "event_type": event_type,
        "status": "failed", "payload": "{}", "source": "gitcode", "actor": "tester",
        "agent_type": "coding", "scene_id": scene_id})
    monkeypatch.setattr(_deps, "check_repo_access", lambda *a, **k: None)
    _meta = {}
    monkeypatch.setattr(udb, "get_meta_value", lambda k: _meta.get(k, ""))
    monkeypatch.setattr(udb, "set_meta_value", lambda k, v: _meta.__setitem__(k, v))
    monkeypatch.setattr(epl, "is_plugin_mode", lambda: plugin)
    _reset = MagicMock()
    monkeypatch.setattr(est, "reset_event_terminal", _reset)
    monkeypatch.setattr(epl, "build_task_env", lambda **k: {})
    monkeypatch.setattr(epl, "get_agent_workdir", lambda *a, **k: "/tmp/w")
    monkeypatch.setattr(svc, "enqueue_task", enqueue_mock)
    monkeypatch.setattr(svc, "requeue_task", MagicMock())
    monkeypatch.setattr(svc, "_remote_runtime_ready", lambda rid: True)
    monkeypatch.setattr(sst, "get_issue_scene", lambda *a, **k: ("", 0))
    monkeypatch.setattr(esel, "pick_engine", lambda *a, **k: 501)
    import src.domains.mgmt.user.org_db as _orgdb
    monkeypatch.setattr(_orgdb, "get_org_id_by_repo_key", lambda rk: 0)
    return ev, svc, _reset, _meta


# ===== 切片 1：失败不吞——终态重建分支 =====

def test_terminal_rebuild_enqueue_valueerror_409(setup_db, monkeypatch):
    """enqueue_task 抛 ValueError（守卫拒绝）→ 409 + 事件留可读失败原因 + 不重置终态。"""
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    c = setup_db
    _mk_engine(c, 501, "codex-t", tag="coding")
    _mk_task(c, "t_old1", "failed", runtime_id=501, seq=9401)
    _seed_event(c, 9401)
    setup_db.commit()
    _boom = MagicMock(side_effect=ValueError(
        "runtime_id or pool_agent_type is required (确定路由 D3)"))
    ev, svc, _reset, _meta = _common_patches(monkeypatch, plugin=False,
                                             enqueue_mock=_boom, seq=9401)

    with pytest.raises(HTTPException) as ei:
        ev.redispatch_event(9401, user={"username": "admin"})

    assert ei.value.status_code == 409, "可判定失败（守卫拒绝）必须显式 409，不得回 200"
    assert "重派入队失败" in str(ei.value.detail), f"回执须带可读失败原因，实际 {ei.value.detail}"
    svc.enqueue_task.assert_called_once()
    _reset.assert_not_called(), "入队失败时不得 reset 事件终态（事件保持 failed 语义）"
    st, result = _get_event_row(c, 9401)
    assert st == "failed"
    assert "重派入队失败" in (result or ""), f"事件 result 须留失败原因，实际 {result!r}"
    assert _meta.get(f"redispatch:error:9401"), "失败原因须写 meta redispatch:error:{seq} 供排障检索"


def test_terminal_rebuild_enqueue_exception_500(setup_db, monkeypatch):
    """enqueue_task 抛非 ValueError 异常（DB 写失败等）→ 500 + 事件留可读失败原因。"""
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    c = setup_db
    _mk_engine(c, 501, "codex-t", tag="coding")
    _mk_task(c, "t_old2", "failed", runtime_id=501, seq=9402)
    _seed_event(c, 9402)
    setup_db.commit()
    _boom = MagicMock(side_effect=RuntimeError("db write boom"))
    ev, svc, _reset, _meta = _common_patches(monkeypatch, plugin=False,
                                             enqueue_mock=_boom, seq=9402)

    with pytest.raises(HTTPException) as ei:
        ev.redispatch_event(9402, user={"username": "admin"})

    assert ei.value.status_code == 500, "不可判定失败（DB 写异常）必须显式 500"
    assert "重派入队异常" in str(ei.value.detail)
    _reset.assert_not_called()
    st, result = _get_event_row(c, 9402)
    assert st == "failed"
    assert "重派入队异常" in (result or "")
    assert _meta.get(f"redispatch:error:9402")


# ===== 切片 2：回执真实 + 入队后置校验 =====

def _enqueue_ok(setup_db, new_tid, status="queued", seq=9401):
    """入队成功桩：真实落库 agent_tasks 行并返回 id（模拟 enqueue_task 成功语义）。"""
    def _real_enqueue(**kwargs):
        c = setup_db
        c.execute(
            "INSERT INTO agent_tasks (id, status, runtime_id, agent_session_id, created_at, "
            "agent_type, event_seq, repo_key, issue_id, prompt) "
            "VALUES (?,?,?,?,datetime('now'),'coding',?,?, '5', 'do work')",
            (new_tid, status, 501, "s1", seq, RK))
        setup_db.commit()
        return new_tid
    return _real_enqueue


def test_terminal_rebuild_success_receipt_real_task_id(setup_db, monkeypatch):
    """成功重派：回执 = agent_tasks 真实 task_id，全回执无 "(new)" 占位。"""
    import json as _json
    c = setup_db
    _mk_engine(c, 501, "codex-t", tag="coding")
    _mk_task(c, "t_old3", "failed", runtime_id=501, seq=9403)
    _seed_event(c, 9403)
    setup_db.commit()
    from unittest.mock import MagicMock
    ev, svc, _reset, _meta = _common_patches(
        monkeypatch, plugin=False,
        enqueue_mock=MagicMock(side_effect=_enqueue_ok(setup_db, "t_real_2547", seq=9403)),
        seq=9403)

    res = ev.redispatch_event(9403, user={"username": "admin"})

    assert res["status"] == "redispatched"
    assert res["task_id"] == "t_real_2547", f"回执须为真实新任务 id，实际 {res.get('task_id')!r}"
    assert "(new)" not in _json.dumps(res), f"回执不得出现 (new) 占位，实际 {res}"
    svc.enqueue_task.assert_called_once()
    _reset.assert_called_once(), "成功路径才重置事件终态"
    assert _meta.get(f"redispatch:9403"), "成功路径记录幂等时间戳"
    assert not _meta.get(f"redispatch:error:9403"), "成功路径不写错误 meta"
    assert c.execute("SELECT status FROM agent_tasks WHERE id='t_real_2547'").fetchone()[0] == "queued"


def test_terminal_rebuild_enqueue_empty_id_500(setup_db, monkeypatch):
    """enqueue_task 返回空（契约破坏）→ 500，不得以 "(new)" 冒充成功。"""
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    c = setup_db
    _mk_engine(c, 501, "codex-t", tag="coding")
    _mk_task(c, "t_old4", "failed", runtime_id=501, seq=9404)
    _seed_event(c, 9404)
    setup_db.commit()
    ev, svc, _reset, _meta = _common_patches(
        monkeypatch, plugin=False, enqueue_mock=MagicMock(return_value=""), seq=9404)

    with pytest.raises(HTTPException) as ei:
        ev.redispatch_event(9404, user={"username": "admin"})

    assert ei.value.status_code == 500
    assert "未返回任务 id" in str(ei.value.detail)
    _reset.assert_not_called()
    st, result = _get_event_row(c, 9404)
    assert st == "failed" and "重派入队失败" in (result or "")
    assert _meta.get(f"redispatch:error:9404")


def test_terminal_rebuild_postverify_missing_row_500(setup_db, monkeypatch):
    """后置校验：enqueue 返回假 id 但 agent_tasks 无行 → 视为入队未生效 → 500。"""
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    c = setup_db
    _mk_engine(c, 501, "codex-t", tag="coding")
    _mk_task(c, "t_old5", "failed", runtime_id=501, seq=9405)
    _seed_event(c, 9405)
    setup_db.commit()
    ev, svc, _reset, _meta = _common_patches(
        monkeypatch, plugin=False,
        enqueue_mock=MagicMock(return_value="t_ghost_2547"), seq=9405)

    with pytest.raises(HTTPException) as ei:
        ev.redispatch_event(9405, user={"username": "admin"})

    assert ei.value.status_code == 500
    assert "重派后置校验失败" in str(ei.value.detail)
    _reset.assert_not_called()
    st, result = _get_event_row(c, 9405)
    assert st == "failed" and "重派后置校验失败" in (result or "")
    assert _meta.get(f"redispatch:error:9405")


def test_terminal_rebuild_postverify_unclaimable_status_500(setup_db, monkeypatch):
    """后置校验：新任务状态不可认领（非 queued/pending/running）→ 500。"""
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    c = setup_db
    _mk_engine(c, 501, "codex-t", tag="coding")
    _mk_task(c, "t_old6", "failed", runtime_id=501, seq=9406)
    _seed_event(c, 9406)
    setup_db.commit()
    ev, svc, _reset, _meta = _common_patches(
        monkeypatch, plugin=False,
        enqueue_mock=MagicMock(side_effect=_enqueue_ok(setup_db, "t_dead_2547",
                                                       status="completed", seq=9406)),
        seq=9406)

    with pytest.raises(HTTPException) as ei:
        ev.redispatch_event(9406, user={"username": "admin"})

    assert ei.value.status_code == 500
    assert "重派后置校验失败" in str(ei.value.detail)
    _reset.assert_not_called()
    assert _meta.get(f"redispatch:error:9406")


# ===== 切片 3：local_only 同机重试分支同款失败语义 =====

def test_local_only_enqueue_valueerror_409(setup_db, monkeypatch):
    """local_only 同机重试：enqueue 失败同样不吞——409 + 事件留因 + 不重置终态。"""
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    c = setup_db
    _mk_engine(c, 77, "local-w", tag="coding")
    _mk_task(c, "t_loc1", "failed", runtime_id=77, seq=9407)
    _seed_event(c, 9407)
    setup_db.commit()
    _boom = MagicMock(side_effect=ValueError(
        "pool_engine_ids resolved to an empty engine list (Issue #653)"))
    ev, svc, _reset, _meta = _common_patches(monkeypatch, plugin=False,
                                             enqueue_mock=_boom, seq=9407)

    with pytest.raises(HTTPException) as ei:
        ev.redispatch_event(9407, local_only=True, user={"username": "admin"})

    assert ei.value.status_code == 409, "local_only 入队失败必须显式 409，不得回 200"
    assert "重派入队失败" in str(ei.value.detail)
    _reset.assert_not_called()
    st, result = _get_event_row(c, 9407)
    assert st == "failed"
    assert "重派入队失败" in (result or "")
    assert _meta.get(f"redispatch:error:9407")


def test_local_only_success_receipt_real_task_id(setup_db, monkeypatch):
    """local_only 同机重试成功：回执真实新任务 id，无 "(new)" 占位。"""
    import json as _json
    from unittest.mock import MagicMock
    c = setup_db
    _mk_engine(c, 77, "local-w", tag="coding")
    _mk_task(c, "t_loc2", "failed", runtime_id=77, seq=9408)
    _seed_event(c, 9408)
    setup_db.commit()
    ev, svc, _reset, _meta = _common_patches(
        monkeypatch, plugin=False,
        enqueue_mock=MagicMock(side_effect=_enqueue_ok(setup_db, "t_real_loc", seq=9408)),
        seq=9408)

    res = ev.redispatch_event(9408, local_only=True, user={"username": "admin"})

    assert res["task_id"] == "t_real_loc", f"local_only 回执须真实 id，实际 {res.get('task_id')!r}"
    assert "(new)" not in _json.dumps(res)
    assert res.get("local_only") is True
    _reset.assert_called_once()


# ===== 切片 4：占位清理——四个非重建分支回执 task_id=None + note =====

def _assert_no_placeholder_receipt(res, extra_keys=()):
    import json as _json
    assert res.get("task_id") is None, \
        f"无新任务分支回执 task_id 须为 null（非占位），实际 {res.get('task_id')!r}"
    assert "(new)" not in _json.dumps(res), f"回执不得出现 (new) 占位，实际 {res}"
    assert res.get("note"), "回执须附 note 说明该分支未返回任务 id 的原因"
    for k in extra_keys:
        assert res.get(k) is not None


def test_onboarding_branch_receipt_null_not_placeholder(setup_db, monkeypatch):
    """onboarding 重派（任务级自愈）：task_id=None + note，不再 "(new)" 冒充。"""
    import src.api.onboarding as onb
    c = setup_db
    _seed_event(c, 9410, event_type="onboarding_explore")
    setup_db.commit()
    from unittest.mock import MagicMock
    ev, svc, _reset, _meta = _common_patches(
        monkeypatch, plugin=False, enqueue_mock=MagicMock(), seq=9410,
        event_type="onboarding_explore")
    monkeypatch.setattr(onb, "retry_onboarding_task",
                        lambda *a, **k: {"ok": True, "seq": 9009, "issue_number": 42})

    res = ev.redispatch_event(9410, user={"username": "admin"})

    assert res["status"] == "redispatched"
    _assert_no_placeholder_receipt(res)
    assert res.get("event_seq") == 9009, "保留 event_seq 语义（新接管事件/任务 seq）"
    svc.enqueue_task.assert_not_called()


def test_pr_subscription_branch_receipt_null_not_placeholder(setup_db, monkeypatch):
    """PR 订阅重派（原事件重建 task，不创建新事件）：task_id=None + note。"""
    from src.domains.runtime.pipeline import event_pipeline as epl
    c = setup_db
    _seed_event(c, 9411, event_type="a2a_event")
    setup_db.commit()
    from unittest.mock import MagicMock
    ev, svc, _reset, _meta = _common_patches(
        monkeypatch, plugin=False, enqueue_mock=MagicMock(), seq=9411,
        event_type="a2a_event", issue_id="#pr_5", scene_id="sc_2547")
    monkeypatch.setattr(epl, "_dispatch_pr_subscription_redispatch", lambda *a, **k: True)

    res = ev.redispatch_event(9411, user={"username": "admin"})

    assert res["status"] == "redispatched"
    _assert_no_placeholder_receipt(res, extra_keys=("event_seq",))
    assert res["event_seq"] == 9411, "PR 订阅重派在原事件上重建，event_seq 不变"


def test_a2a_redo_branch_receipt_null_not_placeholder(setup_db, monkeypatch):
    """A2A 重新交办（新事件接管）：task_id=None + note，保留新 event_seq。"""
    import urllib.request as _urlreq
    import src.domains.mgmt.user.db as udb2
    c = setup_db
    _seed_event(c, 9412, event_type="a2a_event")
    setup_db.commit()
    from unittest.mock import MagicMock

    class _FakeResp:
        def __init__(self, data):
            self._data = data
        def read(self):
            return self._data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    ev, svc, _reset, _meta = _common_patches(
        monkeypatch, plugin=False, enqueue_mock=MagicMock(), seq=9412,
        event_type="a2a_event")
    monkeypatch.setattr(udb2, "get_repo_bot_token_no_global", lambda rk: "tok")
    monkeypatch.setattr(udb2, "get_server_base_url", lambda: "http://localhost:8000")
    monkeypatch.setattr(_urlreq, "urlopen",
                        lambda req, timeout=30: _FakeResp(b'{"status":"ok","seq":777}'))

    res = ev.redispatch_event(9412, user={"username": "admin"})

    assert res["status"] == "redispatched"
    _assert_no_placeholder_receipt(res, extra_keys=("event_seq", "original_seq"))
    assert res["event_seq"] == 777, "A2A 重派由新事件接管，event_seq=新 seq"


def test_run_pipeline_branch_receipt_null_not_placeholder(setup_db, monkeypatch):
    """非 A2A 无任务事件（重走 run_pipeline）：task_id=None + note，保留新 event_seq。"""
    from src.domains.runtime.pipeline import event_pipeline as epl
    c = setup_db
    _seed_event(c, 9413, event_type="push")
    setup_db.commit()
    from unittest.mock import MagicMock
    ev, svc, _reset, _meta = _common_patches(
        monkeypatch, plugin=False, enqueue_mock=MagicMock(), seq=9413,
        event_type="push")
    monkeypatch.setattr(epl, "run_pipeline",
                        lambda *a, **k: {"ok": True, "seq": 888, "event_type": "push"})

    res = ev.redispatch_event(9413, user={"username": "admin"})

    assert res["status"] == "redispatched"
    _assert_no_placeholder_receipt(res, extra_keys=("event_seq", "original_seq"))
    assert res["event_seq"] == 888, "run_pipeline 重派由新事件接管，event_seq=新 seq"


# ===== 切片 5：审计一致——成功记 redispatch（真实 task_id），失败记 redispatch_failed =====

def _audit_rows(c, seq):
    import json as _json
    rows = c.execute(
        "SELECT payload FROM events WHERE event_type='admin_action' AND "
        "json_extract(payload, '$.target_event_seq')=?", (seq,)).fetchall()
    return [_json.loads(r[0]) for r in rows]


def test_audit_success_records_redispatch_with_real_task_id(setup_db, monkeypatch):
    """成功重派：审计 action=redispatch 且 target_task_id=真实新任务 id（非空、非占位）。"""
    import json as _json
    from unittest.mock import MagicMock
    c = setup_db
    _mk_engine(c, 501, "codex-t", tag="coding")
    _mk_task(c, "t_old7", "failed", runtime_id=501, seq=9414)
    _seed_event(c, 9414)
    setup_db.commit()
    ev, svc, _reset, _meta = _common_patches(
        monkeypatch, plugin=False,
        enqueue_mock=MagicMock(side_effect=_enqueue_ok(setup_db, "t_real_2547", seq=9414)),
        seq=9414)

    res = ev.redispatch_event(9414, user={"username": "auditor"})

    audits = _audit_rows(c, 9414)
    assert len(audits) == 1, f"成功重派须且仅须 1 条审计，实际 {len(audits)}"
    assert audits[0]["action"] == "redispatch"
    assert audits[0]["target_task_id"] == "t_real_2547", \
        f"审计须携带真实 task_id，实际 {audits[0].get('target_task_id')!r}"
    assert "(new)" not in _json.dumps(audits)


def test_audit_failure_records_redispatch_failed(setup_db, monkeypatch):
    """失败重派：审计 action=redispatch_failed（不得把失败重派记为成功）。"""
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    c = setup_db
    _mk_engine(c, 501, "codex-t", tag="coding")
    _mk_task(c, "t_old8", "failed", runtime_id=501, seq=9415)
    _seed_event(c, 9415)
    setup_db.commit()
    _boom = MagicMock(side_effect=ValueError("runtime_id or pool_agent_type is required (确定路由 D3)"))
    ev, svc, _reset, _meta = _common_patches(monkeypatch, plugin=False,
                                             enqueue_mock=_boom, seq=9415)

    with pytest.raises(HTTPException):
        ev.redispatch_event(9415, user={"username": "auditor"})

    audits = _audit_rows(c, 9415)
    assert len(audits) == 1, f"失败重派须留痕 1 条审计，实际 {len(audits)}"
    assert audits[0]["action"] == "redispatch_failed", \
        f"失败路径审计须为 redispatch_failed，实际 {audits[0].get('action')!r}"
    assert audits[0].get("error"), "失败审计须附失败原因（error 字段）"


# ===== QA 复审（PR #1432 note 200787016）补遗 =====

def test_audit_non_http_exception_records_redispatch_failed(setup_db, monkeypatch):
    """QA Spec-3：主流程裸抛非 HTTP 异常 → 出口审计兜底记 redispatch_failed 后原样抛出。

    此前包装器仅捕 HTTPException——非 HTTP 异常直接漏出，审计零留痕（低概率漏网）。
    """
    from unittest.mock import MagicMock
    from src.api import deps as _deps
    c = setup_db
    _mk_engine(c, 501, "codex-t", tag="coding")
    _mk_task(c, "t_x1", "failed", runtime_id=501, seq=9416)
    _seed_event(c, 9416)
    setup_db.commit()
    ev, svc, _reset, _meta = _common_patches(monkeypatch, plugin=False,
                                              enqueue_mock=MagicMock(), seq=9416)

    def _access_boom(*a, **k):
        raise RuntimeError("repo access check exploded")
    monkeypatch.setattr(_deps, "check_repo_access", _access_boom)

    with pytest.raises(RuntimeError, match="repo access check exploded"):
        ev.redispatch_event(9416, user={"username": "auditor"})

    audits = _audit_rows(c, 9416)
    assert len(audits) == 1, f"非 HTTP 异常也须留痕 redispatch_failed，实际 {len(audits)}"
    assert audits[0]["action"] == "redispatch_failed"
    assert "repo access check exploded" in (audits[0].get("error") or ""), \
        f"审计 error 须携带原始异常信息，实际 {audits[0].get('error')!r}"


def test_exit_guard_fires_before_terminal_reset(setup_db, monkeypatch):
    """QA Spec-2：出口防御守卫触发 → 500 且事件未被掰回 running（先校验再重置）。

    桩 _enqueue_task_verified 返回空，模拟收口被旁路/契约破坏路径直落出口：
    守卫必须先于 reset_event_terminal 与幂等时间戳触发——否则产生「悬挂 running + 500」，
    正是 Spec 风险表所防场景。
    """
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    c = setup_db
    _mk_engine(c, 501, "codex-t", tag="coding")
    _mk_task(c, "t_x2", "failed", runtime_id=501, seq=9418)
    _seed_event(c, 9418)
    setup_db.commit()
    ev, svc, _reset, _meta = _common_patches(monkeypatch, plugin=False,
                                             enqueue_mock=MagicMock(), seq=9418)
    monkeypatch.setattr(ev, "_enqueue_task_verified", lambda *a, **k: "")

    with pytest.raises(HTTPException) as ei:
        ev.redispatch_event(9418, user={"username": "admin"})

    assert ei.value.status_code == 500
    assert "出口缺少有效任务 id" in str(ei.value.detail)
    _reset.assert_not_called(), \
        "守卫必须先于 reset_event_terminal：触发时事件不得被重置为 running 悬挂"
    assert not _meta.get("redispatch:9418"), "守卫触发时不得先记幂等时间戳"


def test_completed_event_failure_reason_retained_via_meta(setup_db, monkeypatch):
    """QA Spec-1：completed 事件重派入队失败——result 被 event_store 终态保护拒绝
    failed 覆盖（保护语义保持，result 保持原值），失败原因经 meta
    redispatch:error:{seq} + HTTP detail 双通道合规留存。"""
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    c = setup_db
    _mk_engine(c, 501, "codex-t", tag="coding")
    _mk_task(c, "t_c1", "completed", runtime_id=501, seq=9419)
    _seed_event(c, 9419, status="completed", result="旧成功结果")
    setup_db.commit()
    _boom = MagicMock(side_effect=ValueError("runtime_id or pool_agent_type is required (确定路由 D3)"))
    ev, svc, _reset, _meta = _common_patches(monkeypatch, plugin=False,
                                             enqueue_mock=_boom, seq=9419)

    with pytest.raises(HTTPException) as ei:
        ev.redispatch_event(9419, user={"username": "admin"})

    assert ei.value.status_code == 409
    assert "重派入队失败" in str(ei.value.detail), "HTTP detail 须携带失败原因"
    st, result = _get_event_row(c, 9419)
    assert st == "completed", \
        "completed 事件不得被 failed 覆盖（event_store 终态保护语义保持，Issue #130）"
    # arch11 适配：#1503 后到诊断合并语义——终态保护只挡状态翻转，不吞失败原因。
    assert "旧成功结果" in (result or ""), "终态保护下 result 保留原值"
    assert "重派入队失败" in (result or ""), "失败原因经 #1503 合并通道写入 result"
    assert "重派入队失败" in (_meta.get(f"redispatch:error:9419") or ""), \
        "completed 事件失败原因须经 meta redispatch:error:{seq} 合规通道留存"
    _reset.assert_not_called()


def test_cancelled_event_failure_reason_retained_via_meta(setup_db, monkeypatch):
    """QA Spec-1：cancelled 事件重派入队失败——同 completed 口径：终态保护保持，
    原因经 meta + detail 双通道留存。"""
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    c = setup_db
    _mk_engine(c, 501, "codex-t", tag="coding")
    _mk_task(c, "t_c2", "cancelled", runtime_id=501, seq=9420)
    _seed_event(c, 9420, status="cancelled", result="已被新 seq 接管")
    setup_db.commit()
    _boom = MagicMock(side_effect=RuntimeError("db write boom"))
    ev, svc, _reset, _meta = _common_patches(monkeypatch, plugin=False,
                                             enqueue_mock=_boom, seq=9420)

    with pytest.raises(HTTPException) as ei:
        ev.redispatch_event(9420, user={"username": "admin"})

    assert ei.value.status_code == 500
    assert "重派入队异常" in str(ei.value.detail)
    st, result = _get_event_row(c, 9420)
    assert st == "cancelled", "cancelled 事件不得被 failed 覆盖（终态保护语义保持）"
    # arch11 适配：#1503 后到诊断合并语义——保留原值并合并诊断信息。
    assert "已被新 seq 接管" in (result or ""), "终态保护下 result 保留原值"
    assert "重派入队异常" in (result or ""), "失败原因经 #1503 合并通道写入 result"
    assert "重派入队异常" in (_meta.get(f"redispatch:error:9420") or ""), \
        "cancelled 事件失败原因须经 meta redispatch:error:{seq} 合规通道留存"
    _reset.assert_not_called()
