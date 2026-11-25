"""Issue #4088：派发层评论事件重试/重放幂等（event_seq + comment_id）测试。

覆盖：
- 正向：首派抢占派发守卫并记录 event_seq；守卫命中（cid 变体重放）短路不派发，
  事件标记 skipped，出参回显 idempotent=True / 命中事件 seq，event_skipped 留痕；
- 幂等键回显：同 comment_id 重投走既有 duplicate_comment_delivery 语义，
  出参同样回显 idempotent / 命中事件 seq（原事件不重复建段）；
- 重试窗口：窗口内命中短路；守卫行过期后放行重新派发；窗口关闭（<=0）旁路；
- 并发：多线程同 (repo, agent, comment_id) 抢占仅一次成功；
- 清理：窗口过期行清理跳过运行中 task，终态/无 task 正常清理；
- 接管：接管跳过不残留派发守卫，重投/重派不被误判重复派发；
- 回显：守卫命中但读不到命中 seq 时回显 unknown，不伪造 0；
- 重派：守卫同 seq（本事件自身恢复）放行；跨 seq 命中拒绝并回显命中 seq。
"""
import json
import threading

import pytest

from src.kernel.session_manager import db, db_lock
from src.api import event_pipeline as ep
from src.domains.runtime import event_store as es


RK = "gitcode:test/i4088"

_clean_sql = [
    "DELETE FROM events WHERE repo_key LIKE 'gitcode:test/i4088%'",
    "DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:test/i4088%'",
    "DELETE FROM event_active_guard WHERE repo_key LIKE 'gitcode:test/i4088%'",
    "DELETE FROM grape_t_comment_dispatch_guard WHERE repo_key LIKE 'gitcode:test/i4088%'",
    "DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:test/i4088%'",
    "DELETE FROM issue WHERE repo_key LIKE 'gitcode:test/i4088%'",
    "DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:test/i4088%'",
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _dispatched = []
    monkeypatch.setattr(ep, "_enqueue_pipeline_task",
                        lambda *a, **k: _dispatched.append(a) or "t_x")
    _clean()
    _seed_issue()
    yield
    _clean()


def _clean():
    with db_lock:
        c = db.cursor()
        for sql in _clean_sql:
            c.execute(sql)
        db.commit()


def _seed_issue(num: int = 9, stage: str = "coding", status: str = "processing"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title,"
            " task_description, status, stage, owner_agent)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (2, "alice", RK, num, f"需求 #{num}", "desc", status, stage, stage))
        db.commit()


def _run(payload: dict, header: str = "Note Hook"):
    raw = json.dumps(payload).encode()
    headers = {"X-GitCode-Event": header}
    return ep.run_pipeline(raw, headers, account="test/i4088", repo_key_override="test/i4088")


def _issue_comment_payload(username="alice", note="Issue 评论", num=9, cid=90001):
    return {
        "object_kind": "note",
        "project": {"path_with_namespace": "test/i4088"},
        "object_attributes": {"noteable_type": "Issue", "iid": num, "note": note,
                              "id": cid, "noteable_iid": num},
        "issue": {"iid": num, "number": num},
        "user": {"username": username},
    }


def _events():
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT seq, status, event_type, issue_id, result FROM events "
            "WHERE repo_key=? ORDER BY seq", (RK,))
        return c.fetchall()


def _skipped(reason=None):
    with db_lock:
        c = db.cursor()
        if reason:
            c.execute("SELECT COUNT(*), IFNULL(MAX(detail),'') FROM event_skipped "
                      "WHERE repo_key=? AND reason=?", (RK, reason))
        else:
            c.execute("SELECT COUNT(*), '' FROM event_skipped WHERE repo_key=?", (RK,))
        return c.fetchone()


def _guard_rows():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, agent_type, comment_id, event_seq FROM "
                  "grape_t_comment_dispatch_guard WHERE repo_key=?", (RK,))
        return c.fetchall()


def _age_guard(comment_id, seconds=7200):
    from src.domains.runtime.event_store import _utc_now_str
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE grape_t_comment_dispatch_guard SET created_at=? WHERE repo_key=? "
                  "AND comment_id=?", (_utc_now_str(-seconds), RK, str(comment_id)))
        db.commit()


def _guard_exists(comment_id) -> bool:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM grape_t_comment_dispatch_guard WHERE repo_key=? AND comment_id=?",
                  (RK, str(comment_id)))
        return c.fetchone() is not None


def _insert_task(task_id: str, event_seq: int, status: str):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq,"
            " repo_key, agent_type, prompt, status) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, f"sess_{task_id}", 0, event_seq, RK, "qa", "prompt", status))
        db.commit()


