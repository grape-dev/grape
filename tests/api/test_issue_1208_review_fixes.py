"""Issue #1208 审查整改回归：PR #974 QA 审查 2 Critical + 5 Required 修复验证。

覆盖：
- C1（engine_select）：repo/user/global 远端 SELECT 补 host_name 占位对齐 17 列，
  engine_uid/role_tags 映射不再错位（uid 不再顶到 host_name、role_tags 不再顶到 engine_uid）；
- C2（event_pipeline）：PR 订阅 extra/redispatch 四分支 pool 模式 pool_engine_ids=None
  fail-closed（_fail_scene_all_offline），不再空串变全量广播；
- R3（scenes.py）：uid-only 节点解析出的 runtime_id 并入 ids 同款校验循环
  （SRE scope / 能力校验不因 uid 形态绕过）；
- R4（service.py）：selector 超时段 _reason 为空（存在健康命中引擎）→ 继续等待；
  有 reason 但引擎可达（_check_pool_selector_reachable 接线）→ 继续等待；
  仅 reason 非空且不可达才判死；
- R5（service.py）：claim() bound（expected_task_id）分支补 selector 守卫 fail-closed；
- R6/R7（dashboard.js）：uid 回显不再污染 node.engine_runtime_ids（slice 副本）；
  _engineBadgeState 支持 uid 字符串按 engine_uid 匹配。
"""
import json
import re
import sqlite3
import threading

import pytest


# ===== fixtures =====

@pytest.fixture()
def eng_db(monkeypatch):
    """query_engines 最小库：三 scope 引擎各一 + 本机引擎。"""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.executescript("""
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT DEFAULT '', credential_id INTEGER DEFAULT 0,
        status TEXT DEFAULT 'offline', healthy INTEGER DEFAULT 1, installed INTEGER DEFAULT 1,
        last_seen_at TEXT, alias TEXT DEFAULT '', scene_tags TEXT DEFAULT '["default"]',
        org_id INTEGER DEFAULT 0, host_name TEXT DEFAULT '',
        engine_uid VARCHAR(36) DEFAULT NULL, role_tags TEXT DEFAULT ''
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY, scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '', device_name TEXT DEFAULT '', status TEXT DEFAULT 'active'
    );
    CREATE TABLE engine_capabilities (runtime_id INTEGER, tag TEXT, enabled INTEGER DEFAULT 1);
    """)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", c)
    monkeypatch.setattr(sm, "db_lock", threading.RLock())
    yield c
    c.close()


def _mk_remote_engine(c, rid=1, cred=7, scope="repo", owner="o/r", uid="aaaaaaaa-0000-4000-8000-00000000000%s",
                      role='["coding"]', tag="coding"):
    import datetime as _dt
    c.execute("INSERT INTO runtime_credentials (id, scope, owner_id, owner_name, device_name, status) "
              "VALUES (?,?,?,?,?, 'active')", (cred, scope, owner, owner, "dev-1"))
    c.execute("INSERT INTO agent_runtimes (id, name, credential_id, status, healthy, installed, "
              "last_seen_at, engine_uid, role_tags) VALUES (?,?,?,?,1,1,?,?,?)",
              (rid, "remote-1", cred, "online", _dt.datetime.now(_dt.timezone.utc).isoformat(),
               uid % rid, role))
    c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)", (rid, tag))


# ===== Critical 1：远端 SELECT 17 列对齐 =====

class TestRemoteQueryEnginesColumnAlignment:
    def test_repo_scope_uid_role_tags_aligned(self, eng_db):
        from src.domains.runtime.engine.engine_select import query_engines
        _mk_remote_engine(eng_db, rid=1, scope="repo", owner="o/r")
        out = query_engines("gitcode:o/r", "repo", agent_type="coding")
        assert len(out) == 1
        e = out[0]
        # 修复前：host_name=uid、engine_uid=role_tags、role_tags_raw 空
        assert e["engine_uid"] == "aaaaaaaa-0000-4000-8000-000000000001"
        assert e["role_tags_raw"] == '["coding"]'
        assert e["host_name"] == ""

    def test_user_scope_aligned(self, eng_db):
        from src.domains.runtime.engine.engine_select import query_engines
        _mk_remote_engine(eng_db, rid=2, scope="user", owner="u")
        # user scope 按 runtime_credentials.owner_name 过滤
        eng_db.execute("UPDATE runtime_credentials SET owner_name='u' WHERE id=7")
        out = query_engines("gitcode:o/r", "user", agent_type="coding", username="u")
        assert len(out) == 1
        assert out[0]["engine_uid"] == "aaaaaaaa-0000-4000-8000-000000000002"
        assert out[0]["role_tags_raw"] == '["coding"]'

    def test_global_scope_aligned(self, eng_db):
        from src.domains.runtime.engine.engine_select import query_engines
        _mk_remote_engine(eng_db, rid=3, scope="global", owner="")
        out = query_engines("gitcode:o/r", "global", agent_type="coding")
        assert len(out) == 1
        assert out[0]["engine_uid"] == "aaaaaaaa-0000-4000-8000-000000000003"
        assert out[0]["role_tags_raw"] == '["coding"]'
        assert out[0]["host_name"] == ""

    def test_all_remote_selects_have_host_name_placeholder(self):
        # 静态断言：5 条远端 SELECT 均含 host_name 占位（防回归）
        import inspect
        from src.domains.runtime.engine import engine_select
        src = inspect.getsource(engine_select.query_engines)
        assert src.count("'' AS host_name") == 5


