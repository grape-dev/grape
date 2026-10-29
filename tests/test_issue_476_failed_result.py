# -*- coding: utf-8 -*-
"""Issue #476：失败终态强制携带可诊断 result（listener 路径 + 防御 + 字段 + 口径）。

覆盖（对应 Spec Acceptance Criteria）：
1. agent/handoff listener（webhook._on_handoff_dispatch）dispatch_node 返回 failed →
   事件 result 非空且含错误详情与目标 agent/node，payload._fail_diag 含 code/target_stage/phase/ref_seq
2. listener 抛异常 → 同样收口（result 含异常信息，code=a2a_dispatch_exception）
3. _fail_event 扩展 diag：result 内嵌 [code] 前缀，payload 写入 _fail_diag（幂等）
4. update_event_status 终态 failed 非空 result 防御：裸调用自动回填结果/exit_code/completed_at；
   调用方显式传 result 时优先使用
5. /api/events/{owner}/{repo}（list_events）补 result/issue_id/task_id 字段
6. /api/obs/events/failed-result-gap 跨仓空 result 占比口径（含 legacy 区分）

背景：webhook._on_handoff_dispatch 两处裸 update_event_status(seq,"failed") 只写 status，
不落 result/exit_code/completed_at → a2a_event 占空 result 样本 70%；#418/#368/#369
已收口早期退/任务判死，本 listener 路径漏改。
"""
import asyncio
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest

from src.session_manager import db, db_lock

_REPO = "gitcode:issue476-owner/repo"
_REPO_FULL = "issue476-owner/repo"


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "events", "devops_agent_session", "managed_repos",
                  "issue", "event_active_guard", "runtime_credentials", "user_repos"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key=? OR repo_key LIKE 'gitcode:issue476%' "
                          f"OR repo_key LIKE 'issue476%'", (_REPO,))
            except Exception:
                pass
        try:
            c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'issue476-%'")
        except Exception:
            pass
        db.commit()


def _mk_event(status="running", result="", issue_id="#7", agent_type="sre",
              event_type="a2a_event", repo_key=_REPO) -> int:
    from src.api.event_store import append_event
    seq = append_event(
        repo_key=repo_key, event_type=event_type,
        title="issue476-test", issue_id=issue_id, source="a2a",
        agent_type=agent_type, actor="tester",
        summary="issue476 fixture", payload={}, status=status,
        scene_id="", entity_type="",
    )
    if result:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE events SET result=? WHERE seq=?", (result, seq))
            db.commit()
    return seq


def _mk_task(event_seq, task_id="t-issue476-1", repo_key=_REPO, issue_id="#7") -> str:
    from datetime import datetime, timezone
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, repo_key,"
            " agent_type, skill, status, created_at, event_seq, issue_id) "
            "VALUES (?, 'sess-issue476-x', 0, 'p', ?, 'sre', 's', 'queued', ?, ?, ?)",
            (task_id, repo_key, datetime.now(timezone.utc).isoformat(), event_seq, issue_id))
        db.commit()
        return task_id


def _row(seq):
    with db_lock:
        c = db.cursor()
        return c.execute(
            "SELECT status, exit_code, completed_at, result, payload FROM events WHERE seq=?",
            (seq,)).fetchone()


def _fail_diag_of(seq):
    row = _row(seq)
    if not row:
        return {}
    try:
        return (json.loads(row[4] or "{}") or {}).get("_fail_diag") or {}
    except Exception:
        return {}


# ---------------------------------------------------------------- listener 路径

