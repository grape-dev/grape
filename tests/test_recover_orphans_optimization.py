"""recover-orphans 回归测试（Issue #271 / PR #385 follow-up）。

QA 审查发现：runtime.py:713 用 `errors=CONCAT(COALESCE(errors,''), ?)` 在 SQLite 后端
抛 `no such function: CONCAT`，导致 recover-orphans force_failed(attempts>=3) 分支
整个事务异常回滚、端点 500、封顶安全网本身失效（PR #385 引入，仅在 MySQL 后端上被
验证通过，SQLite 默认后端爆雷）。

本测试覆盖三种路径（均跑在测试库 SQLite 默认后端，直接拦住 CONCAT 类问题）：
- active 任务（近期有 task_messages）→ 跳过回收（skipped_active）
- 孤儿任务（无新消息）→ attempts+1 回炉回 queued（recovered）
- attempts>=封顶（3）的孤儿 → 置 failed（force_failed），不再 CONCAT
"""
import os
import secrets

import pytest

from src.session_manager import db, db_lock
from src.runtime import service


def _make_token(name="CI-RecoverOrphans"):
    import hashlib
    token = "rta_" + secrets.token_urlsafe(32)
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO runtime_credentials (token_hash, name, scope) VALUES (?,?,?)",
                  (hashlib.sha256(token.encode()).hexdigest(), name, "global"))
        db.commit()
    return token


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        db.cursor().execute("DELETE FROM task_messages")
        db.cursor().execute("DELETE FROM agent_tasks")
        db.cursor().execute("DELETE FROM runtime_credentials")
        db.cursor().execute("DELETE FROM agent_runtimes")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM task_messages")
        db.cursor().execute("DELETE FROM agent_tasks")
        db.cursor().execute("DELETE FROM runtime_credentials")
        db.cursor().execute("DELETE FROM agent_runtimes")
        db.commit()


def _register_runtime(client, token):
    """注册一个 runtime 返回其 id。"""
    r = client.post("/api/runtime/register",
                    json={"device_name": "ci-orphans", "runtimes": [{"name": "opencode"}]},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()["runtimes"][0]["id"]


def _enqueue(runtime_id, session_id="sess-o", seq=1, prompt="p"):
    return service.enqueue_task(
        agent_session_id=session_id, runtime_id=runtime_id, prompt=prompt,
        event_seq=seq, issue_id="#271", repo_key="gitcode:test/repo",
        agent_type="issue", skill="issue")


def _mark_running(tid):
    """直接置 running（绕开 claim 的 600s stale 回收分支，保证路径确定性）。"""
    with db_lock:
        db.cursor().execute("UPDATE agent_tasks SET status='running' WHERE id=?", (tid,))
        db.commit()


def _set_attempts(tid, attempts):
    with db_lock:
        db.cursor().execute("UPDATE agent_tasks SET attempts=? WHERE id=?", (attempts, tid))
        db.commit()


class TestRecoverOrphans:
    def test_active_task_skipped(self):
        """近期有 task_messages → 活跃任务，不回收（skipped_active）。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        token = _make_token()
        client = TestClient(app)
        rid = _register_runtime(client, token)

        tid = _enqueue(rid)
        _mark_running(tid)
        # 活跃信号：最近一条 task_messages（created_at 用与生产一致的 ISO 格式，
        # SQLite datetime('now') 的 'YYYY-MM-DD HH:MM:SS' 与 ISO 'T'+offset 字符串
        # 比较会失真，导致活跃度检查误判为孤儿）
        from datetime import datetime, timezone
        from src.api.time_utils import normalize_event_time
        active_ts = normalize_event_time(datetime.now(timezone.utc).isoformat())
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO task_messages (task_id, seq, type, content, created_at) "
                "VALUES (?,?,?,?, ?)",
                (tid, 1, "text", "agent active", active_ts))
            db.commit()

        r = client.post(f"/api/runtime/runtimes/{rid}/recover-orphans",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert tid in body["skipped_active"], f"活跃任务应跳过: {body}"
        assert tid not in body["recovered"]
        assert tid not in body["force_failed"]
        # 状态保持 running（不被回炉）
        assert service.get_task(tid)["status"] == "running"

    def test_orphan_requeued_with_attempts_plus1(self):
        """无新消息的孤儿任务 → attempts+1 回炉回 queued（recovered）。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        token = _make_token()
        client = TestClient(app)
        rid = _register_runtime(client, token)

        tid = _enqueue(rid)
        _mark_running(tid)
        _set_attempts(tid, 1)

        r = client.post(f"/api/runtime/runtimes/{rid}/recover-orphans",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert tid in body["recovered"], f"孤儿任务应回炉: {body}"
        t = service.get_task(tid)
        assert t["status"] == "queued"
        assert t["attempts"] == 2, f"attempts 应 +1: {t['attempts']}"

    def test_orphan_at_cap_force_failed_no_concat(self):
        """孤儿任务 attempts>=封顶(3) → 置 failed（force_failed），SQLite 下不抛 CONCAT）。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        token = _make_token()
        client = TestClient(app)
        rid = _register_runtime(client, token)

        tid = _enqueue(rid)
        _mark_running(tid)
        _set_attempts(tid, 2)

        r = client.post(f"/api/runtime/runtimes/{rid}/recover-orphans",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, f"SQLite 下 force_failed 分支不应 500: {r.text}"
        body = r.json()
        assert tid in body["force_failed"], f"attempts>=封顶应 force_failed: {body}"
        t = service.get_task(tid)
        assert t["status"] == "failed"
        assert t["attempts"] == 3
        # 说明写 result_text（非 errors），不依赖 CONCAT
        assert "recover-orphans attempts cap reached" in (t.get("result_text") or "")

    def test_full_pipeline_through_claim(self):
        """端到端：claim 置 dispatched → recover-orphans 回 queued 后可再次 claim。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        token = _make_token()
        client = TestClient(app)
        rid = _register_runtime(client, token)

        tid = _enqueue(rid)
        got = service.claim(rid)
        assert got and got["id"] == tid

        r = client.post(f"/api/runtime/runtimes/{rid}/recover-orphans",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        assert tid in r.json()["recovered"]
        assert service.get_task(tid)["status"] == "queued"
        # 2026-10-09 #253 归零语义调整：回炉任务 runtime_id 归零 + pool_agent_type 回填
        # （对齐 master 侧回收；引擎删重建后防死定向）→ 定向 claim(rid) 不再命中，
        # 改为广播认领：注册引擎默认 'all' 标签，claim_by_tag 可再领。
        _t = service.get_task(tid)
        assert _t["runtime_id"] == 0, f"回炉应归零 runtime_id: {_t['runtime_id']}"
        assert _t["pool_agent_type"] == "issue", f"pool_agent_type 应回填 issue: {_t['pool_agent_type']}"
        assert _t["pool_tag"] == "issue", f"pool_tag 应回填 issue（claim_by_tag 查此字段）: {_t['pool_tag']}"
        again = service.claim_by_tag("issue", rid)
        assert again and again["id"] == tid, "recover 后广播可再次认领"