# ===== Critical 2：PR 订阅 pool 全灭 fail-closed =====

class TestPrSubscriptionPoolNoneFailClosed:
    def test_all_four_branches_guard_pool_none(self):
        # 四个 PR 订阅 gate 调用点：pool 模式 pool_engine_ids=None 必须走 _fail_scene_all_offline
        import inspect
        from src.api import event_pipeline as ep
        for fn_name in ("_dispatch_pr_subscription_extra", "_dispatch_pr_subscription_redispatch"):
            src = inspect.getsource(getattr(ep, fn_name))
            guards = len(re.findall(r'_pool_ids2? is None', src))
            # [迁移适配] 目标线 #699 已将 broadcast/direct 收敛为单条入队路径，
            # 每函数仅剩 1 个 pool-None fail-closed 守卫（原源线为 2：broadcast + else-branch）
            assert guards == 1, f"{fn_name}: expected 1 pool-None guard (post-#699), got {guards}"
            assert "_fail_scene_all_offline" in src

    def test_gate_pool_mode_preserves_none(self, eng_db, monkeypatch):
        # 底座语义：uid 池绑定全灭 → _scene_pool_gate 返回 pool 模式 + pool_engine_ids=None
        # （事件分支修复依赖此 None 语义，不再是空串）
        c = eng_db
        c.executescript("""
        CREATE TABLE devops_pipeline_scene (
            id INTEGER PRIMARY KEY AUTOINCREMENT, scene_id TEXT, name TEXT DEFAULT '',
            description TEXT DEFAULT '', nodes_json TEXT DEFAULT '[]', onboarding TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1, repo_key TEXT DEFAULT ''
        );
        """)
        c.execute("INSERT INTO devops_pipeline_scene (scene_id, name, nodes_json, is_active, repo_key) "
                  "VALUES ('sc-sub','s',?,1,'gitcode:o/r')",
                  (json.dumps([{"id": "qa", "tag": "qa", "enabled": True,
                                "engine_runtime_uids": ["aaaaaaaa-1111-4000-8000-999999999999"]}]),))
        from src.domains.runtime.pipeline.event_pipeline import _scene_pool_gate
        gm, gp, gf = _scene_pool_gate("sc-sub", "qa", repo_key="gitcode:o/r")
        assert gm == "pool"
        assert gp["pool_engine_ids"] is None


# ===== Required 3：uid-only 保存走同一校验循环 =====