class TestHandoffListenerFailEvent:
    """webhook._on_handoff_dispatch：failed / 异常两分支 → _fail_event 完整诊断。"""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        yield
        _clean()

    @staticmethod
    def _handoff_event(seq, *, ref_seq=777, target="sre"):
        from src.api.event_bus import GrapeEvent, EventDomain
        return GrapeEvent(
            domain=EventDomain.AGENT, type="agent/handoff",
            repo_key=_REPO, issue_id="#7", agent_type=target, seq=seq,
            payload={
                "target_agent": target, "node_id": target, "scene_id": "default",
                "evt": {"type": "a2a_event", "ref_seq": ref_seq, "payload": {}},
            },
        )

    def test_failed_result_writes_diag(self):
        """dispatch_node 返回 failed → 事件 result 含错误与 target，payload 含诊断上下文。"""
        from unittest.mock import patch
        from src.api.routes.webhook import _on_handoff_dispatch
        seq = _mk_event(status="running")
        ev = self._handoff_event(seq)
        with patch("src.api.pipeline_executor.dispatch_node",
                   return_value={"status": "failed", "node": "sre", "mode": "emit",
                                 "error": "engine offline"}):
            asyncio.run(_on_handoff_dispatch(ev))
        row = _row(seq)
        assert row is not None
        assert row[0] == "failed", f"status 应 failed，实际 {row[0]}"
        assert row[1] == 1, f"exit_code 应 1，实际 {row[1]}"
        assert row[2], f"completed_at 应非空，实际 {row[2]!r}"
        assert row[3], f"result 应非空，实际 {row[3]!r}"
        assert "engine offline" in row[3], f"result 应含 dispatch_node error，实际 {row[3]!r}"
        assert "sre" in row[3], f"result 应含目标 agent，实际 {row[3]!r}"
        diag = _fail_diag_of(seq)
        assert diag.get("code") == "a2a_dispatch_failed", diag
        assert diag.get("target_stage") == "sre", diag
        assert diag.get("phase") in ("dispatch", "emitted", "listener"), diag
        assert diag.get("ref_seq") == 777, diag

    def test_exception_writes_diag(self):
        """dispatch_node 抛异常 → 事件 failed，result 含异常信息，code=a2a_dispatch_exception。"""
        from unittest.mock import patch
        from src.api.routes.webhook import _on_handoff_dispatch
        seq = _mk_event(status="running")
        ev = self._handoff_event(seq, target="qa")
        with patch("src.api.pipeline_executor.dispatch_node",
                   side_effect=RuntimeError("listener boom")):
            asyncio.run(_on_handoff_dispatch(ev))
        row = _row(seq)
        assert row is not None
        assert row[0] == "failed", f"status 应 failed，实际 {row[0]}"
        assert row[3] and "listener boom" in row[3], f"result 应含异常信息，实际 {row[3]!r}"
        assert "qa" in row[3], f"result 应含目标 agent，实际 {row[3]!r}"
        diag = _fail_diag_of(seq)
        assert diag.get("code") == "a2a_dispatch_exception", diag
        assert diag.get("target_stage") == "qa", diag
        assert diag.get("phase") in ("exception", "listener_exception"), diag
        assert diag.get("ref_seq") == 777, diag


class TestFailEventDiag:
    """_fail_event 扩展 diag：result [code] 前缀 + payload._fail_diag 幂等写入。"""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        yield
        _clean()

    def test_result_code_prefix_and_payload_diag(self):
        from src.api.event_pipeline import _fail_event
        seq = _mk_event(status="running")
        _fail_event(seq, "交办失败测试", diag={
            "code": "a2a_dispatch_failed", "target_stage": "qa",
            "phase": "dispatch", "ref_seq": 42,
        })
        row = _row(seq)
        assert row[0] == "failed"
        assert row[3].startswith("[a2a_dispatch_failed]"), f"result 应含 code 前缀，实际 {row[3]!r}"
        diag = _fail_diag_of(seq)
        assert diag["target_stage"] == "qa" and diag["phase"] == "dispatch" and diag["ref_seq"] == 42

    def test_diag_not_overwritten_on_terminal(self):
        from src.api.event_pipeline import _fail_event
        seq = _mk_event(status="running")
        _fail_event(seq, "第一次", diag={"code": "c1", "target_stage": "qa",
                                        "phase": "dispatch", "ref_seq": 1})
        _fail_event(seq, "第二次", diag={"code": "c2", "target_stage": "sre",
                                        "phase": "dispatch", "ref_seq": 2})
        diag = _fail_diag_of(seq)
        assert diag.get("code") == "c1", f"终态 diag 不应被覆盖，实际 {diag!r}"
        assert diag.get("ref_seq") == 1


