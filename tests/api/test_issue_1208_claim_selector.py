"""Issue #1208 原子契约测试：claim 六层守卫（selector 三级）+ uid 轨空值不误杀。

覆盖（Issue 正文 §2.4 / §3 验收 3/4/5 / §4 实施顺序 6-7）：
- 六层守卫顺序：①能力（tag 精确 OR 'all'）→ ②角色（#658）→ ③selector（本方案）→
  ④pool 名单 → ⑤uid 池 → ⑥scope 隔离；层级日志前缀 `claim guard [selector]`；
- claim_by_tag FIFO：selector 不匹配任务跳过不阻塞队列（继续下一个）；
- bound claim（expected）：selector 不匹配 fail-closed（不尝试其他任务）；
- 验收 4：selector 任务 pool_engine_ids/pool_engine_uids 均空 → 守卫放行（不误杀）；
- _check_pool_selector_reachable：selector 可达性 = 有引擎满足 selector 且物理可达；
- _check_stale_queued_tasks：selector 任务超时降级（配置死即败 / 瞬时死文案）；
- #658 正交回归：selector 命中但角色不符仍拒（验收 3）。
"""
import json
import threading

import pytest


@pytest.fixture()
def claim_db(monkeypatch):
    """独立测试库（同 tests/test_issue_253_engine_binding.py setup_db 口径 + pool_selector 列）。"""
    import sqlite3
    from src.domains.runtime.engine import service as svc
    test_db = sqlite3.connect(":memory:", check_same_thread=False)
    test_db.execute("PRAGMA foreign_keys=OFF")
    test_db.executescript("""
    CREATE TABLE agent_tasks (
        id TEXT PRIMARY KEY, agent_session_id TEXT DEFAULT '', runtime_id INTEGER DEFAULT 0,
        event_seq INTEGER, issue_id TEXT DEFAULT '', repo_key TEXT DEFAULT '',
        agent_type TEXT DEFAULT '', skill TEXT DEFAULT '', skill_id INTEGER DEFAULT 0,
        prompt TEXT DEFAULT '', cwd TEXT DEFAULT '', runtime_session_id TEXT DEFAULT '',
        event_type TEXT DEFAULT '', account TEXT DEFAULT '', user_id INTEGER DEFAULT 0,
        repo_gitcode_token TEXT DEFAULT '', env_extra TEXT DEFAULT '{}',
        skill_files TEXT DEFAULT '{}', server_url TEXT DEFAULT '',
        issue_session_id TEXT DEFAULT '',
        project_info_files TEXT DEFAULT '{}', project_info_meta TEXT DEFAULT '{}',
        knowledge_files TEXT DEFAULT '{}', project_info_file_urls TEXT DEFAULT '{}',
        knowledge_file_urls TEXT DEFAULT '{}', attempts INTEGER DEFAULT 0,
        pool_agent_type TEXT DEFAULT '', affinity_runtime_id INTEGER DEFAULT 0,
        pool_tag TEXT DEFAULT '', pool_engine_ids TEXT DEFAULT '',
        pool_engine_uids TEXT DEFAULT '', pool_selector TEXT,
        status TEXT DEFAULT 'queued', stream_ended INTEGER DEFAULT 0, self_reported INTEGER DEFAULT 0,
        exit_code INTEGER, result_text TEXT DEFAULT '', errors TEXT DEFAULT '', stderr TEXT DEFAULT '',
        usage_json TEXT DEFAULT '{}', started_at TEXT, completed_at TEXT, created_at TEXT,
        user_gitcode_token TEXT DEFAULT '', pool_engine_source TEXT DEFAULT ''
    );
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT, status TEXT DEFAULT 'offline',
        healthy INTEGER DEFAULT 0, installed INTEGER DEFAULT 0, credential_id INTEGER DEFAULT 0,
        last_seen_at TEXT, scope TEXT DEFAULT '', owner_id TEXT DEFAULT '',
        busy_slots INTEGER DEFAULT 0, max_concurrent INTEGER DEFAULT 4,
        probe_status TEXT DEFAULT '', role_tags TEXT DEFAULT '',
        engine_uid VARCHAR(36) DEFAULT NULL,
        -- [迁移适配] 目标线 claim 查询新增云沙箱路由列（i7 沙箱域移植），测试库补齐
        provider TEXT DEFAULT '', runtime_type TEXT DEFAULT '', runtime_info TEXT DEFAULT ''
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY, scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '', device_name TEXT DEFAULT '', status TEXT DEFAULT 'active'
    );
    CREATE TABLE engine_capabilities (
        runtime_id INTEGER, tag TEXT, max_concurrent INTEGER DEFAULT 4,
        priority INTEGER DEFAULT 50, enabled INTEGER DEFAULT 1,
        created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '',
        engine_uid VARCHAR(36) DEFAULT NULL
    );
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY, repo_key TEXT, event_type TEXT, source TEXT, issue_id TEXT,
        title TEXT, summary TEXT, actor TEXT, payload TEXT DEFAULT '', agent_type TEXT,
        runtime TEXT, skill TEXT, status TEXT, exit_code INTEGER, duration REAL,
        agent_logs TEXT, result TEXT, structured_output TEXT,
        created_at TEXT, started_at TEXT, completed_at TEXT, entity_type TEXT DEFAULT ''
    );
    CREATE TABLE task_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, created_at TEXT, content TEXT
    );
    """)
    monkeypatch.setattr(svc, "db", test_db)
    monkeypatch.setattr(svc, "db_lock", threading.RLock())
    import src.kernel.session_manager as _sm
    import src.api.event_store as _es
    monkeypatch.setattr(_sm, "db", test_db)
    monkeypatch.setattr(_sm, "db_lock", threading.RLock())
    monkeypatch.setattr(_es, "db", test_db)
    monkeypatch.setattr(_es, "db_lock", threading.RLock())
    # 列缓存按 db 身份失效（_table_cols_cache 机制）
    yield test_db
    test_db.close()