class TestDispatchGuardHelpers:
    """helper 层：抢占 / 命中查询 / 重试窗口 / 并发。"""

    def test_claim_then_hit_within_window(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "3600")
        assert es.claim_comment_dispatch_guard(RK, "qa", "90001", event_seq=77)
        hit = es.find_comment_dispatch_guard_hit(RK, "qa", "90001")
        assert hit.get("event_seq") == 77
        # 同键二次抢占失败（命中）
        assert not es.claim_comment_dispatch_guard(RK, "qa", "90001", event_seq=78)
        # 跨 Agent 各自独立一次
        assert es.claim_comment_dispatch_guard(RK, "coding", "90001", event_seq=79)

    def test_window_expiry_allows_new_claim(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "3600")
        assert es.claim_comment_dispatch_guard(RK, "qa", "90002", event_seq=80)
        _age_guard("90002", seconds=7200)
        assert es.find_comment_dispatch_guard_hit(RK, "qa", "90002") == {}
        assert es.claim_comment_dispatch_guard(RK, "qa", "90002", event_seq=81)

    def test_window_zero_disables_guard(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "0")
        assert es.claim_comment_dispatch_guard(RK, "qa", "90003", event_seq=82)
        assert es.claim_comment_dispatch_guard(RK, "qa", "90003", event_seq=83)
        assert es.find_comment_dispatch_guard_hit(RK, "qa", "90003") == {}

    def test_incomplete_key_fail_open(self):
        assert es.claim_comment_dispatch_guard(RK, "", "90004", event_seq=84)
        assert es.claim_comment_dispatch_guard(RK, "qa", "", event_seq=85)

    def test_concurrent_claims_only_one_wins(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "3600")
        results = []
        lock = threading.Lock()

        def _worker(i):
            ok = es.claim_comment_dispatch_guard(RK, "qa", "90005", event_seq=100 + i)
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 1, results
        hit = es.find_comment_dispatch_guard_hit(RK, "qa", "90005")
        assert 100 <= hit.get("event_seq") <= 107

    def test_redispatch_gate_same_seq_allowed_cross_seq_blocked(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "3600")
        assert es.claim_comment_dispatch_guard(RK, "qa", "90006", event_seq=200)
        # 同 seq：本事件自身恢复 → 放行
        gate = es.comment_dispatch_guard_allows_redispatch(RK, "qa", "90006", 200)
        assert gate.get("allowed") is True
        # 跨 seq：同一评论已有派发事件 → 拒绝并回显命中 seq
        gate2 = es.comment_dispatch_guard_allows_redispatch(RK, "qa", "90006", 201)
        assert gate2.get("allowed") is False
        assert gate2.get("hit_seq") == 200
        # 无命中 → 放行
        gate3 = es.comment_dispatch_guard_allows_redispatch(RK, "qa", "90007", 202)
        assert gate3.get("allowed") is True


class TestPipelineDispatchIdempotency:
    """pipeline 层：先查后派、命中短路、出参回显。"""

    def test_first_delivery_claims_guard_with_event_seq(self):
        r1 = _run(_issue_comment_payload(note="首条派发", cid=90011))
        assert r1.get("seq") > 0
        assert r1.get("idempotent") is not True, r1
        rows = _guard_rows()
        assert len(rows) == 1, rows
        assert rows[0][3] == r1["seq"], rows

    def test_guard_hit_short_circuits_dispatch_with_echo(self):
        # 模拟 #4085 同源不同键变体：守卫已登记该评论的首派（seq=77）
        assert es.claim_comment_dispatch_guard(RK, _routed_agent(), "90012", event_seq=77)
        r = _run(_issue_comment_payload(note="变体重放", cid=90012))
        assert r.get("skipped") == "duplicate_comment_dispatch", r
        assert r.get("idempotent") is True, r
        assert r.get("idempotent_hit_seq") == 77, r
        # 事件段标记 skipped，不派发（无任务入队）；非 skipped 段仅探测事件 1 条
        rows = _events()
        skipped_rows = [r for r in rows if r[1] == "skipped"]
        assert len(skipped_rows) == 1, rows
        assert "duplicate_comment_dispatch" in (skipped_rows[0][4] or ""), rows
        assert len([r for r in rows if r[1] != "skipped"]) == 1, rows
        cnt, detail = _skipped("duplicate_comment_dispatch")
        assert cnt == 1, detail
        assert "idempotent_hit_seq=77" in detail, detail

    def test_guard_window_expiry_allows_redispatch_delivery(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "3600")
        assert es.claim_comment_dispatch_guard(RK, _routed_agent(), "90013", event_seq=78)
        _age_guard("90013", seconds=7200)
        r = _run(_issue_comment_payload(note="窗口过期后重投", cid=90013))
        assert r.get("skipped") != "duplicate_comment_dispatch", r
        assert r.get("seq") > 0

    def test_same_comment_id_replay_echoes_hit_seq(self):
        r1 = _run(_issue_comment_payload(note="同 id 重投", cid=90014))
        r2 = _run(_issue_comment_payload(note="同 id 重投-内容已变", cid=90014))
        assert r2.get("skipped") == "duplicate_comment_delivery", r2
        assert r2.get("idempotent") is True, r2
        assert r2.get("idempotent_hit_seq") == r1["seq"], (r1, r2)
        # 仅首条派发：非 skipped 事件只有 1 条
        rows = [r for r in _events() if r[1] != "skipped"]
        assert len(rows) == 1, _events()

    def test_guard_window_zero_bypasses_dispatch_guard(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "0")
        assert es.claim_comment_dispatch_guard(RK, _routed_agent(), "90015", event_seq=79)
        r = _run(_issue_comment_payload(note="守卫关闭", cid=90015))
        assert r.get("skipped") != "duplicate_comment_dispatch", r
        assert r.get("seq") > 0


