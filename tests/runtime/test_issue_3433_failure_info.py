"""Issue #3433：异常中断事件落库结构化失败原因，详情页可读。

覆盖：
① failure_info 构造/兜底/阶段分类/解析单元行为；
② update_event_complete / update_event_status 失败终态写入 failure_info（含自动兜底）；
③ completed 终态不写 failure_info（成功路径归档结构不变）；
④ _fail_event 派发失败路径落阶段与错误码；
⑤ GET /api/events/detail/{seq} 返回 failure_info。
"""
import json
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.kernel.session_manager import db, db_lock  # noqa: E402

RK = "gitcode:org/issue3433"


@pytest.fixture(autouse=True)
def _cleanup_seeded_events():
    """每条用例后清掉本文件写入的事件，避免固定 seq 在重复/并行运行时互相污染。"""
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM events WHERE repo_key=?", (RK,))
        db.commit()


def _seed_event(seq, *, status="running", result="", failure_info="",
                agent_type="coding", runtime="opencode"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, source, issue_id, title, summary, "
            "actor, payload, agent_type, runtime, skill, status, exit_code, duration, "
            "agent_logs, result, created_at, started_at, completed_at, failure_info) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'),?,?)",
            (seq, RK, "a2a_event", "a2a", "#3433", "t", "s", "", "{}",
             agent_type, runtime, "coding", status, None, None, "[]", result, "", failure_info))
        db.commit()


