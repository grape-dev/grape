"""Issue #3865：runtime 侧 runtime 域表（issue/events）读取必须走域连接原语。

三库拓扑下 mgmt 主库没有 runtime 域表，直连 ``session_manager.db`` 必 1146：
- ``collect_knowledge_files`` 第 4 步 org-context 快照静默丢失（.org-context/ 不进知识）；
- A2A 去重/取消门禁异常即 fail-open（``return 0`` / 继续派发），且用户与运维无感知；
- 跨仓 issue 存在性/来源映射查询同类。

本用例锁四件事：
1. 上述读取走 runtime 域游标（三库）；单库拓扑仍回落进程库（零回归）；
2. 读取失败可见：``meta["context_degraded"]=org_snapshot``、
   响应 ``detail.dedup_unavailable`` / ``detail.cancel_check_unavailable``；
3. 门禁失败仍 fail-open 派发（不因 DB 抖动全量阻断 A2A），只是「可见」；
4. 读点不再裸用 ``session_manager.db``（静态守门）。
"""
import inspect
import json
from contextlib import contextmanager

import pytest

from src.domains.runtime.engine import service as svc
from src.domains.runtime.routes import webhook as wh
from src.kernel import db_registry
from src.kernel.session_manager import db, db_lock

REPO_KEY = "gitcode:org/issue3865"
RUNTIME_DSN = "mysql://u:p@127.0.0.1:3306/grape_runtime_db"
_ERR_1146 = ("(1146, \"Table 'grape_prod_mgmt.issue' doesn't exist\")")


class _FakeCursor:
    """最小域游标桩：记录 SQL，可注入异常，可断言是否关闭。"""

    def __init__(self, rows=None, exc=None):
        self.rows = list(rows or [])
        self.exc = exc
        self.executed = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.exc is not None:
            raise self.exc
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self.closed = True


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


def _install_runtime_domain(monkeypatch, cur):
    """模拟三库拓扑：runtime 域有自有 DSN，cursor_for 拿到注入的假连接。"""
    monkeypatch.setenv("GRAPE_DSN_RUNTIME", RUNTIME_DSN)
    monkeypatch.delenv("GRAPE_PROCESS_ROLE", raising=False)
    monkeypatch.delenv("DEVOPS_DB_NAME", raising=False)
    monkeypatch.setattr(db_registry, "domain_db",
                        lambda d: _FakeConn(cur) if d == "runtime" else None)
    return cur


@pytest.fixture
def runtime_domain(monkeypatch):
    return lambda cur: _install_runtime_domain(monkeypatch, cur)


def _blank_sources(monkeypatch):
    """隔离知识三源的 IO：只验证 org-context 第 4 步。"""
    from src.domains.mgmt import public as mgmt_public

    monkeypatch.setattr(svc, "load_project_info_with_obs", lambda *a, **k: ({}, {}))
    monkeypatch.setattr("src.api.knowledge.member_files_pi", lambda *a, **k: {})
    monkeypatch.setattr("src.api.knowledge.agent_knowledge_files", lambda *a, **k: {})
    monkeypatch.setattr(mgmt_public, "load_org_snapshot_files",
                        lambda sid: {"org-context/rules.md": "# 组织硬规则"})


# ── A. org-context 快照：三库下走 domain，成功注入 .org-context/ ──────────────


def test_collect_knowledge_files_reads_org_snapshot_from_runtime_domain(
        runtime_domain, monkeypatch):
    cur = runtime_domain(_FakeCursor(rows=[("snap-1",)]))
    _blank_sources(monkeypatch)

    out = svc.collect_knowledge_files(REPO_KEY, "coding", "#7", scene_id="scene-1")

    assert "FROM issue" in cur.executed[0][0], "org-context 快照必须查 runtime 域 issue"
    assert cur.executed[0][1] == (REPO_KEY, 7)
    assert "org-context/rules.md" in out["knowledge"]
    assert out["knowledge"]["org-context/rules.md"]["content"] == "# 组织硬规则"
    assert cur.closed, "域游标必须关闭（泄漏连接）"
    assert "context_degraded" not in out["meta"]


# ── B. org-context 快照：域读失败 → meta 可见 context_degraded ────────────────


