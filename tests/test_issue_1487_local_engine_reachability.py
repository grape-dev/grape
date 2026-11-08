"""Issue #1487：本机直连引擎（credential_id=0）认领侧可达性口径对齐。

覆盖 Spec 验收标准：
- A：cred0 引擎心跳过期/为空仍判可达（对齐 engine_select._health_ok 的 installed+healthy 口径）；
     cred0 installed=0 / healthy=0 仍不可达；cred>0 心跳过期仍不可达（Issue #253 不变）。
- C：claim 认领时池内全不可达 → 保持 queued，交 _check_stale_queued_tasks 120s 窗口裁决。
- D：_fail_task_pool_unreachable 文案区分 离线/心跳过期/无对应标签能力/scope 不匹配。
"""
import os, sys, json, threading, datetime

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

import pytest


@pytest.fixture()
def setup_db(monkeypatch):
    """独立测试库（同 test_issue_253 布局）。"""
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
        pool_engine_source TEXT DEFAULT '',
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
    monkeypatch.setattr(svc, "db_lock", threading.RLock())
    import src.session_manager as _sm
    import src.api.event_store as _es
    monkeypatch.setattr(_sm, "db", test_db)
    monkeypatch.setattr(_sm, "db_lock", threading.RLock())
    monkeypatch.setattr(_es, "db", test_db)
    monkeypatch.setattr(_es, "db_lock", threading.RLock())
    yield test_db
    test_db.close()


def _ts(seconds_ago=0):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=seconds_ago)).isoformat()


def _mk_engine(c, rid, name, *, status="online", healthy=1, installed=1, cred=0,
               seen="now", tag="all", cap_tag=None, probe="ok",
               cred_scope="", cred_owner=""):
    _now = _ts(0) if seen == "now" else seen
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, installed, credential_id, "
              "last_seen_at, busy_slots, max_concurrent, probe_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (rid, name, status, healthy, installed, cred, _now, 0, 4, probe))
    if cred or cred_scope:
        c.execute("INSERT OR IGNORE INTO runtime_credentials (id, scope, owner_id) VALUES (?,?,?)",
                  (cred if cred else rid, cred_scope or "global", cred_owner or ""))
    if tag is not None:
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, enabled) "
                  "VALUES (?,?,4,1)", (rid, cap_tag if cap_tag is not None else tag))


# ===== A：cred0 心跳豁免（对齐 engine_select._health_ok）=====

def test_a_cred0_stale_heartbeat_still_reachable(setup_db):
    """cred0 引擎 last_seen_at 超 100s 阈值 → 仍可达（Issue #1487 验收 1）。"""
    from src.runtime import service as svc
    c = setup_db
    _mk_engine(c, 1, "local-opencode", cred=0, seen=_ts(400))
    setup_db.commit()
    with svc.db_lock:
        assert svc._check_pool_engines_reachable([1], "coding") is True


def test_a_cred0_empty_heartbeat_still_reachable(setup_db):
    """cred0 引擎 last_seen_at 为空（启动 600s 窗口）→ 仍可达。"""
    from src.runtime import service as svc
    c = setup_db
    _mk_engine(c, 1, "local-opencode", cred=0, seen=None)
    setup_db.commit()
    with svc.db_lock:
        assert svc._check_pool_engines_reachable([1], "coding") is True


def test_a_cred0_not_installed_unreachable(setup_db):
    """cred0 installed=0 → 不可达（_health_ok 口径：installed+healthy）。"""
    from src.runtime import service as svc
    c = setup_db
    _mk_engine(c, 1, "local-opencode", cred=0, installed=0)
    setup_db.commit()
    with svc.db_lock:
        assert svc._check_pool_engines_reachable([1], "coding") is False


def test_a_cred0_unhealthy_unreachable(setup_db):
    """cred0 healthy=0 → 不可达。"""
    from src.runtime import service as svc
    c = setup_db
    _mk_engine(c, 1, "local-opencode", cred=0, healthy=0)
    setup_db.commit()
    with svc.db_lock:
        assert svc._check_pool_engines_reachable([1], "coding") is False


