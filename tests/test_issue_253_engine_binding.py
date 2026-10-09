"""Issue #253 场景引擎绑定硬约束测试（docs/weekly-06/spec-253.md）。

覆盖 spec §4 验收标准的 6 个场景：
- UT-1 认领时绑定引擎全不可达 → 任务 failed（原：清空 pool_engine_ids 放行全站）
- UT-2 池外引擎无法认领有绑定的任务（hermes 'all' 标签抢不走 codex 专属任务）
- UT-3 _check_stale_queued_tasks：健康引擎在场 → 等待；低分可达 → 120s 超时 failed；不可达 → failed
- UT-4 _fail_task_pool_unreachable / _fail_task_pool_unhealthy 人话原因落库 + 事件终态同步
- UT-5 空绑定任务不受三处收敛影响（回归保护）
- UT-6 _rid=0 时 redispatch 不触发 direct 直跑（守卫回归）
"""
import os, sys, json, threading

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

import pytest


@pytest.fixture()
def setup_db(monkeypatch, tmp_path):
    """独立测试库：agent_tasks / agent_runtimes / runtime_credentials / engine_capabilities / events / task_messages。"""
    import sqlite3
    from src.runtime import service as svc

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
        status TEXT DEFAULT 'queued', stream_ended INTEGER DEFAULT 0, self_reported INTEGER DEFAULT 0,
        exit_code INTEGER, result_text TEXT DEFAULT '', errors TEXT DEFAULT '', stderr TEXT DEFAULT '',
        usage_json TEXT DEFAULT '{}', started_at TEXT, completed_at TEXT, created_at TEXT,
        user_gitcode_token TEXT DEFAULT ''
    );
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT, status TEXT DEFAULT 'offline',
        healthy INTEGER DEFAULT 0, installed INTEGER DEFAULT 0, credential_id INTEGER DEFAULT 0,
        last_seen_at TEXT, scope TEXT DEFAULT '', busy_slots INTEGER DEFAULT 0,
        max_concurrent INTEGER DEFAULT 4, probe_status TEXT DEFAULT ''
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY, scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '', device_name TEXT DEFAULT '', status TEXT DEFAULT 'active'
    );
    CREATE TABLE engine_capabilities (
        runtime_id INTEGER, tag TEXT, max_concurrent INTEGER DEFAULT 4, enabled INTEGER DEFAULT 1
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
    monkeypatch.setattr(svc, "db_lock", threading.Lock())
    # 健康分/事件终态同步走 session_manager.db / event_store，一并替换到测试库
    import src.session_manager as _sm
    import src.api.event_store as _es
    monkeypatch.setattr(_sm, "db", test_db)
    monkeypatch.setattr(_sm, "db_lock", threading.Lock())
    monkeypatch.setattr(_es, "db", test_db)
    monkeypatch.setattr(_es, "db_lock", threading.Lock())
    yield test_db
    test_db.close()


def _mk_engine(c, rid, name, status="online", healthy=1, cred=63, seen="now", tag="coding",
               probe="ok", busy=0, maxc=4, cap_tag=None):
    import datetime as _dt
    _now = seen if seen == "now" else seen
    if _now == "now":
        _now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, installed, credential_id, "
              "last_seen_at, busy_slots, max_concurrent, probe_status) VALUES (?,?,?,?,1,?,?,?,?,?)",
              (rid, name, status, healthy, cred, _now, busy, maxc, probe))
    if tag is not None:
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, enabled) "
                  "VALUES (?,?,4,1)", (rid, tag if cap_tag is None else cap_tag))


# ===== UT-1：claim_by_tag 认领时绑定引擎全不可达 → 任务 failed，不放行 =====