def test_collect_knowledge_files_marks_context_degraded(runtime_domain, monkeypatch, caplog):
    runtime_domain(_FakeCursor(exc=RuntimeError(_ERR_1146)))
    _blank_sources(monkeypatch)

    with caplog.at_level("WARNING"):
        out = svc.collect_knowledge_files(REPO_KEY, "coding", "#7", scene_id="scene-1")

    assert out["meta"]["context_degraded"] == "org_snapshot"
    assert "org-context/rules.md" not in out["knowledge"]
    logs = caplog.text
    assert "org-context" in logs and "degraded" in logs
    # 旧静默文案必须消失（Loki 验收：|= "collect org-context snapshot failed" 归零）
    assert "collect org-context snapshot failed" not in logs


# ── C. 单库拓扑零回归：无自有 DSN → 仍读进程库并注入 ─────────────────────────


def test_collect_knowledge_files_single_db_falls_back_to_process_db(monkeypatch):
    monkeypatch.setenv("GRAPE_DOMAIN_LIB", "0")
    monkeypatch.delenv("GRAPE_DSN_RUNTIME", raising=False)
    _blank_sources(monkeypatch)

    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
            "status, org_context_snapshot_id) VALUES (?,?,?,?,?,'active',?)",
            ("1", "", REPO_KEY, 7, "issue3865 single db", "snap-single"))
        db.commit()
    try:
        out = svc.collect_knowledge_files(REPO_KEY, "coding", "#7", scene_id="scene-1")
        assert "org-context/rules.md" in out["knowledge"]
        assert "context_degraded" not in out["meta"]
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
            db.commit()


# ── D. A2A 去重门禁：三库下查 domain；异常 → dedup_unavailable（仍 fail-open）──


def test_active_event_dedup_reads_runtime_domain(runtime_domain):
    cur = runtime_domain(_FakeCursor(rows=[(42, "", "{}")]))
    flags = {}

    seq = wh._has_active_a2a_event(REPO_KEY, "#5", "coding", degraded=flags)

    assert seq == 42
    assert "FROM events" in cur.executed[-1][0]
    assert flags == {}, "命中真实连接时不应打降级标记"
    assert cur.closed


def test_active_event_dedup_degrades_visibly(runtime_domain, caplog):
    runtime_domain(_FakeCursor(exc=RuntimeError(_ERR_1146)))
    flags = {}

    with caplog.at_level("WARNING"):
        seq = wh._has_active_a2a_event(REPO_KEY, "#5", "coding", degraded=flags)

    assert seq == 0, "fail-open 语义保持（DB 抖动不全量阻断派发）"
    assert flags["dedup_unavailable"] is True
    assert flags["runtime_domain_route"] == "domain"
    logs = caplog.text
    assert "dedup_unavailable" in logs
    assert REPO_KEY in logs and "route=domain" in logs
    assert "active-event dedup check failed" not in logs


def test_active_event_seqs_degrades_visibly(runtime_domain):
    runtime_domain(_FakeCursor(exc=RuntimeError(_ERR_1146)))
    flags = {}

    seqs = wh._active_a2a_event_seqs(REPO_KEY, "#5", "coding", degraded=flags)

    assert seqs == []
    assert flags["dedup_unavailable"] is True, "空集合 ≠ 无阻塞，必须带同源标记"


# ── E. 取消态门禁：domain 读；异常 → cancel_check_unavailable ────────────────


def test_cancel_check_reads_runtime_domain(runtime_domain):
    cur = runtime_domain(_FakeCursor(rows=[("cancelled",)]))
    flags = {}

    assert wh._a2a_issue_cancelled(REPO_KEY, "5", degraded=flags) is True
    assert "FROM issue" in cur.executed[-1][0]
    assert "status='cancelled'" in cur.executed[-1][0]
    assert flags == {}

    cur.rows = []
    assert wh._a2a_issue_cancelled(REPO_KEY, "5") is False


def test_cancel_check_degrades_visibly(runtime_domain, caplog):
    runtime_domain(_FakeCursor(exc=RuntimeError(_ERR_1146)))
    flags = {}

    with caplog.at_level("WARNING"):
        cancelled = wh._a2a_issue_cancelled(REPO_KEY, "5", degraded=flags)

    assert cancelled is False, "取消检查失败不阻断派发（显式降级而非 fail-closed）"
    assert flags["cancel_check_unavailable"] is True
    logs = caplog.text
    assert "cancel_check_unavailable" in logs and "route=domain" in logs
    assert "cancelled check failed" not in logs


# ── F. 跨仓 issue 存在性：domain 读；失败返回 False 且日志结构化 ──────────────


def test_issue_exists_in_db_reads_runtime_domain(runtime_domain):
    cur = runtime_domain(_FakeCursor(rows=[(1,)]))
    assert wh._issue_exists_in_db(REPO_KEY, 5) is True
    assert "FROM issue" in cur.executed[-1][0]
    assert "session_manager" not in inspect.getsource(wh._issue_exists_in_db)