def _failure_info(seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT failure_info FROM events WHERE seq=?", (seq,))
        row = c.fetchone()
    assert row is not None
    return json.loads(row[0]) if row[0] else {}


def _event_cols():
    return [r[1] for r in db.cursor().execute("PRAGMA table_info(events)").fetchall()]


# ── ① 单元：构造 / 分类 / 兜底 ───────────────────────────────────────────

class TestFailureInfoUnit:
    def test_build_contains_required_fields(self):
        from src.domains.runtime.agents.failure_info import build_failure_info
        fi = build_failure_info(
            error_type="RuntimeError", phase="execution", message="boom",
            runtime="engine-1", agent="coding", exit_code=-1)
        assert fi["error_type"] == "RuntimeError"
        assert fi["phase"] == "execution"
        assert fi["phase_label"] == "执行阶段"
        assert fi["message"] == "boom"
        assert fi["runtime"] == "engine-1"
        assert fi["agent"] == "coding"
        assert fi["exit_code"] == -1

    def test_build_truncates_huge_message(self):
        from src.domains.runtime.agents.failure_info import build_failure_info
        fi = build_failure_info(message="x" * 5000)
        assert len(fi["message"]) <= 800

    def test_classify_phase_by_exit_code_and_hint(self):
        from src.domains.runtime.agents.failure_info import classify_phase
        assert classify_phase(124) == "timeout"
        assert classify_phase(-9) == "terminated"
        assert classify_phase(1) == "execution"
        assert classify_phase(1, "执行超时（600s）") == "timeout"
        assert classify_phase(1, "process killed by SIGKILL") == "terminated"

    def test_finalize_fills_missing_from_result_and_row(self):
        from src.domains.runtime.agents.failure_info import finalize_failure_info
        fi = finalize_failure_info({"phase": "execution", "message": "内核崩溃"}, agent="coding",
                                   runtime="engine-2")
        assert fi["agent"] == "coding"
        assert fi["runtime"] == "engine-2"
        assert fi["phase_label"] == "执行阶段"
        assert fi["error_type"]
        fi2 = finalize_failure_info(None, result="派发失败：引擎离线", exit_code=1)
        assert fi2["phase"] == "dispatch"
        assert "派发失败" in fi2["message"]

    def test_parse_accepts_json_string(self):
        from src.domains.runtime.agents.failure_info import parse_failure_info
        parsed = parse_failure_info('{"error_type":"X","phase":"timeout","message":"m"}')
        assert parsed["error_type"] == "X"
        assert parse_failure_info("not-json") == {}

    def test_fake_completion_vs_killed(self):
        from src.domains.runtime.agents.failure_info import failure_info_for_exit
        fake = failure_info_for_exit([], "无可信产出", 0, fake_completion=True)
        assert fake["phase"] == "completion_gate"
        assert fake["error_type"] == "FakeCompletion"
        killed = failure_info_for_exit(["killed"], "进程被 kill", -9)
        assert killed["phase"] == "terminated"
        assert killed["error_type"] == "ProcessKilled"


class TestFailureInfoHelpers:
    """direct / pipeline 共用 helper（Standards 去重）与级联归因。"""

    def test_from_exception_builds_crash_attribution(self):
        from src.domains.runtime.agents.failure_info import failure_info_from_exception
        try:
            raise RuntimeError("kaboom")
        except RuntimeError as exc:
            fi = failure_info_from_exception(exc, "traceback-snippet")
        assert fi["error_type"] == "RuntimeError"
        assert fi["phase"] == "execution"
        assert "kaboom" in fi["message"]

    def test_for_stream_skips_non_failed_terminal(self):
        from src.domains.runtime.agents.failure_info import failure_info_for_stream
        assert failure_info_for_stream("completed", [], "ok", 0) is None
        assert failure_info_for_stream("cancelled", [], "cancelled", -1) is None
        killed = failure_info_for_stream("failed", ["killed"], "", -9)
        assert killed["phase"] == "terminated"
        assert killed["error_type"] == "ProcessKilled"

    def test_for_cascade_timeout_and_recover(self):
        from src.domains.runtime.agents.failure_info import failure_info_for_cascade
        timeout = failure_info_for_cascade(
            task_status="timeout_failed", result="任务超时", exit_code=-1)
        assert timeout["phase"] == "timeout"
        assert timeout["error_type"] == "TimeoutError"
        recover = failure_info_for_cascade(
            result="claimed but the engine session never started", exit_code=1,
            phase="recover")
        assert recover["phase"] == "recover"
        assert recover["message"]


class TestCascadeFailureInfo:
    """超时/孤儿回收级联判死必须落 failure_info（Spec 期望行为 #3）。"""

    def test_timeout_failed_cascade_writes_failure_info(self):
        from src.domains.runtime.engine.service import _cascade_event_terminal
        _seed_event(34337)
        with db_lock:
            c = db.cursor()
            converged = _cascade_event_terminal(
                c, 34337, "timeout_failed", exit_code=-1,
                task_status="timeout_failed")
            db.commit()
        assert converged
        fi = _failure_info(34337)
        assert fi["phase"] == "timeout"
        assert fi["error_type"] == "TimeoutError"
        assert fi["agent"] == "coding"

    def test_orphan_recover_writes_failure_info(self):
        from src.domains.runtime.engine.service import _recover_orphan_event_terminal
        _seed_event(34338)
        with db_lock:
            c = db.cursor()
            rowcount = _recover_orphan_event_terminal(
                c, 34338, reason="中断：running 超时无活跃任务执行记录",
                phase="recover")
            db.commit()
        assert rowcount == 1
        fi = _failure_info(34338)
        assert fi["phase"] == "recover"
        assert "中断" in fi["message"]


# ── ② 收尾写库：显式 / 兜底 / 成功不写 ─────────────────────────────────

class TestEventStoreFailureInfo:
    def test_failed_writes_structured_failure_info_and_fills_row(self):
        from src.api.event_store import update_event_complete
        assert "failure_info" in _event_cols(), "events.failure_info 列缺失（需加列迁移）"
        _seed_event(34330)
        update_event_complete(
            34330, "failed", exit_code=-1, result="执行崩溃",
            failure_info={"error_type": "RuntimeError", "phase": "execution",
                          "message": "boom"})
        fi = _failure_info(34330)
        assert fi["error_type"] == "RuntimeError"
        assert fi["phase"] == "execution"
        assert fi["agent"] == "coding"
        assert fi["runtime"] == "opencode"
        assert fi["exit_code"] == -1

    def test_failed_without_failure_info_falls_back_to_result(self):
        from src.api.event_store import update_event_complete
        _seed_event(34331)
        update_event_complete(34331, "failed", exit_code=-9, result="进程被 SIGKILL 终止")
        fi = _failure_info(34331)
        assert fi, "失败事件必须有非空 failure_info"
        assert fi["message"]
        assert fi["phase"] == "terminated"
        assert fi["error_type"] == "ProcessKilled"

    def test_failed_empty_result_uses_explicit_message(self):
        from src.api.event_store import update_event_complete
        _seed_event(34332)
        update_event_complete(34332, "failed", exit_code=1, result="",
                              failure_info={"error_type": "ValueError",
                                            "phase": "execution",
                                            "message": "bad input"})
        with db_lock:
            row = db.cursor().execute(
                "SELECT result FROM events WHERE seq=?", (34332,)).fetchone()
        assert "bad input" in (row[0] or "")

    def test_completed_does_not_write_failure_info(self):
        from src.api.event_store import update_event_complete
        _seed_event(34333, failure_info="")
        update_event_complete(34333, "completed", exit_code=0, result="ok")
        with db_lock:
            row = db.cursor().execute(
                "SELECT failure_info FROM events WHERE seq=?", (34333,)).fetchone()
        assert not row[0], "成功事件不得写入 failure_info"

    def test_update_event_status_failed_writes_fallback(self):
        from src.api.event_store import update_event_status
        _seed_event(34334)
        update_event_status(34334, "failed", result="派发失败：引擎离线")
        fi = _failure_info(34334)
        assert fi["message"]
        assert fi["phase"] == "dispatch"
        assert fi["error_type"] == "DispatchError"


# ── ③ _fail_event 派发失败路径 ─────────────────────────────────────────

def test_fail_event_records_dispatch_phase():
    from src.domains.runtime.pipeline.event_pipeline import _fail_event
    _seed_event(34335)
    _fail_event(34335, "A2A 交办派发失败（target=qa）",
                diag={"phase": "dispatch", "code": "a2a_dispatch_failed"})
    fi = _failure_info(34335)
    assert fi["phase"] == "dispatch"
    assert fi["error_type"] == "a2a_dispatch_failed"
    assert "A2A" in fi["message"]


# ── ④ 详情 API 返回 failure_info ───────────────────────────────────────

def _detail(seq):
    from fastapi.testclient import TestClient
    from unittest.mock import patch
    from src.api.main import app
    from src.domains.mgmt.auth import grape_account_db
    from src.kernel.auth_sdk.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version

    uid = grape_account_db.create_grape_account("issue3433_user", email=None, password="pwd")
    if not uid:
        uid = grape_account_db.get_grape_account_by_username("issue3433_user")
    token = create_access_token(uid["id"], uid["username"], get_token_version(uid["id"]))
    with patch("src.api.deps.check_repo_access", return_value=None):
        return TestClient(app).get(
            f"/api/events/detail/{seq}",
            headers={"Authorization": "Bearer " + token})


def test_event_detail_returns_failure_info():
    _seed_event(
        34336, status="failed", result="执行崩溃",
        failure_info=json.dumps({
            "error_type": "RuntimeError", "phase": "execution",
            "phase_label": "执行阶段", "message": "boom",
            "runtime": "engine-9", "agent": "coding", "exit_code": -1,
        }, ensure_ascii=False))
    r = _detail(34336)
    assert r.status_code == 200
    fi = r.json()["failure_info"]
    assert fi["error_type"] == "RuntimeError"
    assert fi["phase"] == "execution"
    assert fi["message"] == "boom"
    assert fi["runtime"] == "engine-9"


# ── ⑤ 观测事件与端到端异常/被 kill 归因 ───────────────────────────────

def test_skill_resolve_failed_observation_event_has_failure_info():
    from src.domains.runtime.pipeline.event_pipeline import _fail_skill_resolve_event
    result = _fail_skill_resolve_event(
        RK, "missing-skill", "scene-1", "coding", "技能不存在或不可用",
        issue_id="#3433", seq=0)
    assert result["status"] == "failed"
    _seq = result["seq"]
    assert _seq and _seq > 0
    fi = _failure_info(_seq)
    assert fi["phase"] == "dispatch"
    assert fi["error_type"] == "SkillResolveError"
    assert "技能不存在" in fi["message"]


def test_unhandled_exception_end_to_end_persists_failure_info(monkeypatch):
    # 目标线把执行入口迁到 direct_executor（event_pipeline 只剩 __getattr__ 转发），
    # 补丁必须打在实现所在模块，否则打到空壳、真执行器照跑。
    from src.domains.runtime.engine import direct_executor as _de
    from src.domains.runtime.pipeline import event_pipeline as _ep
    _seed_event(34339)

    def _boom(*args, **kwargs):
        raise RuntimeError("kaboom-in-executor")

    monkeypatch.setattr(_de, "_execute_agent_inner", _boom)
    monkeypatch.setattr(_ep, "_update_direct_task", lambda *a, **k: None)
    monkeypatch.setattr(_ep, "_cleanup_direct_workdir", lambda *a, **k: None)
    _de._execute_agent(RK, 34339, "coding", "direct", "coding", {})

    fi = _failure_info(34339)
    assert fi["error_type"] == "RuntimeError"
    assert fi["phase"] == "execution"
    assert "kaboom-in-executor" in fi["message"]


def test_killed_exit_code_persists_terminated_failure_info():
    from src.api.event_store import update_event_complete
    from src.domains.runtime.agents.failure_info import failure_info_for_stream
    _seed_event(34340)
    fi = failure_info_for_stream("failed", ["killed by signal 9"], "", -9)
    update_event_complete(34340, "failed", exit_code=-9,
                          result="进程被 SIGKILL 终止", failure_info=fi)
    persisted = _failure_info(34340)
    assert persisted["phase"] == "terminated"
    assert persisted["error_type"] == "ProcessKilled"
    assert persisted["exit_code"] == -9