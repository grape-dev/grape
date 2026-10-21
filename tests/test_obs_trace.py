"""追踪 / 任务时间线测试（§6.4）。

钉死四件事：
1. trace id **确定性**：同一 task / 同一 (repo,issue) 恒等；无 issue 的任务退回任务级 id
2. 时间线装配：enqueue → dispatch → 步骤（task_messages）→ terminal，队列等待/耗时/失败原因/用量
3. **可见范围**：他人仓库的 trace 返回空（路由 404）；同 issue 的多个任务共享 pipeline trace
4. 与日志互跳：时间线内联命中该任务的日志（按 task_id）
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability import trace as tr  # noqa: E402

UID = 7331
REPO = "gitcode:org/trace-mine"
OTHER = "gitcode:org/trace-theirs"
T1 = "t_trc0000000001"
T2 = "t_trc0000000002"


def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for sql, params in [
            ("DELETE FROM agent_tasks WHERE id LIKE 't_trc%'", ()),
            ("DELETE FROM task_messages WHERE task_id LIKE 't_trc%'", ()),
            ("DELETE FROM obs_log WHERE task_id LIKE 't_trc%'", ()),
            ("DELETE FROM obs_llm_usage WHERE task_id LIKE 't_trc%'", ()),
            ("DELETE FROM user_repos WHERE user_id=?", (UID,)),
        ]:
            try:
                c.execute(sql, params)
            except Exception:
                pass
        db.commit()


def _grant(repo_key):
    from src.session_manager import db, db_lock
    owner, _, name = repo_key.split(":", 1)[1].partition("/")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join)"
                  " VALUES (?,?,?,?,1,1)", (UID, repo_key, owner, name))
        db.commit()


def _task(tid, repo_key, issue_id="#7", agent_type="coding", status="completed",
          wait_s=2, dur_s=127, errors="", runtime_id=262, event_seq=588):
    from src.session_manager import db, db_lock
    now = datetime.now(timezone.utc)
    enq = now - timedelta(seconds=wait_s + dur_s + 5)
    disp = enq + timedelta(seconds=wait_s)
    term = disp + timedelta(seconds=dur_s)
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, repo_key, agent_type,"
            " user_id, status, issue_id, event_seq, created_at, started_at, completed_at, errors,"
            " trace_id, pipeline_trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "s-" + tid, runtime_id, "p", repo_key, agent_type, UID, status, issue_id, event_seq,
             enq.isoformat(), disp.isoformat(), term.isoformat(), errors,
             tr.task_trace_id(tid), tr.pipeline_trace_id(repo_key, issue_id, tid)))
        db.commit()


def _msg(tid, seq, typ="tool_use", tool="shell", output="ok"):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR IGNORE INTO task_messages (task_id, seq, type, tool, content,"
                  " input_json, output, created_at) VALUES (?,?,?,?,?,?,?,?)",
                  (tid, seq, typ, tool, "", '{"cmd": "ls"}', output,
                   datetime.now(timezone.utc).isoformat()))
        db.commit()


def _usage(tid, i=1000, o=100, cr=500):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO obs_llm_usage (dedup_key, task_id, seq, source, repo_key,"
                  " model_key, input_tokens, output_tokens, cache_read_tokens, created_at, updated_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (f"{tid}:1", tid, 1, "hermes", REPO, "claude-sonnet-4.5", i, o, cr,
                   datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
        db.commit()


def _log(tid, level="ERROR", msg="boom"):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO obs_log (dedup_key, ts, level, component, source, task_id,"
                  " repo_key, msg) VALUES (?,?,?,?,?,?,?,?)",
                  (f"k-{tid}-{level}", datetime.now(timezone.utc).isoformat(), level, "worker", "w1",
                   tid, REPO, msg))
        db.commit()


@pytest.fixture(autouse=True)
def _setup():
    _clean()
    yield
    _clean()


# ---------------------------------------------------------------- id 确定性

def test_trace_ids_are_deterministic():
    assert tr.task_trace_id(T1) == tr.task_trace_id(T1)
    assert tr.task_trace_id(T1) != tr.task_trace_id(T2)
    assert tr.task_trace_id(T1).startswith("tr_")
    # 同 (repo, issue) 共享流水线 id；不同 issue / 不同仓库不同
    a = tr.pipeline_trace_id(REPO, "#7", T1)
    b = tr.pipeline_trace_id(REPO, "#7", T2)
    assert a == b, "同一 issue 的任务必须共享 pipeline trace"
    assert tr.pipeline_trace_id(REPO, "#8", T1) != a
    assert tr.pipeline_trace_id(OTHER, "#7", T1) != a
    # 无 issue → 退回任务级
    assert tr.pipeline_trace_id(REPO, "", T1) == tr.task_trace_id(T1)


def test_stamp_missing_backfills_legacy_rows():
    from src.session_manager import db, db_lock
    _task(T1, REPO)
    with db_lock:                                   # 模拟历史任务：trace 为空
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET trace_id='', pipeline_trace_id='' WHERE id=?", (T1,))
        db.commit()
    n = tr.stamp_missing()
    assert n >= 1
    with db_lock:
        c = db.cursor()
        c.execute("SELECT trace_id, pipeline_trace_id FROM agent_tasks WHERE id=?", (T1,))
        row = c.fetchone()
    assert row[0] == tr.task_trace_id(T1)
    assert row[1] == tr.pipeline_trace_id(REPO, "#7", T1)


# ---------------------------------------------------------------- 时间线装配

def test_timeline_assembles_steps_tokens_and_timing():
    _grant(REPO)
    _task(T1, REPO, status="completed", wait_s=2, dur_s=127)
    _msg(T1, 1, "tool_use", "shell", "ok1")
    _msg(T1, 2, "text", "", "")
    _usage(T1, 1000, 100, 500)
    _log(T1, "ERROR", "engine crashed")
    user = {"id": UID, "username": "u", "org_id": 0}
    d = tr.timeline(user, tr.task_trace_id(T1))
    assert d["counts"]["tasks"] == 1
    t = d["tasks"][0]
    assert t["id"] == T1 and t["agent_type"] == "coding"
    assert t["queue_wait_s"] == 2, t          # enqueue → dispatch
    assert t["duration_s"] == 127, t           # dispatch → terminal
    assert len(t["steps"]) == 2 and t["steps"][0]["tool"] == "shell"
    assert t["tokens"]["total"] == 1600, t["tokens"]
    assert len(t["log_hits"]) == 1 and "crashed" in t["log_hits"][0]["msg"]
    assert d["kind"] == "task"
    assert d["logs"], "时间线应内联命中该任务的日志"


def test_timeline_failure_reason_and_engine_label():
    _grant(REPO)
    _task(T1, REPO, status="failed", errors="pytest failed: 3 errors\nmore")
    user = {"id": UID, "username": "u", "org_id": 0}
    d = tr.timeline(user, tr.task_trace_id(T1))
    t = d["tasks"][0]
    assert t["failure_reason"].startswith("pytest failed"), t
    assert d["counts"]["failed"] == 1


def test_pipeline_trace_groups_all_tasks_of_issue():
    _grant(REPO)
    _task(T1, REPO, agent_type="vod", issue_id="#7")
    _task(T2, REPO, agent_type="coding", issue_id="#7")
    user = {"id": UID, "username": "u", "org_id": 0}
    d = tr.timeline(user, tr.pipeline_trace_id(REPO, "#7"))
    assert d["counts"]["tasks"] == 2, d["tasks"]
    assert {t["agent_type"] for t in d["tasks"]} == {"vod", "coding"}
    assert d["kind"] == "pipeline"
    # 按 issue 直接查（前端入口）
    d2 = tr.pipeline_by_issue(user, "org/trace-mine", "#7")
    assert d2["counts"]["tasks"] == 2
    assert d2["issue_id"] == "#7"


def test_timeline_without_steps():
    _grant(REPO)
    _task(T1, REPO)
    _msg(T1, 1)
    user = {"id": UID, "username": "u", "org_id": 0}
    d = tr.timeline(user, tr.task_trace_id(T1), with_steps=False)
    assert "steps" not in d["tasks"][0]


# ---------------------------------------------------------------- 可见范围

def test_other_users_trace_is_invisible():
    _grant(REPO)
    _task(T2, OTHER, issue_id="#9")
    user = {"id": UID, "username": "u", "org_id": 0}
    d = tr.timeline(user, tr.task_trace_id(T2))
    assert d["tasks"] == [], "他人仓库的 trace 不该可见"
    assert d["logs"] == []


def test_unknown_trace_returns_empty():
    _grant(REPO)
    user = {"id": UID, "username": "u", "org_id": 0}
    d = tr.timeline(user, "tr_deadbeefdeadbeef")
    assert d["tasks"] == [] and d["kind"] == "unknown"


# ---------------------------------------------------------------- 路由

def test_routes_trace_and_404_for_out_of_scope():
    from fastapi.testclient import TestClient
    from src.api.deps import require_org_member
    from src.api.main import app

    _grant(REPO)
    _task(T1, REPO, issue_id="#7")
    _task(T2, OTHER, issue_id="#9")
    user = {"id": UID, "username": "u", "org_id": 0, "org_role": "owner"}
    app.dependency_overrides[require_org_member] = lambda: user
    try:
        with TestClient(app) as cli:
            r = cli.get(f"/api/obs/trace/{tr.task_trace_id(T1)}")
            assert r.status_code == 200, r.text
            assert r.json()["tasks"][0]["id"] == T1
            # 他人仓库 → 404
            r = cli.get(f"/api/obs/trace/{tr.task_trace_id(T2)}")
            assert r.status_code == 404, r.text
            # 不存在的 trace → 404
            r = cli.get("/api/obs/trace/tr_0000000000000000")
            assert r.status_code == 404, r.text
            # 按 issue 查流水线
            r = cli.get("/api/obs/trace", params={"repo": "org/trace-mine", "issue_id": "#7"})
            assert r.status_code == 200 and r.json()["counts"]["tasks"] == 1, r.text
            r = cli.get("/api/obs/trace", params={"repo": "org/trace-theirs", "issue_id": "#9"})
            assert r.status_code == 404, r.text
            # 回填端点
            r = cli.post("/api/obs/trace/stamp")
            assert r.status_code == 200 and "stamped" in r.json(), r.text
    finally:
        app.dependency_overrides.pop(require_org_member, None)