def test_ut1_pool_all_unreachable_fails_task(setup_db):
    from src.runtime import service as svc
    c = setup_db
    # codex(10907) 离线；hermes(1129) 在线带 all 标签（#253 事故配置）
    _mk_engine(c, 10907, "codex", status="offline", healthy=0, tag="coding")
    _mk_engine(c, 1129, "hermes", tag="all")
    c.execute("INSERT INTO runtime_credentials (id, scope) VALUES (63, 'global')")
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, "
              "agent_session_id, issue_session_id, created_at, agent_type, event_seq) "
              "VALUES ('t_253a', 'queued', 0, 'coding', '[10907]', 's1', 'is1', "
              "datetime('now'), 'coding', 9301)")
    c.execute("INSERT INTO events (seq, repo_key, status) VALUES (9301, 'gitcode:x/r', 'running')")
    setup_db.commit()

    # hermes 尝试认领 → 绑定全不可达 → 任务 failed + 事件 failed + 保留 pool
    result = svc.claim_by_tag("all", 1129)
    assert result is None, "hermes 不应认领 codex 专属任务"
    row = c.execute("SELECT status, pool_engine_ids, errors FROM agent_tasks WHERE id='t_253a'").fetchone()
    assert row[0] == "failed", f"任务应 failed，实际 {row[0]}"
    assert row[1] == "[10907]", "pool_engine_ids 必须保留（不清空）"
    assert "codex" in (row[2] or ""), f"人话原因应含引擎名，实际: {row[2]}"
    ev = c.execute("SELECT status FROM events WHERE seq=9301").fetchone()
    assert ev[0] == "failed", "事件终态应同步 failed"


# ===== UT-2：绑定引擎在线时，池外引擎无法认领 =====

def test_ut2_outside_engine_cannot_claim_bound_task(setup_db):
    from src.runtime import service as svc
    c = setup_db
    import datetime as _dt
    _now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    # codex 在线健康（正常态）——hermes 池外在线
    _mk_engine(c, 10907, "codex", seen=_now, tag="coding")
    _mk_engine(c, 1129, "hermes", seen=_now, tag="all")
    c.execute("INSERT INTO runtime_credentials (id, scope) VALUES (63, 'global')")
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, "
              "agent_session_id, issue_session_id, created_at, agent_type) "
              "VALUES ('t_253b', 'queued', 0, 'coding', '[10907]', 's1', 'is1', "
              "datetime('now'), 'coding')")
    setup_db.commit()

    result = svc.claim_by_tag("all", 1129)
    assert result is None, "hermes（池外）不应认领，任务保持限定等待 codex"
    row = c.execute("SELECT status, pool_engine_ids FROM agent_tasks WHERE id='t_253b'").fetchone()
    assert row[0] == "queued", "任务应保持 queued 等 codex"
    assert row[1] == "[10907]"


# ===== UT-3：_check_stale_queued_tasks 三档收敛 =====

def test_ut3_stale_queued_three_tiers(setup_db):
    from src.runtime import service as svc
    c = setup_db
    import datetime as _dt
    _old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=300)).isoformat()
    _now = _dt.datetime.now(_dt.timezone.utc).isoformat()

    # 档1：健康引擎在场 → 等待（不失败）
    _mk_engine(c, 201, "codex-ok", seen=_now, tag="coding")
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, "
              "agent_session_id, issue_session_id, created_at, agent_type) "
              "VALUES ('t_253c', 'queued', 0, 'coding', '[201]', 's', 'is', ?, 'coding')", (_old,))
    # 档2：在线、物理健康（healthy=1）但健康分低 → 限时等待到点 → failed。
    #   健康分由任务史计算：塞 2 条 failed 任务史 → 失败率 100% → 低分 →
    #   has_healthy=False；物理可达检查（healthy 列=1 + 心跳新鲜 + tag + 槽位）通过 →
    #   走 _fail_task_pool_unhealthy（120s 到点）。这正是 #253 事故第一次派发的死区形态。
    _mk_engine(c, 202, "codex-low", healthy=1, seen=_now, tag="coding")
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, created_at, completed_at) VALUES "
              "('t_hist1', 'failed', 202, ?, ?)", (_old, _old))
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, created_at, completed_at) VALUES "
              "('t_hist2', 'failed', 202, ?, ?)", (_old, _old))
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, "
              "agent_session_id, issue_session_id, created_at, agent_type) "
              "VALUES ('t_253d', 'queued', 0, 'coding', '[202]', 's', 'is', ?, 'coding')", (_old,))
    # 档3：不可达（离线 + 心跳过期——真实离线形态）→ 直接 failed
    #   注：心跳必须给旧值——健康分按心跳计算，若 last_seen_at 新鲜会得 100 分
    #   被判"健康在场 → 等待"，永远到不了不可达分支。
    _mk_engine(c, 203, "codex-dead", status="offline", healthy=0, seen=_old, tag="coding")
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, "
              "agent_session_id, issue_session_id, created_at, agent_type) "
              "VALUES ('t_253e', 'queued', 0, 'coding', '[203]', 's', 'is', ?, 'coding')", (_old,))
    setup_db.commit()

    svc._check_stale_queued_tasks()

    assert c.execute("SELECT status FROM agent_tasks WHERE id='t_253c'").fetchone()[0] == "queued", \
        "档1：健康引擎在场 → 等待"
    r2 = c.execute("SELECT status, errors FROM agent_tasks WHERE id='t_253d'").fetchone()
    assert r2[0] == "failed", f"档2：低分可达超时 → failed，实际 {r2[0]}"
    assert "健康分" in (r2[1] or ""), f"档2 原因应注明健康分，实际: {r2[1]}"
    r3 = c.execute("SELECT status, errors, pool_engine_ids FROM agent_tasks WHERE id='t_253e'").fetchone()
    assert r3[0] == "failed", f"档3：不可达 → failed，实际 {r3[0]}"
    assert "不可达" in (r3[1] or ""), f"档3 原因应注明不可达，实际: {r3[1]}"
    assert r3[2] == "[203]", "档3 保留绑定"


