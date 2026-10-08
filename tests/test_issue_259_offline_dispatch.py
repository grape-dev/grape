"""Issue #259：worker/引擎离线后任务不再调度到离线引擎。

验证点：
1. get_scene_pool_engine_ids 健康过滤：离线/心跳过期引擎不进 pool
2. 全灭返回 None（方案A）；未配引擎返回 ""（全量广播，原行为）
3. _check_pool_engines_reachable 心跳过期 → 不可达（窗口期防误判）
4. pipeline_executor 直选定向：心跳过期 → 降级池任务
5. reap_stale_runtimes：NULL 心跳幽灵行也标 offline；dispatched/running 回收分级
"""
import datetime
import json
import time

import pytest

from src.session_manager import db, db_lock
from src.runtime import engine_select as es
from src.runtime import service as svc


@pytest.fixture(autouse=True)
def _clean_tables(monkeypatch):
    monkeypatch.setenv("GRAPE_PIPELINE_MODE", "legacy")
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "events", "devops_agent_session", "agent_runtimes",
                  "runtime_credentials", "devops_pipeline_scene", "unified_user_mapping",
                  "user_repos", "engine_capabilities", "task_messages"):
            c.execute(f"DELETE FROM {t}")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "events", "devops_agent_session", "agent_runtimes",
                  "runtime_credentials", "devops_pipeline_scene", "unified_user_mapping",
                  "user_repos", "engine_capabilities", "task_messages"):
            c.execute(f"DELETE FROM {t}")
        db.commit()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mk_cred(name, scope="global", owner_id=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h" + name, name, "dev-" + name, scope, owner_id, "tester"))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, *, credential_id, status="online", healthy=1, installed=1,
               scope="global", owner_id="", last_seen=None) -> int:
    ls = last_seen if last_seen is not None else _now()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, name, credential_id, status, healthy, installed, ls, scope, owner_id))
        rid = c.lastrowid
        c.execute(
            "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'all', 1)",
            (rid,))
        db.commit()
        return rid


def _mk_scene(scene_id, agent_type, engine_ids, user_id=1, repo_key=""):
    with db_lock:
        c = db.cursor()
        try:
            c.execute("INSERT OR IGNORE INTO user_repos (user_id, repo_key, owner, name, is_join) VALUES (?, 'gitcode:owner/repo', 'owner', 'repo', 1)", (user_id,))
        except Exception:
            pass
        db.commit()
    nodes = [{"id": agent_type, "tag": agent_type, "skill": "test-skill",
              "mode": "emit", "next": "done", "handoff": "complete",
              "engine_runtime_ids": engine_ids}]
    from src.api.pipeline_config import create_custom_scene
    _rk = repo_key or "owner/repo"
    _rk = _rk.split(":")[-1] if ":" in _rk else _rk
    create_custom_scene(scene_id=scene_id, name=scene_id, description="",
                        repo_key=_rk, nodes=nodes)


def _stale_ts(seconds_ago):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=seconds_ago)).isoformat()