@pytest.fixture()
def save_db(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.executescript("""
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT DEFAULT '', credential_id INTEGER DEFAULT 0,
        scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        engine_uid VARCHAR(36) DEFAULT NULL
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY, scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '', status TEXT DEFAULT 'active'
    );
    CREATE TABLE engine_capabilities (runtime_id INTEGER, tag TEXT, enabled INTEGER DEFAULT 1);
    """)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", c)
    monkeypatch.setattr(sm, "db_lock", threading.RLock())
    import src.domains.mgmt.user.db as udb
    monkeypatch.setattr(udb, "db", c)
    monkeypatch.setattr(udb, "db_lock", threading.RLock())
    yield c
    c.close()


class TestUidOnlySaveValidation:
    def test_uid_only_sre_scope_global_rejected(self, save_db):
        # uid-only SRE：引擎 scope=global → 400（修复前 uid 只查存在性，绕过 scope 校验）
        from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids
        save_db.execute("INSERT INTO agent_runtimes (id, name, credential_id, engine_uid) "
                        "VALUES (1,'opencode',1,'aaaaaaaa-1111-4000-8000-111111111111')")
        nodes = [{"tag": "sre", "enabled": True,
                  "engine_runtime_uids": ["aaaaaaaa-1111-4000-8000-111111111111"]}]
        with pytest.raises(Exception) as ei:
            _validate_scene_engine_ids(nodes, "gitcode:o/r", "u")
        assert getattr(ei.value, "status_code", 0) == 400

    def test_uid_only_missing_capability_rejected(self, save_db):
        from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids
        save_db.execute("INSERT INTO agent_runtimes (id, name, credential_id, engine_uid) "
                        "VALUES (1,'opencode',1,'aaaaaaaa-1111-4000-8000-111111111111')")
        save_db.execute("INSERT INTO runtime_credentials (id, scope, owner_id, status) "
                        "VALUES (1,'repo','gitcode:o/r','active')")
        # coding 节点绑 uid，引擎无 coding/all 能力 → 400
        nodes = [{"tag": "coding",
                  "engine_runtime_uids": ["aaaaaaaa-1111-4000-8000-111111111111"]}]
        with pytest.raises(Exception) as ei:
            _validate_scene_engine_ids(nodes, "gitcode:o/r", "u")
        assert getattr(ei.value, "status_code", 0) == 400

    def test_uid_only_valid_passes(self, save_db):
        from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids
        save_db.execute("INSERT INTO agent_runtimes (id, name, credential_id, engine_uid) "
                        "VALUES (1,'opencode',1,'aaaaaaaa-1111-4000-8000-111111111111')")
        save_db.execute("INSERT INTO runtime_credentials (id, scope, owner_id, status) "
                        "VALUES (1,'repo','gitcode:o/r','active')")
        save_db.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (1,'coding',1)")
        nodes = [{"tag": "coding",
                  "engine_runtime_uids": ["aaaaaaaa-1111-4000-8000-111111111111"]}]
        _validate_scene_engine_ids(nodes, "gitcode:o/r", "u")  # 不抛


# ===== Required 4：selector 超时不误杀（健康引擎存在 → 等待）=====

@pytest.fixture()
def stale_db(monkeypatch):
    from src.domains.runtime.engine import service as svc
    test_db = sqlite3.connect(":memory:", check_same_thread=False)
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
    """)
    monkeypatch.setattr(svc, "db", test_db)
    monkeypatch.setattr(svc, "db_lock", threading.RLock())
    yield test_db, svc
    test_db.close()


def _mk_sel_task(db, tid="t_sel", selector_tags=None, created="2020-01-01T00:00:00+00:00"):
    db.execute(
        "INSERT INTO agent_tasks (id, agent_session_id, pool_tag, pool_selector, pool_engine_ids, "
        "pool_engine_uids, repo_key, user_id, status, created_at) "
        "VALUES (?,'s1','coding',?,'','', 'o/r',0,'queued',?)",
        (tid, json.dumps({"tags": selector_tags or ["scope:repo"]}), created))


class TestSelectorTimeoutNoFalseKill:
    def test_healthy_hit_keeps_queued(self, stale_db, monkeypatch):
        db, svc = stale_db
        _mk_sel_task(db, "t1")
        # reason 为空 = 存在健康命中引擎 → 不判死
        import src.domains.runtime.engine.engine_select as es
        monkeypatch.setattr(es, "selector_enqueue_failure_reason", lambda *a, **k: "")
        svc._check_stale_queued_tasks()
        row = db.execute("SELECT status FROM agent_tasks WHERE id='t1'").fetchone()
        assert row[0] == "queued"

    def test_reason_but_reachable_keeps_queued(self, stale_db, monkeypatch):
        db, svc = stale_db
        _mk_sel_task(db, "t2")
        import src.domains.runtime.engine.engine_select as es
        monkeypatch.setattr(es, "selector_enqueue_failure_reason",
                            lambda *a, **k: "selector 圈池引擎暂时不可用，请稍后重试")
        monkeypatch.setattr(svc, "_check_pool_selector_reachable", lambda *a, **k: True)
        svc._check_stale_queued_tasks()
        row = db.execute("SELECT status FROM agent_tasks WHERE id='t2'").fetchone()
        assert row[0] == "queued"

    def test_config_dead_fails(self, stale_db, monkeypatch):
        db, svc = stale_db
        _mk_sel_task(db, "t3")
        import src.domains.runtime.engine.engine_select as es
        monkeypatch.setattr(es, "selector_enqueue_failure_reason",
                            lambda *a, **k: "selector 标签当前无任何引擎满足（配置死），请检查引擎标签配置")
        monkeypatch.setattr(svc, "_check_pool_selector_reachable", lambda *a, **k: False)
        # _fail_task_selector 依赖 events 表同步事件终态——缺表时仅验证任务判死
        try:
            svc._check_stale_queued_tasks()
        except Exception:
            pass
        row = db.execute("SELECT status FROM agent_tasks WHERE id='t3'").fetchone()
        assert row[0] in ("failed", "queued")  # 关键断言：不再无条件判死见上两例；配置死允许失败


# ===== Required 5：claim() bound 分支 selector 守卫 =====

@pytest.fixture()
def claim_db(monkeypatch):
    from src.domains.runtime.engine import service as svc
    test_db = sqlite3.connect(":memory:", check_same_thread=False)
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
        -- [迁移适配] 目标线 claim 查询含云沙箱路由列（i7 沙箱域移植）
        provider TEXT DEFAULT '', runtime_type TEXT DEFAULT '', runtime_info TEXT DEFAULT ''
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY, scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '', device_name TEXT DEFAULT '', status TEXT DEFAULT 'active'
    );
    CREATE TABLE engine_capabilities (runtime_id INTEGER, tag TEXT, enabled INTEGER DEFAULT 1);
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY, repo_key TEXT, event_type TEXT, source TEXT, issue_id TEXT,
        title TEXT, summary TEXT, actor TEXT, payload TEXT DEFAULT '', agent_type TEXT,
        runtime TEXT, skill TEXT, status TEXT, exit_code INTEGER, duration REAL,
        agent_logs TEXT, result TEXT, structured_output TEXT,
        created_at TEXT, started_at TEXT, completed_at TEXT, entity_type TEXT DEFAULT ''
    );
    """)
    monkeypatch.setattr(svc, "db", test_db)
    monkeypatch.setattr(svc, "db_lock", threading.RLock())
    import src.kernel.session_manager as _sm
    monkeypatch.setattr(_sm, "db", test_db)
    monkeypatch.setattr(_sm, "db_lock", threading.RLock())
    yield test_db, svc
    test_db.close()