def _mk_engine(c, rid, name="opencode", status="online", healthy=1, cred=63, seen="now",
               tag="coding", probe="ok", role="", uid=None, derived=None):
    """derived：派生标签列表（engine:/scope:/scene: 冒号行）——能力表平铺 + 派生同表。"""
    import datetime as _dt
    if seen == "now":
        seen = _dt.datetime.now(_dt.timezone.utc).isoformat()
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, installed, credential_id, "
              "last_seen_at, probe_status, role_tags, engine_uid) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (rid, name, status, healthy, 1, cred, seen, probe, role, uid))
    c.execute("INSERT INTO runtime_credentials (id, scope, owner_id, owner_name, status) "
              "VALUES (?, 'global', '', '', 'active')", (cred,))
    if tag:
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)", (rid, tag))
    for t in (derived or []):
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)", (rid, t))


def _mk_task(c, tid="t_sel1", tag="coding", pool_selector=None, pool_engine_ids="",
             pool_engine_uids="", repo="o/r", uid=0, created="2020-01-01T00:00:00+00:00"):
    import datetime as _dt
    created = created or _dt.datetime.now(_dt.timezone.utc).isoformat()
    c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, pool_tag, pool_engine_ids, "
              "pool_engine_uids, pool_selector, repo_key, user_id, status, created_at) "
              "VALUES (?,?,0,?,?,?,?,?,?,'queued',?)",
              (tid, "s1", tag, pool_engine_ids, pool_engine_uids,
               json.dumps(pool_selector, ensure_ascii=False) if pool_selector else None,
               repo, uid, created))


