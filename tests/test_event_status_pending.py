"""方案 E：事件状态语义「入队 pending、认领才 running」（seq#2310 修复配套）。

覆盖：
- _fetch_heartbeat：本机 direct 引擎（credential_id=0）不因心跳写节奏慢被判失联（方案 A）
- claim_by_tag / claim 认领成功 → events pending→running（认领是唯一入口）
- update_task_status(running) → events pending→running（worker 上报开始执行）
- requeue_task → events running→pending（任务回落排队）
- _migrate_event_status_pending：存量 running+queued/untasked → pending
"""
import pytest
from unittest.mock import patch, MagicMock

from src.session_manager import db, db_lock
from src.runtime import health_scorer, service
from src.api import event_store


@pytest.fixture(autouse=True)
def _cleanup_runtime():
    """每个用例前清理测试残留（业务 id 段，避开并发其他测试）。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id LIKE 't_pend%'")
        c.execute("DELETE FROM events WHERE seq >= 96000 AND seq <= 96400")
        c.execute("DELETE FROM agent_runtimes WHERE id >= 96000 AND id <= 96400")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id >= 96000 AND runtime_id <= 96400")
        c.execute("DELETE FROM runtime_credentials WHERE id >= 96000 AND id <= 96400")
        db.commit()
    health_scorer._cache.clear()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id LIKE 't_pend%'")
        c.execute("DELETE FROM events WHERE seq >= 96000 AND seq <= 96400")
        c.execute("DELETE FROM agent_runtimes WHERE id >= 96000 AND id <= 96400")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id >= 96000 AND runtime_id <= 96400")
        c.execute("DELETE FROM runtime_credentials WHERE id >= 96000 AND id <= 96400")
        db.commit()


def _seed_runtime(rid, credential_id=0, status="online", scope="global", last_seen_at=None,
                  name=None):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (id, name, credential_id, status, healthy, installed, "
            "scope, owner_id, host_name, last_seen_at, org_id, max_concurrent, busy_slots) "
            "VALUES (?, ?, ?, ?, 1, 1, ?, 'pend-tester', 'pend-host', ?, "
            "COALESCE((SELECT org_id FROM agent_runtimes LIMIT 1), 1), 4, 0)",
            (rid, name or f"pend-eng-{rid}", credential_id, status, scope, last_seen_at))
        db.commit()


def _seed_event(seq, status, started_at=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, agent_type, title, actor, summary, "
            "payload, status, result, created_at, started_at) "
            "VALUES (?, 'gitcode:test/pend', 'a2a_event', 'coding', 't', 'a', 's', '{}', ?, '', datetime('now'), ?)",
            (seq, status, started_at))
        db.commit()


def _enqueue_pool_task(tid, seq, pool_tag="coding"):
    """直接构造池任务行（与 test_claim_by_tag_sql_fix 一致，避开 enqueue_task 链路依赖）。"""
    from datetime import datetime, timezone
    _now_iso = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, status, runtime_id, event_seq, issue_id, repo_key, "
            "agent_type, skill, prompt, cwd, created_at, started_at, pool_tag, pool_agent_type, "
            "pool_engine_ids, agent_session_id, issue_session_id, stream_ended, self_reported, "
            "env_extra, skill_files, project_info_files, project_info_meta, knowledge_files, "
            "project_info_file_urls, knowledge_file_urls, usage_json, runtime_session_id, event_type, "
            "account, repo_gitcode_token, server_url, attempts, affinity_runtime_id, exit_code, "
            "result_text, errors, stderr, user_id) "
            "VALUES (?, 'queued', 0, ?, '#1', 'gitcode:test/pend', 'coding', 'coding', 'p', '/tmp', "
            "?, NULL, ?, 'coding', '', '[]', 's1', 'is1', 0, 0, '{}', '{}', '{}', '{}', '{}', '{}',"
            "'{}', '', 'a2a_event', '', '', '', 0, 0, NULL, '', '', '', 0)",
            (tid, seq, _now_iso, _now_iso))
        db.commit()


class TestEventStatusSemantics:
    """方案 E：认领/启动才 running，排队为 pending。"""

    def test_event_pending_can_be_turned_running_by_claim(self):
        """排队事件 pending → claim_by_tag 认领 → events running。"""
        seq = 96001
        _seed_event(seq, "pending")
        _seed_runtime(96001, credential_id=0, scope="global")
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (96001, 'all', 1)")
            db.commit()
        _enqueue_pool_task("t_pend_claim", seq)
        with db_lock:
            got = service.claim_by_tag("all", 96001)
        assert got is not None, "claim_by_tag 应认领 pending 池任务"
        assert got["id"] == "t_pend_claim"
        with db_lock:
            evt = db.cursor().execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()
        assert evt[0] == "running", f"认领成功事件应为 running, got {evt[0]}"

    def test_event_untouched_claim_returns_none(self):
        """引擎不存在/健康分 0 时认领被拒，事件保持 pending。"""
        seq = 96002
        _seed_event(seq, "pending")
        _seed_runtime(96002, credential_id=99, status="online", scope="global",
                      last_seen_at="2020-01-01T00:00:00+00:00")  # 远端心跳超时 → score=0 → 被拒
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (96002, 'all', 1)")
            db.commit()
        _enqueue_pool_task("t_pend_none", seq)
        with db_lock:
            got = service.claim_by_tag("all", 96002)
        assert got is None, "心跳失联远端引擎不应认领（健康闸门）"
        with db_lock:
            evt = db.cursor().execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()
        assert evt[0] == "pending", "未被认领事件应保持 pending"

    def test_update_task_status_running_syncs_event(self):
        """worker 上报开始执行（running）→ 事件同步 running。"""
        seq = 96003
        _seed_event(seq, "pending")
        _enqueue_pool_task("t_pend_run", seq)
        service.update_task_status("t_pend_run", "running")
        with db_lock:
            evt = db.cursor().execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()
        assert evt[0] == "running", f"任务 running 应同步事件 running, got {evt[0]}"

    def test_requeue_returns_event_to_pending(self):
        """任务被 requeue（下回合重新认领）→ 事件回落 pending。"""
        seq = 96004
        _seed_event(seq, "running")
        _enqueue_pool_task("t_pend_req", seq)
        service.update_task_status("t_pend_req", "running")  # running
        service.requeue_task("t_pend_req")                    # 回 queued
        with db_lock:
            evt = db.cursor().execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()
        assert evt[0] == "pending", f"requeue 后事件应为 pending, got {evt[0]}"


class TestHeartbeatLocalEngine:
    """方案 A：本机 direct 引擎不因 last_seen_at 写节奏慢被判失联。"""

    def _fetch(self, rid):
        with patch.object(health_scorer, "_db", lambda: db), \
             patch.object(health_scorer, "_lock", lambda: db_lock):
            return health_scorer._fetch_heartbeat(rid)

    def test_local_engine_stale_heartbeat_still_alive(self):
        """credential_id=0 + status=online + last_seen_at 很旧 → 仍活跃（probe 判定）。"""
        from datetime import datetime, timedelta
        rid = 96050
        old = (datetime.now().astimezone() - timedelta(seconds=900)).strftime("%Y-%m-%d %H:%M:%S")
        _seed_runtime(rid, credential_id=0, status="online", last_seen_at=old)
        assert self._fetch(rid) is True, "本机 online 引擎不应因心跳旧而失联"

    def test_remote_engine_stale_heartbeat_lost(self):
        """credential_id>0 + last_seen_at 超阈值 → 失联（远端语义保持）。"""
        rid = 96051
        _seed_runtime(rid, credential_id=99, status="online",
                      last_seen_at="2020-01-01T00:00:00+00:00")
        assert self._fetch(rid) is False, "远端心跳超时仍应判失联"

    def test_local_engine_offline_lost(self):
        """本机引擎 status=offline → 失联。"""
        rid = 96052
        _seed_runtime(rid, credential_id=0, status="offline")
        assert self._fetch(rid) is False, "本机离线引擎应失联"

    def test_engine_not_exist_lost(self):
        """引擎存在性校验：不存在 → 失联。"""
        with patch.object(health_scorer, "_db", lambda: db), \
             patch.object(health_scorer, "_lock", lambda: db_lock):
            result = health_scorer._fetch_heartbeat(969999)
        assert result is False


class TestMigrateEventStatusPending:
    """一次性迁移：存量 running+queued / running+untasked → pending。"""

    def test_migrate_running_with_queued_task(self):
        from src.api.db_setup import _migrate_event_status_pending
        seq = 96301
        _seed_event(seq, "running")
        _seed_event(96302, "running")
        _enqueue_pool_task("t_pend_m1", seq)  # seq 96301 有 queued 任务
        _migrate_event_status_pending()
        with db_lock:
            c = db.cursor()
            assert c.execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()[0] == "pending"
            assert c.execute("SELECT status FROM events WHERE seq=?", (96302,)).fetchone()[0] == "pending"

    def test_migrate_running_with_active_task_stays(self):
        from src.api.db_setup import _migrate_event_status_pending
        seq = 96303
        _seed_event(seq, "running")
        _enqueue_pool_task("t_pend_m2", seq)
        service.update_task_status("t_pend_m2", "running")  # 任务 running → 事件 running
        _migrate_event_status_pending()
        with db_lock:
            evt = db.cursor().execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()
        assert evt[0] == "running", "有活跃任务的事件不应被迁移为 pending"

    def test_migrate_idempotent(self):
        from src.api.db_setup import _migrate_event_status_pending
        seq = 96304
        _seed_event(seq, "running")
        _enqueue_pool_task("t_pend_m3", seq)
        _migrate_event_status_pending()
        _migrate_event_status_pending()  # 重跑
        with db_lock:
            evt = db.cursor().execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()
        assert evt[0] == "pending"