class TestScenePoolHealthFilter:
    """Issue #259 核心改动：get_scene_pool_engine_ids 健康过滤。"""

    def test_offline_engine_filtered_out(self):
        """离线引擎不进 pool，在线引擎保留。"""
        ok_eng = _mk_engine("opencode-ok", credential_id=_mk_cred("c-ok"))
        dead_eng = _mk_engine("opencode-dead", credential_id=_mk_cred("c-dead"), status="offline")
        _mk_scene("sc259-a", "vod", [ok_eng, dead_eng])
        result = es.get_scene_pool_engine_ids("sc259-a", "vod", repo_key="owner/repo", user_id=1)
        assert result is not None and json.loads(result) == [ok_eng]

    def test_stale_heartbeat_filtered_out(self):
        """status=online 但心跳过期（刚断线窗口期）→ 不进 pool。"""
        ok_eng = _mk_engine("opencode-ok2", credential_id=_mk_cred("c-ok2"))
        # 心跳 400s 前（阈值 100s）——status 仍是 online（reap 还没翻牌的窗口期）
        stale_eng = _mk_engine("opencode-stale", credential_id=_mk_cred("c-stale"),
                               status="online", last_seen=_stale_ts(400))
        _mk_scene("sc259-b", "vod", [ok_eng, stale_eng])
        result = es.get_scene_pool_engine_ids("sc259-b", "vod", repo_key="owner/repo", user_id=1)
        assert result is not None and json.loads(result) == [ok_eng]

    def test_all_offline_returns_none(self):
        """全灭 → None（方案A：调用方报错，不放开广播）。"""
        dead1 = _mk_engine("opencode-d1", credential_id=_mk_cred("c-d1"), status="offline")
        dead2 = _mk_engine("opencode-d2", credential_id=_mk_cred("c-d2"),
                           status="online", last_seen=_stale_ts(999))
        _mk_scene("sc259-c", "vod", [dead1, dead2])
        result = es.get_scene_pool_engine_ids("sc259-c", "vod", repo_key="owner/repo", user_id=1)
        assert result is None

    def test_no_config_returns_empty_string(self):
        """没配引擎 → ""（全量广播，原行为不变）。必须与 None 区分。"""
        _mk_scene("sc259-d", "vod", [])
        result = es.get_scene_pool_engine_ids("sc259-d", "vod", repo_key="owner/repo", user_id=1)
        assert result == ""

    def test_fresh_heartbeat_kept(self):
        """心跳在阈值内（90s < 100s）→ 保留。"""
        fresh = _mk_engine("opencode-fresh", credential_id=_mk_cred("c-fresh"),
                           last_seen=_stale_ts(90))
        _mk_scene("sc259-e", "vod", [fresh])
        result = es.get_scene_pool_engine_ids("sc259-e", "vod", repo_key="owner/repo", user_id=1)
        assert result is not None and json.loads(result) == [fresh]


class TestCheckPoolEnginesReachable:
    """Issue #259：兜底判定加心跳新鲜度。"""

    def test_stale_heartbeat_not_reachable(self):
        """status=online + 心跳过期 → 不可达（原 bug：只看 status 会误判可达）。"""
        eng = _mk_engine("opencode-r1", credential_id=_mk_cred("c-r1"),
                         status="online", last_seen=_stale_ts(400))
        assert svc._check_pool_engines_reachable([eng], "vod", "owner/repo") is False

    def test_null_heartbeat_not_reachable(self):
        """status=online + last_seen_at NULL → 不可达（幽灵行）。"""
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_runtimes SET last_seen_at=NULL WHERE id=?", (eng_id := _mk_engine("opencode-r2", credential_id=_mk_cred("c-r2")),))
            db.commit()
        assert svc._check_pool_engines_reachable([eng_id], "vod", "owner/repo") is False

    def test_fresh_reachable(self):
        eng = _mk_engine("opencode-r3", credential_id=_mk_cred("c-r3"))
        assert svc._check_pool_engines_reachable([eng], "vod", "owner/repo") is True