class TestClaimSelectorGuard:
    def test_selector_matched_claim_ok(self, claim_db):
        # selector 命中 → 正常认领（验收 4 后半：守卫放行不误杀）
        from src.domains.runtime.engine import service as svc
        _mk_engine(claim_db, 9, tag="coding", derived=["scope:repo", "engine:opencode"])
        _mk_task(claim_db, pool_selector={"tags": ["scope:repo"]})
        claim_db.commit()
        r = svc.claim_by_tag("coding", 9)
        assert r and r["id"] == "t_sel1"

    def test_selector_mismatch_skips_to_next(self, claim_db):
        # FIFO：队首 selector 不匹配 → 跳过不阻塞，认领后续匹配任务
        from src.domains.runtime.engine import service as svc
        _mk_engine(claim_db, 9, tag="coding", derived=["scope:global", "engine:opencode"])
        _mk_task(claim_db, "t_bad", pool_selector={"tags": ["scope:repo"]})
        _mk_task(claim_db, "t_ok", pool_selector={"tags": ["scope:global"]})
        claim_db.commit()
        r = svc.claim_by_tag("coding", 9)
        assert r and r["id"] == "t_ok"

    def test_bound_claim_selector_mismatch_fail_closed(self, claim_db):
        # bound claim（expected）：selector 不匹配 → None（fail-closed，不尝试其他任务）
        from src.domains.runtime.engine import service as svc
        _mk_engine(claim_db, 9, tag="coding", derived=["scope:global"])
        _mk_task(claim_db, "t_bad", pool_selector={"tags": ["scope:repo"]})
        claim_db.execute("UPDATE agent_tasks SET event_seq=777 WHERE id='t_bad'")
        claim_db.commit()
        r = svc.claim_by_tag("coding", 9, expected_task_id="t_bad", expected_event_seq=777)
        assert r is None
        st = claim_db.execute("SELECT status FROM agent_tasks WHERE id='t_bad'").fetchone()[0]
        assert st == "queued"  # 未被抢走

    def test_uid_track_empty_pools_not_killed(self, claim_db):
        # 验收 4：selector 任务 pool_engine_ids/uids 均空 → claim 守卫空值放行（不误杀）
        from src.domains.runtime.engine import service as svc
        _mk_engine(claim_db, 9, tag="coding", derived=["scope:repo"])
        _mk_task(claim_db, "t_sel", pool_selector={"tags": ["scope:repo"]})
        claim_db.commit()
        row = claim_db.execute("SELECT pool_engine_ids, pool_engine_uids, pool_selector "
                               "FROM agent_tasks WHERE id='t_sel'").fetchone()
        assert (row[0] or "") == "" and (row[1] or "") == "" and row[2]
        r = svc.claim_by_tag("coding", 9)
        assert r and r["id"] == "t_sel"

    def test_role_guard_before_selector(self, claim_db):
        # #658 正交回归（验收 3）：selector 命中但角色不符仍拒（角色在 selector 之前）
        from src.domains.runtime.engine import service as svc
        _mk_engine(claim_db, 9, tag="coding", role='["coding"]', derived=["scope:repo"])
        _mk_task(claim_db, "t_sre", tag="sre", pool_selector={"tags": ["scope:repo"]})
        claim_db.commit()
        r = svc.claim_by_tag("sre", 9)
        assert r is None
        st = claim_db.execute("SELECT status FROM agent_tasks WHERE id='t_sre'").fetchone()[0]
        assert st == "queued"

    def test_capability_first(self, claim_db):
        # 第 1 级能力守卫仍最前：无 tag 能力 → 认领不到（不进 selector 层）
        from src.domains.runtime.engine import service as svc
        _mk_engine(claim_db, 9, tag="coding", derived=["scope:repo"])
        _mk_task(claim_db, "t_qa", tag="qa", pool_selector={"tags": ["scope:repo"]})
        claim_db.commit()
        assert svc.claim_by_tag("qa", 9) is None


class TestSelectorReachability:
    def test_reachable_and_unreachable(self, claim_db):
        from src.domains.runtime.engine import service as svc
        _mk_engine(claim_db, 9, cred=63, tag="coding", derived=["scope:repo"], status="online")
        _mk_engine(claim_db, 10, cred=64, tag="coding", derived=["scope:repo"], status="offline")
        claim_db.commit()
        sel = {"tags": ["scope:repo"]}
        assert svc._check_pool_selector_reachable(sel, "coding", "o/r") is True
        assert svc._check_pool_selector_reachable({"tags": ["scope:user"]}, "coding", "o/r") is False


class TestStaleQueuedSelector:
    def test_config_dead_fails_immediately(self, claim_db, monkeypatch):
        # 配置死（全库无引擎带该标签）：stale 超时收敛 → failed + 配置死文案
        from src.domains.runtime.engine import service as svc
        import datetime as _dt
        _old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=9999)).isoformat()
        _mk_task(claim_db, "t_dead", pool_selector={"tags": ["engine:codearts"]}, created=_old)
        claim_db.commit()
        monkeypatch.setattr(svc, "_STALE_QUEUED_THRESHOLD", 1)
        svc._check_stale_queued_tasks()
        row = claim_db.execute("SELECT status, errors FROM agent_tasks WHERE id='t_dead'").fetchone()
        assert row[0] == "failed"
        assert "无引擎满足标签" in (row[1] or "")

    def test_transient_dead_message(self, claim_db, monkeypatch):
        # 瞬时死（标签有引擎带但全离线）：stale 超时收敛 → failed + 瞬时死文案
        from src.domains.runtime.engine import service as svc
        import datetime as _dt
        _old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=9999)).isoformat()
        _mk_engine(claim_db, 9, tag="coding", derived=["scope:repo"], status="offline")
        _mk_task(claim_db, "t_tr", pool_selector={"tags": ["scope:repo"]}, created=_old)
        claim_db.commit()
        monkeypatch.setattr(svc, "_STALE_QUEUED_THRESHOLD", 1)
        svc._check_stale_queued_tasks()
        row = claim_db.execute("SELECT status, errors FROM agent_tasks WHERE id='t_tr'").fetchone()
        assert row[0] == "failed"
        assert "均不在线" in (row[1] or "") or "稍后" in (row[1] or "")