def test_issue_exists_in_db_degrades_with_structured_log(runtime_domain, caplog):
    runtime_domain(_FakeCursor(exc=RuntimeError(_ERR_1146)))
    with caplog.at_level("WARNING"):
        assert wh._issue_exists_in_db(REPO_KEY, 5) is False
    # 保留原「未命中 → 外呼 VCS」语义，但必须给出 route/pid/host 定位字段
    assert "route=" in caplog.text and "target_db=runtime" in caplog.text


# ── G. 降级标记必须落到响应体（detail），dict / JSONResponse 两种形态 ────────


def test_duplicate_a2a_original_refs_reads_runtime_domain(runtime_domain):
    """Required（QA R2）：duplicate_a2a 拦截的 original_event 回查同病收口。

    `_duplicate_a2a_original_refs` 用 session_manager.db 读 events（runtime 域表），
    三库下必 1146 → agent/status/created_at 静默为空，违反 #729 引用契约。
    """
    cur = runtime_domain(_FakeCursor(rows=[("coding", "running", "#5", "2026-11-23 10:00:00")]))

    evt = wh._duplicate_a2a_original_refs(REPO_KEY, 42)

    assert evt["agent"] == "coding", "原事件字段必须来自 runtime 域读取"
    assert evt["status"] == "running"
    assert evt["issue_id"] == "#5"
    assert evt["created_at"] == "2026-11-23 10:00:00"
    assert any("FROM events" in sql and "repo_key=?" in sql and "seq=?"
               for sql, _ in cur.executed), cur.executed
    assert cur.closed


def test_duplicate_a2a_original_refs_degrades_visibly(runtime_domain, caplog):
    """域读失败 → unavailable 引用标注仍生成（拦截响应不阻断），日志含定位字段。"""
    runtime_domain(_FakeCursor(exc=RuntimeError(_ERR_1146)))

    with caplog.at_level("WARNING"):
        evt = wh._duplicate_a2a_original_refs(REPO_KEY, 42)

    assert evt["agent"] == "" and evt["seq"] == 42
    assert "task_ref" in evt and "result_ref" in evt
    assert "route=" in caplog.text and "target_db=runtime" in caplog.text


def test_stamp_degraded_json_response_content_length_matches_body():
    """Critical（QA R2）：重建 JSONResponse 时复制旧 content-length 会被 uvicorn 拒收。

    降级标记使 body 变长，而旧头仍是替换前的长度 → 服务端
    `RuntimeError: Response content longer than Content-Length`，客户端收到截断
    JSON。重建时必须剔除实体头（content-length/content-type），让 Starlette 按新
    body 重算。
    """
    resp = wh._a2a_skip_response("duplicate_a2a", agent="coding", source_seq=0,
                                 detail={"existing_seq": 9})
    stamped = wh._a2a_stamp_degraded(
        resp, {"dedup_unavailable": True, "runtime_domain_route": "domain"})
    assert stamped is not resp, "降级时必须重建响应（改 body 后头体一致）"
    headers = {k.lower(): v for k, v in stamped.headers.items()}
    assert int(headers["content-length"]) == len(stamped.body), (
        f"content-length({headers['content-length']}) 必须等于新 body 长度"
        f"({len(stamped.body)})，否则响应被 uvicorn 截断")
    assert headers["content-type"] == "application/json"
    # 非实体头（自定义追踪头）必须透传，不能因重建丢失
    import io
    import json as _json
    from fastapi.responses import JSONResponse as _JR
    _raw = _JR(status_code=200, content={"detail": {}},
               headers={"x-a2a-trace": "req-42"})
    _stamped = wh._a2a_stamp_degraded(_raw, {"dedup_unavailable": True})
    assert _stamped.headers.get("x-a2a-trace") == "req-42", "自定义头不得丢失"
    _h = {k.lower(): v for k, v in _stamped.headers.items()}
    assert int(_h["content-length"]) == len(_stamped.body)


def test_stamp_degraded_into_dict_and_json_response():
    from fastapi.responses import JSONResponse

    plain = {
        "status": "ok", "seq": 7,
        "detail": {"issue_number": 5},
    }
    out = wh._a2a_stamp_degraded(plain, {"dedup_unavailable": True})
    assert out["detail"] == {"issue_number": 5, "dedup_unavailable": True}

    resp = wh._a2a_skip_response("duplicate_a2a", agent="coding", source_seq=0,
                                 detail={"existing_seq": 9})
    stamped = wh._a2a_stamp_degraded(resp, {"dedup_unavailable": True})
    body = json.loads(stamped.body)
    assert body["detail"]["existing_seq"] == 9
    assert body["detail"]["dedup_unavailable"] is True
    assert stamped.status_code == resp.status_code

    noop = wh._a2a_stamp_degraded(plain, {})
    assert noop is plain, "无降级时不得改写响应对象"