class TestReapStaleRuntimes:
    """Issue #259：reap NULL 盲区 + dispatched/running 回收分级。"""

    def _mk_task(self, runtime_id, status, started_at=None, created=None):
        import uuid as _uuid
        tid = f"t-{runtime_id}-{status}-{_uuid.uuid4().hex[:6]}"
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_tasks (id, agent_session_id, prompt, repo_key, agent_type, skill, status, runtime_id, "
                "started_at, created_at, event_seq) "
                "VALUES (?, 'sess-259', 'p', 'owner/repo', 'vod', 's', ?, ?, ?, ?, 0)",
                (tid, status, runtime_id,
                 started_at or _now(), created or _now()))
            db.commit()
            return tid

    def test_null_last_seen_reaped(self):
        """NULL 心跳 + status=online 的幽灵行也被标 offline（原查询漏掉）。"""
        with db_lock:
            c = db.cursor()
            cid = _mk_cred("c-null")
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
                "VALUES ('ghost', 'ghost', ?, 'online', 1, 1, NULL, 'global', '')", (cid,))
            rid = c.lastrowid
            c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'all', 1)", (rid,))
            db.commit()
        svc.reap_stale_runtimes()
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status FROM agent_runtimes WHERE id=?", (rid,))
            assert c.fetchone()[0] == "offline"

    def test_dispatched_recovered_fast_running_not(self):
        """心跳过期 → dispatched 立即回收；running 未满 3600s 不回收（防双跑）。"""
        eng = _mk_engine("opencode-rc", credential_id=_mk_cred("c-rc"),
                         status="online", last_seen=_stale_ts(400))
        t_disp = self._mk_task(eng, "dispatched")
        t_run_fresh = self._mk_task(eng, "running", started_at=_now())  # 刚开始跑
        t_run_old = self._mk_task(eng, "running",
                                  started_at=(datetime.datetime.now(datetime.timezone.utc)
                                              - datetime.timedelta(seconds=3700)).isoformat())  # 超 3600s
        svc.reap_stale_runtimes()
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status FROM agent_tasks WHERE id=?", (t_disp,))
            assert c.fetchone()[0] == "queued"      # dispatched → 快速回收
            c.execute("SELECT status FROM agent_tasks WHERE id=?", (t_run_fresh,))
            assert c.fetchone()[0] == "running"     # running 未超时 → 保留（防双跑）
            c.execute("SELECT status FROM agent_tasks WHERE id=?", (t_run_old,))
            assert c.fetchone()[0] == "queued"      # running 超过 3600s → 回收重派
            c.execute("SELECT status FROM agent_runtimes WHERE id=?", (eng,))
            assert c.fetchone()[0] == "offline"     # 引擎被标 offline


class TestDirectDispatchStaleCheck:
    """Issue #259：pipeline_executor 直选定向加心跳检查（通过 _enqueue_via_pipeline 验证降级）。"""

    def test_stale_direct_dropped_to_pool(self):
        """runtime_id 心跳过期 → 降级为池任务（runtime_id=0 广播）。"""
        from src.api.pipeline_executor import _enqueue_via_pipeline
        stale_eng = _mk_engine("opencode-dd", credential_id=_mk_cred("c-dd"),
                               status="online", last_seen=_stale_ts(400))
        evt = {"repo_key": "owner/repo", "issue_id": "#1", "payload": {}, "actor": "",
               "repository": "owner/repo", "source": "gitcode"}
        # legacy 模式（非 plugin）：runtime_id 传入过期引擎 → 应降级 0 后走 pick_engine
        import os
        os.environ["GRAPE_PIPELINE_MODE"] = "legacy"
        result = _enqueue_via_pipeline("owner/repo", 1, "vod", "test-skill", evt,
                                        stale_eng, "#1", "vod")
        # legacy + runtime 降级 + pick_engine 无候选 → 应返回 error（no engine available）
        # 而不是把任务钉在死引擎上
        assert result.get("error") == "no engine available"

    def test_fresh_direct_kept(self):
        """runtime_id 心跳新鲜 → 正常定向（不降级）。"""
        from src.api.pipeline_executor import _enqueue_via_pipeline
        fresh_eng = _mk_engine("opencode-df", credential_id=_mk_cred("c-df"))
        evt = {"repo_key": "owner/repo", "issue_id": "#2", "payload": {}, "actor": "",
               "repository": "owner/repo", "source": "gitcode"}
        import os
        os.environ["GRAPE_PIPELINE_MODE"] = "legacy"
        result = _enqueue_via_pipeline("owner/repo", 2, "vod", "test-skill", evt,
                                        fresh_eng, "#2", "vod")
        # 正常入队：无 error；且任务 runtime_id 保持定向
        assert not result.get("error"), result
        with db_lock:
            c = db.cursor()
            c.execute("SELECT runtime_id, status FROM agent_tasks WHERE event_seq=2 ORDER BY created_at DESC")
            row = c.fetchone()
            assert row is not None, "task row not found"
            assert row[0] == fresh_eng