def _routed_agent() -> str:
    """评论事件按 Issue 阶段（coding）路由的目标 Agent。"""
    r0 = _run(_issue_comment_payload(note="路由探测", cid=99999))
    assert r0.get("seq") > 0
    with db_lock:
        c = db.cursor()
        c.execute("SELECT agent_type FROM events WHERE seq=?", (r0["seq"],))
        row = c.fetchone()
    return (row[0] if row and row[0] else "coding") or "coding"


class TestPurgeStaleDispatchGuards:
    """清理：窗口过期行清理需沿用「排除运行中 task」保护（Standards 修复 1）。"""

    def test_purge_skips_guard_with_running_task(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "3600")
        assert es.claim_comment_dispatch_guard(RK, "qa", "90031", event_seq=401)
        _age_guard("90031", seconds=7200)
        _insert_task("t_4088_run", 401, "running")
        es.purge_stale_comment_dispatch_guards()
        # 窗口虽已过期，但有运行中 task 关联 → 守卫保留（防长任务被误清理后重复建段）
        assert _guard_exists("90031")

    def test_purge_skips_guard_with_dispatched_task(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "3600")
        assert es.claim_comment_dispatch_guard(RK, "qa", "90032", event_seq=402)
        _age_guard("90032", seconds=7200)
        _insert_task("t_4088_disp", 402, "dispatched")
        es.purge_stale_comment_dispatch_guards()
        assert _guard_exists("90032")

    def test_purge_removes_guard_after_task_terminal(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "3600")
        assert es.claim_comment_dispatch_guard(RK, "qa", "90033", event_seq=403)
        _age_guard("90033", seconds=7200)
        _insert_task("t_4088_done", 403, "completed")
        n = es.purge_stale_comment_dispatch_guards()
        assert n >= 1
        assert not _guard_exists("90033")

    def test_purge_removes_guard_without_task(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "3600")
        assert es.claim_comment_dispatch_guard(RK, "qa", "90034", event_seq=404)
        _age_guard("90034", seconds=7200)
        n = es.purge_stale_comment_dispatch_guards()
        assert n >= 1
        assert not _guard_exists("90034")


class TestTakeoverDispatchGuardRelease:
    """接管：claim 移出接管守门之前 + 跳过时同步释放守卫（Spec 高风险修复）。"""

    def test_takeover_skip_releases_dispatch_guard(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "3600")
        import src.domains.runtime.engine.takeover as takeover
        monkeypatch.setattr(takeover, "active_takeover_for",
                            lambda *a, **k: {"id": "tk4088", "username": "bob"})
        assert es.claim_comment_dispatch_guard(RK, "qa", "90021", event_seq=321)
        sess = ep._skip_dispatch_for_takeover(RK, 321, "#9", "qa")
        assert sess
        # 接管跳过 → 释放以该事件 seq 登记的派发守卫，避免残留守卫吞掉重投
        assert not _guard_exists("90021")
        gate = es.comment_dispatch_guard_allows_redispatch(RK, "qa", "90021", 322)
        assert gate.get("allowed") is True, gate

    def test_takeover_skip_pipeline_leaves_no_residual_guard(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "3600")
        agent = _routed_agent()
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_t_comment_dispatch_guard WHERE repo_key=?", (RK,))
            db.commit()

        import src.domains.runtime.engine.takeover as takeover
        monkeypatch.setattr(takeover, "active_takeover_for",
                            lambda *a, **k: {"id": "tk4088", "username": "bob"})
        r = _run(_issue_comment_payload(note="接管期间评论", cid=90022))
        assert r.get("skipped") == "already_taken_over", r
        assert not _guard_exists("90022"), _guard_rows()
        # 接管结束后重派（新 seq）可恢复派发，不被残留守卫判重复派发
        gate = es.comment_dispatch_guard_allows_redispatch(
            RK, agent, "90022", int(r["seq"]) + 1)
        assert gate.get("allowed") is True, gate


class TestDispatchGuardHitEcho:
    """回显：命中但读不到命中 seq 时回显 unknown，不伪造 0（Standards 修复 3）。"""

    def test_guard_hit_without_hit_seq_echoes_unknown(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "3600")
        agent = _routed_agent()
        assert es.claim_comment_dispatch_guard(RK, agent, "90041", event_seq=501)
        monkeypatch.setattr(es, "find_comment_dispatch_guard_hit", lambda *a, **k: {})
        r = _run(_issue_comment_payload(note="命中但读不到 seq", cid=90041))
        assert r.get("skipped") == "duplicate_comment_dispatch", r
        assert r.get("idempotent") is True, r
        assert r.get("idempotent_hit_seq_unknown") is True, r
        assert "idempotent_hit_seq" not in r, r
        cnt, detail = _skipped("duplicate_comment_dispatch")
        assert cnt == 1, detail
        assert "idempotent_hit_seq=unknown" in detail, detail