def test_a_cred0_status_is_not_part_of_yardstick(setup_db):
    """cred0 的 status 不进入可达性尺子（与 engine_select._health_ok 完全一致）。

    Issue #1487 返工：原实现 cred0 仍要求 status=='online'，而派发侧 _health_ok 只看
    installed+healthy → 两把尺子在 status 维度继续打架（实测 claim=False / dispatch=True）。
    统一到 src.runtime.health.engine_health_ok 后两侧一致：本机引擎行 status 只是探测副产物。
    """
    from src.runtime import service as svc
    from src.runtime.engine_select import _health_ok
    c = setup_db
    _mk_engine(c, 1, "local-opencode", cred=0, status="offline", healthy=1, installed=1)
    setup_db.commit()
    with svc.db_lock:
        claim = svc._check_pool_engines_reachable([1], "coding")
    row = c.execute("SELECT status, healthy, installed, credential_id, last_seen_at "
                    "FROM agent_runtimes WHERE id=1").fetchone()
    dispatch = _health_ok({"credential_id": row[3], "status": row[0], "healthy": row[1],
                           "installed": row[2], "last_seen_at": row[4]})
    assert claim == dispatch, f"两把尺子仍不一致：claim={claim} dispatch={dispatch}"
    assert claim is True, "installed+healthy 的本机引擎应可达（心跳/status 都不参与）"


def test_a_remote_stale_heartbeat_still_unreachable(setup_db):
    """cred>0 远端引擎心跳过期 → 仍不可达（Issue #253 行为不变，验收 3）。"""
    from src.runtime import service as svc
    c = setup_db
    _mk_engine(c, 2, "remote-worker", cred=63, seen=_ts(400))
    setup_db.commit()
    with svc.db_lock:
        assert svc._check_pool_engines_reachable([2], "coding") is False


# ===== C：claim 侧全不可达 → 保持 queued，交 stale-queued 裁决 =====

def test_c_claim_pool_all_unreachable_keeps_queued(setup_db):
    """池外引擎认领时池内全不可达 → 任务保持 queued（不再直接 failed）。"""
    from src.runtime import service as svc
    c = setup_db
    # 池内：远端 codex 心跳过期（cred>0，不可达）
    _mk_engine(c, 10907, "codex", cred=63, seen=_ts(400), tag="coding", cap_tag="coding")
    # 池外：hermes 在线带 all 标签
    _mk_engine(c, 1129, "hermes", cred=0, tag="all")
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, "
              "agent_session_id, issue_session_id, created_at, agent_type, event_seq) "
              "VALUES ('t_1487c', 'queued', 0, 'coding', '[10907]', 's1', 'is1', "
              "datetime('now'), 'coding', 14871)")
    c.execute("INSERT INTO events (seq, repo_key, status) VALUES (14871, 'gitcode:x/r', 'running')")
    setup_db.commit()

    result = svc.claim_by_tag("all", 1129)
    assert result is None, "hermes（池外）不应认领 codex 专属任务"
    row = c.execute("SELECT status, pool_engine_ids FROM agent_tasks WHERE id='t_1487c'").fetchone()
    assert row[0] == "queued", f"单次快照不可达不应判死，应保持 queued，实际 {row[0]}"
    assert row[1] == "[10907]", "pool_engine_ids 必须保留"
    ev = c.execute("SELECT status FROM events WHERE seq=14871").fetchone()
    assert ev[0] == "running", "事件不应在 claim 阶段被判死"


# ===== D：失败文案四分类 =====

def test_d_fail_message_classifies_four_reasons(setup_db):
    """离线/心跳过期/无对应标签能力/scope 不匹配 → 文案分别含对应关键字。"""
    from src.runtime import service as svc
    c = setup_db
    _mk_engine(c, 11, "eng-offline", cred=64, status="offline")
    _mk_engine(c, 12, "eng-stale", cred=65, seen=_ts(400))
    _mk_engine(c, 13, "eng-notag", cred=66, tag=None, cap_tag="other")
    _mk_engine(c, 14, "eng-scope", cred=67, cred_scope="repo", cred_owner="gitcode:other/r")
    c.execute("INSERT INTO agent_tasks (id, status, pool_tag, repo_key, created_at) "
              "VALUES ('t_1487d', 'queued', 'coding', 'gitcode:x/r', datetime('now'))")
    setup_db.commit()

    svc._fail_task_pool_unreachable(c.cursor(), "t_1487d", [11, 12, 13, 14], "coding",
                                    repo_key="gitcode:x/r")
    row = c.execute("SELECT errors, status FROM agent_tasks WHERE id='t_1487d'").fetchone()
    assert row[1] == "failed"
    msg = row[0] or ""
    assert "离线" in msg, f"应含『离线』分类，实际: {msg}"
    assert "心跳过期" in msg, f"应含『心跳过期』分类，实际: {msg}"
    assert "无对应标签能力" in msg, f"应含『无对应标签能力』分类，实际: {msg}"
    assert "scope 不匹配" in msg, f"应含『scope 不匹配』分类，实际: {msg}"
    assert "标签=coding" in msg, f"应保留原标签信息，实际: {msg}"