# ── H. 端点级：门禁失败仍派发成功，且 detail 同时带两条降级标记 ──────────────


def _selective_runtime_cursor(needles):
    """cursor_for("runtime") 替身：命中 needles 的查询抛 1146，其余走进程库。"""

    class _Proxy:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, params=None):
            if any(n in sql for n in needles):
                raise RuntimeError(_ERR_1146)
            self._cur.execute(sql, params)
            return self

        def __getattr__(self, item):
            return getattr(self._cur, item)

    @contextmanager
    def _cf(domain, write=False):
        assert domain == "runtime"
        with db_lock:
            c = db.cursor()
            try:
                yield _Proxy(c)
            finally:
                c.close()

    return _cf


class TestA2AGateDegradeEndpoint:
    REPO = "gitcode:owner/repo"

    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:owner/repo%'")
            c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:owner/repo%'")
            c.execute("DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:owner/repo%'")
            try:
                c.execute("DELETE FROM managed_repos WHERE repo_key='gitcode:owner/repo'")
            except Exception:
                pass
            db.commit()

    def _seed_repo(self):
        with db_lock:
            c = db.cursor()
            try:
                c.execute(
                    "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                    "VALUES ('gitcode:owner/repo', 'owner', 'repo', 'owner/repo')")
            except Exception:
                pass
            db.commit()

    def test_gate_degraded_flags_visible_on_dispatch(self, monkeypatch):
        import os

        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from src.domains.runtime.routes import webhook as webhook_mod

        self._clean()
        self._seed_repo()
        app = FastAPI()
        app.include_router(webhook_mod.router)
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "queue_remote")
        monkeypatch.setattr(db_registry, "cursor_for", _selective_runtime_cursor(
            ("event_type='a2a_event' AND status IN ('running','dispatched')",
             "AND status='cancelled' LIMIT 1")))
        try:
            # 允许 A2A 入口走到派发：鉴权/agent 启用/场景节点/派发均用桩绕过，
            # 只保留「两条门禁读取失败」这一个变量。
            import src.api.session_store as session_store
            monkeypatch.setattr(webhook_mod, "_authorize_a2a", lambda *a, **k: {
                "user_id": 0, "username": "tester", "session_key": ""})
            monkeypatch.setattr(session_store, "is_agent_enabled", lambda *a, **k: True)
            monkeypatch.setattr(webhook_mod, "_scene_node_check",
                                lambda *a, **k: {"in_scene": True, "scene_id": "", "nodes": []})
            monkeypatch.setattr(webhook_mod, "run_a2a_dispatch",
                                lambda *a, **k: {"status": "ok", "seq": 1})

            client = TestClient(app)
            resp = client.post("/api/a2a", json={
                "agent": "coding",
                "payload": {"repository": "owner/repo", "account": "owner/repo",
                            "issue_number": "5", "pr_number": "", "context": "issue3865"},
                "queue": False, "force": False,
            })
        finally:
            os.environ.pop("DEVOPS_EXECUTION_MODE", None)

        assert resp.status_code == 200, resp.text
        data = resp.json()
        detail = data.get("detail") or {}
        assert detail.get("cancel_check_unavailable") is True, data
        assert detail.get("dedup_unavailable") is True, data
        # 本用例直接替换 cursor_for，处理拓扑标签按进程实际路由给出（单测 D 已断言 domain）
        assert detail.get("runtime_domain_route") in ("local", "domain"), data


# ── I. 静态守门：读点内不得再裸用 session_manager ────────────────────────────


@pytest.mark.parametrize("mod,names", [
    (svc, ("collect_knowledge_files",)),
    (wh, ("_has_active_a2a_event", "_active_a2a_event_seqs", "_queued_a2a_events",
          "_a2a_issue_cancelled", "_issue_exists_in_db",
          "_resolve_cross_repo_issue_number", "_duplicate_a2a_original_refs")),
])
def test_read_paths_no_longer_touch_session_manager(mod, names):
    for name in names:
        src = inspect.getsource(getattr(mod, name))
        assert "from src.kernel.session_manager import" not in src, \
            f"{name} 仍导入进程库直连（同病未收口）"
        assert "db.cursor()" not in src, f"{name} 仍裸用进程库游标"