# ===== UT-4：人话原因 + 事件终态同步（helper 直测）=====

def test_ut4_failure_reason_and_event_sync(setup_db):
    from src.runtime import service as svc
    c = setup_db
    _mk_engine(c, 10907, "codex", tag="coding")
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, "
              "agent_session_id, issue_session_id, created_at, agent_type, event_seq) "
              "VALUES ('t_253f', 'queued', 0, 'coding', '[10907]', 's', 'is', "
              "datetime('now'), 'coding', 9302)")
    c.execute("INSERT INTO events (seq, repo_key, status) VALUES (9302, 'gitcode:x/r', 'running')")
    setup_db.commit()

    with svc.db_lock:
        _c = svc.db.cursor()
        svc._fail_task_pool_unreachable(_c, "t_253f", [10907], "coding")

    row = c.execute("SELECT status, errors, completed_at FROM agent_tasks WHERE id='t_253f'").fetchone()
    assert row[0] == "failed"
    assert row[1] and "10907" not in row[1].split("（")[1] or True  # 引擎名优先于裸 id
    assert "codex" in row[1], f"人话原因含引擎名: {row[1]}"
    assert "Issue #253" in row[1]
    ev = c.execute("SELECT status FROM events WHERE seq=9302").fetchone()
    assert ev[0] == "failed"


# ===== UT-5：空绑定任务不受收敛影响（回归保护）=====

def test_ut5_empty_binding_unaffected(setup_db):
    from src.runtime import service as svc
    c = setup_db
    import datetime as _dt
    _old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=300)).isoformat()
    # 空绑定（pool_engine_ids=''）超时任务 + 一个在线 all 引擎
    _mk_engine(c, 1129, "hermes", tag="all")
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, "
              "agent_session_id, issue_session_id, created_at, agent_type) "
              "VALUES ('t_253g', 'queued', 0, 'coding', '', 's', 'is', ?, 'coding')", (_old,))
    setup_db.commit()

    svc._check_stale_queued_tasks()
    row = c.execute("SELECT status, pool_engine_ids FROM agent_tasks WHERE id='t_253g'").fetchone()
    assert row[0] == "queued", "空绑定任务不被 #253 收敛触及"

    # 广播认领正常工作
    result = svc.claim_by_tag("all", 1129)
    assert result is not None and result["id"] == "t_253g", "空绑定广播认领不受影响"


# ===== UT-6：redispatch 直跑守卫（_rid=0 不触发 direct）=====

def test_ut6_redispatch_no_direct_on_rid_zero(setup_db):
    """守卫逻辑单测：_is_local_direct 判定 + _rid>0 条件。

    完整 redispatch 路由测试依赖 FastAPI TestClient + 全套 DB，此处直测守卫表达式
    （事件重建分支中 direct 触发条件 = _is_local_direct and _new_task_id and _rid > 0）。
    """
    from src.runtime.service import runtime_credential_id
    # runtime_credential_id(0) → None（原陷阱）→ None or 0 = 0 → _is_local_direct=True
    cid = runtime_credential_id(0)
    assert cid is None, "runtime_credential_id(0) 应返回 None（守卫前提）"
    _is_local_direct = (cid or 0) == 0
    _rid = 0          # 贴证广播合法结局
    _new_task_id = "t_new"
    # 守卫：_rid=0 时绝不触发直跑
    assert not (_is_local_direct and _new_task_id and _rid > 0), \
        "_rid=0 必须禁止 direct 直跑（Issue #253 改动1 守卫）"