def test_d_cred0_uninstalled_classified_offline(setup_db):
    """cred0 installed=0 归入离线类（心跳豁免口径下唯一可能的本机不可达形态）。"""
    from src.runtime import service as svc
    c = setup_db
    _mk_engine(c, 21, "local-broken", cred=0, installed=0)
    c.execute("INSERT INTO agent_tasks (id, status, pool_tag, created_at) "
              "VALUES ('t_1487e', 'queued', 'coding', datetime('now'))")
    setup_db.commit()

    svc._fail_task_pool_unreachable(c.cursor(), "t_1487e", [21], "coding")
    row = c.execute("SELECT errors FROM agent_tasks WHERE id='t_1487e'").fetchone()
    msg = row[0] or ""
    assert "离线" in msg and "心跳过期" not in msg.split("离线")[0]


# ===== A 返工：认领侧与派发侧口径一致性矩阵 =====

PARITY_CASES = [
    # (说明, cred, status, healthy, installed, seen_seconds_ago, 期望)
    ("cred0 心跳过期", 0, "online", 1, 1, 400, True),
    ("cred0 无心跳", 0, "online", 1, 1, None, True),
    ("cred0 status=offline", 0, "offline", 1, 1, 400, True),
    ("cred0 未安装", 0, "online", 1, 0, 0, False),
    ("cred0 不健康", 0, "online", 0, 1, 0, False),
    ("远端 心跳新鲜", 63, "online", 1, 1, 5, True),
    ("远端 心跳过期", 63, "online", 1, 1, 400, False),
    ("远端 无心跳", 63, "online", 1, 1, None, False),
    ("远端 offline", 63, "offline", 1, 1, 5, False),
    ("远端 不健康", 63, "online", 0, 1, 5, False),
]


def test_a_claim_and_dispatch_share_one_yardstick(setup_db):
    """认领侧 _check_pool_engines_reachable 与派发侧 _health_ok 必须逐例一致（Issue #1487 返工）。"""
    from src.runtime import service as svc
    from src.runtime.engine_select import _health_ok
    from src.runtime.health import parse_dt
    c = setup_db
    rid = 100
    for desc, cred, status, healthy, installed, ago, want in PARITY_CASES:
        rid += 1
        _mk_engine(c, rid, f"eng-{rid}", cred=cred, status=status, healthy=healthy,
                   installed=installed, seen=None if ago is None else _ts(ago), tag="all")
    setup_db.commit()
    rid = 100
    mismatched, wrong = [], []
    for desc, cred, status, healthy, installed, ago, want in PARITY_CASES:
        rid += 1
        with svc.db_lock:
            claim = svc._check_pool_engines_reachable([rid], "all")
        row = c.execute("SELECT status, healthy, installed, credential_id, last_seen_at "
                        "FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
        dispatch = _health_ok({"credential_id": row[3], "status": row[0], "healthy": row[1],
                               "installed": row[2], "last_seen_at": row[4]})
        if claim != dispatch:
            mismatched.append((desc, claim, dispatch))
        if claim != want:
            wrong.append((desc, claim, want))
    assert not mismatched, f"两把尺子不一致：{mismatched}"
    assert not wrong, f"判定与既有契约不符（心跳过期={parse_dt(_ts(400)) is not None}）：{wrong}"


def test_a_remote_probe_gate_is_claim_only(setup_db):
    """文档化差异：probe 异常只在认领侧生效（既有口径，Issue #1487 未改），不是回归。"""
    from src.runtime import service as svc
    from src.runtime.engine_select import _health_ok
    c = setup_db
    _mk_engine(c, 201, "remote-probe-err", cred=63, seen=_ts(5), probe="error", tag="all")
    setup_db.commit()
    with svc.db_lock:
        claim = svc._check_pool_engines_reachable([201], "all")
    row = c.execute("SELECT status, healthy, installed, credential_id, last_seen_at "
                    "FROM agent_runtimes WHERE id=201").fetchone()
    dispatch = _health_ok({"credential_id": row[3], "status": row[0], "healthy": row[1],
                           "installed": row[2], "last_seen_at": row[4]})
    assert claim is False and dispatch is True, f"probe 关应只在认领侧：claim={claim} dispatch={dispatch}"


# ===== C 闭环：claim 不再判死后，stale-queued 必须真能裁决 =====

def _mk_task(c, tid, pool, *, tag="coding", ago=600, source="scene", repo="gitcode:x/r"):
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, "
              "pool_engine_source, agent_session_id, issue_session_id, created_at, agent_type, repo_key) "
              "VALUES (?, 'queued', 0, ?, ?, ?, 's1', 'is1', ?, 'coding', ?)",
              (tid, tag, json.dumps(pool), source, _ts(ago), repo))