class TestUpdateEventStatusDefense:
    """update_event_status 终态 failed 非空 result 防御。"""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        yield
        _clean()

    def test_bare_failed_backfills_diag(self):
        from src.api.event_store import update_event_status
        seq = _mk_event(status="running")
        update_event_status(seq, "failed")
        row = _row(seq)
        assert row[0] == "failed"
        assert row[3], f"裸 failed 应回填 result，实际 {row[3]!r}"
        assert row[1] == 1, f"裸 failed 应回填 exit_code=1，实际 {row[1]}"
        assert row[2], f"裸 failed 应回填 completed_at，实际 {row[2]!r}"

    def test_explicit_result_preserved(self):
        from src.api.event_store import update_event_status
        seq = _mk_event(status="running")
        update_event_status(seq, "failed", result="用户手动终止事件")
        row = _row(seq)
        assert row[0] == "failed"
        assert row[3] == "用户手动终止事件", f"显式 result 应优先，实际 {row[3]!r}"

    def test_completed_not_affected(self):
        from src.api.event_store import update_event_status
        seq = _mk_event(status="running")
        update_event_status(seq, "completed")
        row = _row(seq)
        assert row[0] == "completed"
        assert row[3] in (None, ""), f"completed 不应被防御回填，实际 {row[3]!r}"


# ---------------------------------------------------------------- 接口字段

_app = None
_client = None
_uid = None
_headers = {}


def setup_module():
    global _client, _uid, _headers
    from src.api.main import app as _a
    from src.api.db_setup import init_database
    init_database()
    _client = TestClient(_a)
    _uid = grape_account_db.create_grape_account("ev476_user", email=None, password="pwd")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("ev476_user")
    _headers = {"Authorization": "Bearer " + _jwt(_uid["id"], _uid["username"])}


def _jwt(user_id, username):
    from src.auth.grape_account_db import get_token_version
    from src.api.jwt_utils import create_access_token
    return create_access_token(user_id, username, get_token_version(user_id))


def _grant_access(uid, repo_key=_REPO):
    """托管 + 用户可见（list_events JWT 校验需要 managed_repos JOIN user_repos）。"""
    owner, _, name = repo_key.split(":", 1)[1].partition("/")
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
            "VALUES (?, ?, ?, ?)", (repo_key, owner, name, f"{owner}/{name}"))
        c.execute(
            "INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) "
            "VALUES (?, ?, ?, ?, 1, 1)", (uid, repo_key, owner, name))
        db.commit()


from fastapi.testclient import TestClient  # noqa: E402
from src.auth import grape_account_db  # noqa: E402


def test_list_events_fields():
    """list_events 返回 result/issue_id/task_id（含 agent_tasks 关联）。"""
    _clean()
    _grant_access(_uid["id"])
    seq = _mk_event(status="failed", result="[a2a_dispatch_failed] 引擎离线", issue_id="#7")
    _mk_task(seq)
    r = _client.get("/api/events/issue476-owner/repo", headers=_headers)
    assert r.status_code == 200, r.text
    items = [e for e in r.json()["events"] if e["seq"] == seq]
    assert items, r.text
    ev = items[0]
    assert ev["issue_id"] == "#7", ev
    assert ev["task_id"] == "t-issue476-1", ev
    assert ev["result"] and "引擎离线" in ev["result"], ev


def test_failed_result_gap_route():
    """/api/obs/events/failed-result-gap：跨仓空 result 占比（legacy 区分）。"""
    _clean()
    from src.api.deps import require_org_member
    from src.api.main import app
    from src.observability.overview import visible_repo_keys
    UID = 47601
    USER = {"id": UID, "username": "ev476-obs", "org_id": 0, "org_role": "owner"}
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join)"
                  " VALUES (?, ?, 'issue476-owner', 'repo', 1, 1)", (UID, _REPO))
        db.commit()
    # 4 条 failed：1 空、1 legacy、2 有诊断
    _mk_event(status="failed", result="", issue_id="#1")
    _mk_event(status="failed", result="[legacy] 历史失败事件未记录原因", issue_id="#2")
    _mk_event(status="failed", result="[a2a_dispatch_failed] 引擎离线", issue_id="#3")
    _mk_event(status="failed", result="无可用引擎（engine_unavailable）", issue_id="#4")
    # completed 空 result 不进口径
    _mk_event(status="completed", result="", issue_id="#5")
    app.dependency_overrides[require_org_member] = lambda: USER
    try:
        with TestClient(app) as cli:
            r = cli.get("/api/obs/events/failed-result-gap")
            assert r.status_code == 200, r.text
            b = r.json()
            assert b["total_failed"] == 4, b
            assert b["empty_result"] == 1, b
            assert b["legacy"] == 1, b
            assert b["with_diag"] == 2, b
            assert b["empty_ratio"] == 0.25, b
            assert b["legacy_ratio"] == 0.25, b
            assert b["diagnosed_ratio"] == 0.5, b
    finally:
        app.dependency_overrides.pop(require_org_member, None)