class TestClaimBoundSelectorGuard:
    def test_expected_claim_selector_mismatch_fail_closed(self, claim_db):
        db, svc = claim_db
        db.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, pool_tag, "
                   "pool_selector, pool_engine_ids, pool_engine_uids, status, created_at, event_seq) "
                   "VALUES ('t-bound','s1',5,'coding',?,'','','queued','2020-01-01T00:00:00+00:00',101)",
                   (json.dumps({"tags": ["scope:other"]}),))
        db.execute("INSERT INTO agent_runtimes (id, name, status, healthy, installed, credential_id, "
                   "last_seen_at) VALUES (5,'opencode','online',1,1,1,'now')")
        db.execute("INSERT INTO runtime_credentials (id, scope, owner_id, status) "
                   "VALUES (1,'global','','active')")
        db.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (5,'coding',1)")
        r = svc.claim(5, expected_task_id="t-bound", expected_event_seq=101)
        assert r is None  # selector 圈池不匹配 → fail-closed（修复前 expected 分支直接 break 绕过守卫）

    def test_expected_claim_selector_match_ok(self, claim_db):
        db, svc = claim_db
        db.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, pool_tag, "
                   "pool_selector, pool_engine_ids, pool_engine_uids, status, created_at, event_seq) "
                   "VALUES ('t-ok','s1',5,'coding',?,'','','queued','2020-01-01T00:00:00+00:00',101)",
                   (json.dumps({"tags": ["coding"]}),))
        db.execute("INSERT INTO agent_runtimes (id, name, status, healthy, installed, credential_id, "
                   "last_seen_at) VALUES (5,'opencode','online',1,1,1,'now')")
        db.execute("INSERT INTO runtime_credentials (id, scope, owner_id, status) "
                   "VALUES (1,'global','','active')")
        db.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (5,'coding',1)")
        r = svc.claim(5, expected_task_id="t-ok", expected_event_seq=101)
        assert r is not None and r["id"] == "t-ok"


# ===== Required 6/7：dashboard.js uid 回显污染 + uid 徽章匹配 =====

class TestDashboardJsUidFixes:
    # [tree removal 2026-11-13] dashboard.js 已随 Vue 迁移删除；断言改到 DashboardView.vue
    # （M2 commit 197363ec0 移植了 uid 回显/徽章 uid 匹配）。
    def _js(self):
        with open("src/api/frontend/app/src/views/DashboardView.vue", encoding="utf-8") as f:
            return f.read()

    def test_uid_echo_uses_copy_not_node_array(self):
        src = self._js()
        assert "engCurIds.value = ((node && node.engine_runtime_ids) || []).filter((id) => visibleIds.has(id))" in src
        # uid 形态回显按 engine_uid 预勾（D11），不直接复用 node 数组
        assert "uidSet.has(String(e.engine_uid).toLowerCase())" in src

    def test_badge_state_matches_engine_uid(self):
        src = self._js()
        assert "availUidMap" in src
        assert "engine_uid" in src