def test_c_stale_queued_healthy_by_score_but_unreachable_fails(setup_db, monkeypatch):
    """健康分健康但物理不可达（无 tag 能力）→ 超窗口后必须判失败，不能永久排队。

    实测缺陷复现（2026-11-08 真机）：claim 侧不再判死后，本分支被
    _check_pool_has_healthy 提前 continue，任务永远停在 queued。
    """
    from src.runtime import service as svc
    import src.runtime.health_scorer as hs
    monkeypatch.setattr(hs, "is_healthy", lambda eid: True, raising=False)
    c = setup_db
    _mk_engine(c, 301, "eng-notag", cred=0, tag=None, cap_tag="other")
    _mk_task(c, "t_c1", [301], tag="coding", ago=600)
    setup_db.commit()

    svc._check_stale_queued_tasks()

    row = c.execute("SELECT status, errors FROM agent_tasks WHERE id='t_c1'").fetchone()
    assert row[0] == "failed", f"健康分健康但不可达应判失败，实际 {row[0]}"
    assert "无对应标签能力" in (row[1] or ""), f"文案应归因到标签能力，实际: {row[1]}"
    assert "健康分达标但物理不可达" in (row[1] or ""), f"应标注两把尺子矛盾，实际: {row[1]}"


def test_c_stale_queued_hard_window_fails_unclaimed_task(setup_db, monkeypatch):
    """健康且可达，但超过硬窗口仍无人认领 → 诚实失败（防永久挂单）。"""
    from src.runtime import service as svc
    import src.runtime.health_scorer as hs
    monkeypatch.setattr(hs, "is_healthy", lambda eid: True, raising=False)
    c = setup_db
    _mk_engine(c, 401, "eng-ok", cred=0, tag="all")
    _mk_task(c, "t_c2", [401], tag="coding",
             ago=svc._STALE_QUEUED_HARD_THRESHOLD + 60)
    setup_db.commit()

    svc._check_stale_queued_tasks()

    row = c.execute("SELECT status, errors FROM agent_tasks WHERE id='t_c2'").fetchone()
    assert row[0] == "failed", f"硬窗口到期应判失败，实际 {row[0]}"
    msg = row[1] or ""
    assert "未被认领" in msg, f"文案应说明排队超时，实际: {msg}"
    assert "排队超时" in msg or "Issue #1487" in msg, f"应可归因到排队超时口径，实际: {msg}"
    # 关键：引擎是可达的，绝不能把它误报成「全部不可达/离线」（实测曾出现）
    assert "全部不可达" not in msg, f"可达引擎不应被报成不可达，实际: {msg}"
    assert "离线" not in msg, f"可达引擎不应被报成离线，实际: {msg}"


def test_c_stale_queued_reachable_within_hard_window_waits(setup_db, monkeypatch):
    """健康且可达、仍在硬窗口内 → 保持 queued（正常等待引擎认领，不误杀）。"""
    from src.runtime import service as svc
    import src.runtime.health_scorer as hs
    monkeypatch.setattr(hs, "is_healthy", lambda eid: True, raising=False)
    c = setup_db
    _mk_engine(c, 501, "eng-ok2", cred=0, tag="all")
    _mk_task(c, "t_c3", [501], tag="coding", ago=svc._STALE_QUEUED_THRESHOLD + 30)
    setup_db.commit()

    svc._check_stale_queued_tasks()

    row = c.execute("SELECT status FROM agent_tasks WHERE id='t_c3'").fetchone()
    assert row[0] == "queued", f"硬窗口内应继续等待，实际 {row[0]}"
