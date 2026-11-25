"""Issue #3564：场景绑定机器优先级（claim_by_tag 优先级闸）验收用例。

全部用例使用 **sqlite 内存库**驱动真实 `claim_by_tag`（不 mock 认领主路径），
覆盖 Spec 验收：高优在线空闲 → 低优跳过；高优满载/离线/心跳过期/无 tag/scope
不匹配 → 低优可接；未配置逐字节一致；未配置按最低档；queued 超时降级兜底；
决策日志可复现跳过原因。
"""
import datetime as _dt
import json
import sqlite3
import threading

import pytest

from src.domains.runtime.engine import service


def _now_iso(offset_seconds: int = 0) -> str:
    return (_dt.datetime.now(_dt.timezone.utc)
            + _dt.timedelta(seconds=offset_seconds)).isoformat()


SCHEMA = """
CREATE TABLE agent_tasks (
    id TEXT PRIMARY KEY,
    agent_session_id TEXT DEFAULT '',
    runtime_id INTEGER DEFAULT 0,
    event_seq INTEGER,
    issue_id TEXT DEFAULT '',
    repo_key TEXT DEFAULT '',
    agent_type TEXT DEFAULT '',
    skill TEXT DEFAULT '',
    skill_id INTEGER DEFAULT 0,
    prompt TEXT DEFAULT '',
    cwd TEXT DEFAULT '',
    runtime_session_id TEXT DEFAULT '',
    event_type TEXT DEFAULT '',
    account TEXT DEFAULT '',
    user_id TEXT DEFAULT 0,
    repo_gitcode_token TEXT DEFAULT 'tk',
    user_gitcode_token TEXT DEFAULT '',
    env_extra TEXT DEFAULT '{}',
    skill_files TEXT DEFAULT '{}',
    server_url TEXT DEFAULT '',
    issue_session_id TEXT DEFAULT '',
    project_info_files TEXT DEFAULT '{}',
    project_info_meta TEXT DEFAULT '{}',
    knowledge_files TEXT DEFAULT '{}',
    project_info_file_urls TEXT DEFAULT '',
    knowledge_file_urls TEXT DEFAULT '',
    attempts INTEGER DEFAULT 0,
    pool_agent_type TEXT DEFAULT '',
    affinity_runtime_id INTEGER DEFAULT 0,
    pool_tag TEXT DEFAULT '',
    pool_engine_ids TEXT DEFAULT '',
    pool_engine_uids TEXT DEFAULT '',
    pool_selector TEXT DEFAULT '',
    engine_uid TEXT DEFAULT '',
    engine_name_snapshot TEXT DEFAULT '',
    pool_engine_source TEXT DEFAULT '',
    engine_priorities TEXT DEFAULT '',
    status TEXT DEFAULT 'queued',
    stream_ended INTEGER DEFAULT 0,
    self_reported INTEGER DEFAULT 0,
    pending_since TEXT DEFAULT NULL,
    exit_code INTEGER,
    result_text TEXT DEFAULT '',
    errors TEXT DEFAULT '',
    stderr TEXT DEFAULT '',
    usage_json TEXT DEFAULT '{}',
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT,
    trace_context TEXT DEFAULT ''
);
CREATE TABLE agent_runtimes (
    id INTEGER PRIMARY KEY,
    credential_id INTEGER DEFAULT 0,
    name TEXT,
    status TEXT DEFAULT 'online',
    healthy INTEGER DEFAULT 1,
    probe_status TEXT DEFAULT 'ok',
    installed INTEGER DEFAULT 1,
    worker_version TEXT DEFAULT '1.0',
    busy_slots INTEGER DEFAULT 0,
    max_concurrent INTEGER DEFAULT 4,
    host_name TEXT DEFAULT '',
    last_seen_at TEXT,
    role_tags TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    runtime_type TEXT DEFAULT '',
    runtime_info TEXT DEFAULT ''
);
CREATE TABLE runtime_credentials (
    id INTEGER PRIMARY KEY,
    scope TEXT DEFAULT 'global',
    owner_id TEXT DEFAULT '',
    owner_name TEXT DEFAULT '',
    device_name TEXT DEFAULT '',
    status TEXT DEFAULT 'active'
);
CREATE TABLE engine_capabilities (
    runtime_id INTEGER,
    tag TEXT,
    max_concurrent INTEGER DEFAULT 4,
    enabled INTEGER DEFAULT 1
);
CREATE TABLE runtime_health_scores (
    runtime_id INTEGER PRIMARY KEY,
    health_score REAL DEFAULT 100,
    total_tasks INTEGER DEFAULT 0,
    failed_tasks INTEGER DEFAULT 0,
    completed_tasks INTEGER DEFAULT 0,
    consecutive_failures INTEGER DEFAULT 0,
    failure_rate REAL DEFAULT 0,
    last_status TEXT DEFAULT 'completed',
    window_days INTEGER DEFAULT 7,
    updated_at TEXT DEFAULT '',
    failed_tasks_effective INTEGER DEFAULT 0,
    last_failure_at TEXT DEFAULT ''
);
"""


@pytest.fixture
def memdb(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()

    import src.kernel.session_manager as _sm
    monkeypatch.setattr(service, "db", conn)
    monkeypatch.setattr(service, "db_lock", threading.RLock())
    monkeypatch.setattr(_sm, "db", conn)
    monkeypatch.setattr(_sm, "db_lock", threading.RLock())

    import src.domains.runtime.engine.health_scorer as _hs
    monkeypatch.setattr(_hs, "_cache", {})
    # 健康分维度不属本单验收（见 Spec 的风险兜底），固定为健康，隔离优先级闸行为。
    monkeypatch.setattr(_hs, "is_healthy", lambda *a, **k: True)

    yield conn
    conn.close()


def _engine(conn, eid, *, tag="vod", scope="global", owner_id="", owner_name="",
            status="online", healthy=1, installed=1, credential_id=0,
            last_seen=None, max_concurrent=4, probe_status="ok",
            worker_version="1.0", name="opencode"):
    cred_id = credential_id
    if cred_id:
        conn.execute(
            "INSERT INTO runtime_credentials (id, scope, owner_id, owner_name, device_name) "
            "VALUES (?,?,?,?,?)", (cred_id, scope, owner_id, owner_name, "dev"))
    else:
        # credential_id=0 → 本机引擎，scope 由 runtime_credentials 缺失决定
        pass
    conn.execute(
        "INSERT INTO agent_runtimes (id, credential_id, name, status, healthy, installed, "
        "probe_status, worker_version, max_concurrent, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (eid, cred_id, name, status, healthy, installed, probe_status,
         worker_version, max_concurrent, last_seen if last_seen is not None else _now_iso()))
    conn.execute(
        "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, enabled) "
        "VALUES (?,?,?,1)", (eid, tag, max_concurrent))
    conn.commit()


def _task(conn, tid, *, tag="vod", prio=None, pool=None, created_at=None,
          status="queued", runtime_id=0, issue_session_id=None, repo_key="gitcode:o/r"):
    conn.execute(
        "INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, "
        "engine_priorities, agent_session_id, issue_session_id, created_at, agent_type, "
        "repo_key, repo_gitcode_token) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, status, runtime_id, tag, pool if pool is not None else "[1,2]",
         json.dumps(prio) if prio else "",
         "s-" + tid, issue_session_id if issue_session_id is not None else "is-" + tid,
         created_at or _now_iso(), tag, repo_key, "tk"))
    conn.commit()
    return tid


def _task_status(conn, tid):
    return conn.execute("SELECT status FROM agent_tasks WHERE id=?", (tid,)).fetchone()[0]


def _high_idle(conn, **kw):
    kw.setdefault("tag", "vod")
    _engine(conn, 1, **kw)


def _low(conn, **kw):
    _engine(conn, 2, tag="vod", **kw)


def test_high_priority_idle_low_is_skipped(memdb, caplog):
    """高优在线空闲 → 低优认领被跳过（任务保持 queued），高优随后认领成功。"""
    _high_idle(memdb)
    _low(memdb)
    tid = _task(memdb, "t-prio-skip", prio={"1": 1, "2": 9})
    caplog.set_level("INFO")
    assert service.claim_by_tag("vod", 2) is None
    assert _task_status(memdb, tid) == "queued"
    assert "claim guard [priority]" in caplog.text
    assert "engine 1(p=1)" in caplog.text
    claimed = service.claim_by_tag("vod", 1)
    assert claimed and claimed["id"] == tid and claimed["status"] == "running"


def test_high_priority_full_low_claims(memdb):
    """高优满载（真实 active >= max_concurrent）→ 低优可接。"""
    _high_idle(memdb, max_concurrent=1)
    _low(memdb)
    memdb.execute(
        "INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, agent_session_id, "
        "issue_session_id, created_at, agent_type) VALUES "
        "('t-running', 'running', 1, 'vod', 's-run', 'is-run', ?, 'vod')",
        (_now_iso(),))
    memdb.commit()
    tid = _task(memdb, "t-prio-full", prio={"1": 1, "2": 9})
    claimed = service.claim_by_tag("vod", 2)
    assert claimed and claimed["id"] == tid


def test_high_priority_offline_low_claims(memdb):
    """高优离线 → 低优可接。"""
    _high_idle(memdb, credential_id=13, status="offline")
    _low(memdb)
    tid = _task(memdb, "t-prio-offline", prio={"1": 1, "2": 9})
    claimed = service.claim_by_tag("vod", 2)
    assert claimed and claimed["id"] == tid


def test_high_priority_stale_heartbeat_low_claims(memdb):
    """高优心跳过期（远端引擎 > 100s）→ 低优可接。"""
    _high_idle(memdb, credential_id=11, last_seen=_now_iso(-600))
    _low(memdb)
    tid = _task(memdb, "t-prio-stale", prio={"1": 1, "2": 9})
    claimed = service.claim_by_tag("vod", 2)
    assert claimed and claimed["id"] == tid


def test_high_priority_missing_tag_low_claims(memdb):
    """高优无对应 tag 能力 → 低优可接。"""
    _high_idle(memdb, tag="qa")
    _low(memdb)
    tid = _task(memdb, "t-prio-tag", prio={"1": 1, "2": 9})
    claimed = service.claim_by_tag("vod", 2)
    assert claimed and claimed["id"] == tid


def test_high_priority_scope_mismatch_low_claims(memdb):
    """高优 repo scope 属于其它仓库 → 低优可接。"""
    _high_idle(memdb, credential_id=12, scope="repo", owner_id="other/repo")
    _low(memdb)
    tid = _task(memdb, "t-prio-scope", prio={"1": 1, "2": 9}, repo_key="gitcode:o/r")
    claimed = service.claim_by_tag("vod", 2)
    assert claimed and claimed["id"] == tid


def test_unconfigured_priorities_legacy_behavior(memdb, caplog):
    """未配置 engine_priorities → 与改前逐字节一致：低优立即认领且无优先级决策日志。"""
    _high_idle(memdb)
    _low(memdb)
    tid = _task(memdb, "t-prio-unset", prio=None)
    caplog.set_level("INFO")
    claimed = service.claim_by_tag("vod", 2)
    assert claimed and claimed["id"] == tid and claimed["status"] == "running"
    assert "claim guard [priority]" not in caplog.text


def test_unconfigured_engine_treated_as_lowest(memdb):
    """未配置优先级的机器按最低档（9）：仅高优配置 p=1 时，未配置的低优机跳过。"""
    _high_idle(memdb)
    _low(memdb)
    tid = _task(memdb, "t-prio-only-high", prio={"1": 1})
    assert service.claim_by_tag("vod", 2) is None
    assert _task_status(memdb, tid) == "queued"
    claimed = service.claim_by_tag("vod", 1)
    assert claimed and claimed["id"] == tid


def test_queued_timeout_bypasses_priority(memdb, caplog):
    """queued 超时（默认 120s）→ 忽略优先级，低优可接（防高优可达但从不出手饿死）。"""
    _high_idle(memdb)
    _low(memdb)
    tid = _task(memdb, "t-prio-timeout", prio={"1": 1, "2": 9},
                created_at=_now_iso(-300))
    caplog.set_level("INFO")
    claimed = service.claim_by_tag("vod", 2)
    assert claimed and claimed["id"] == tid
    assert "bypass" in caplog.text and "ignore priority" in caplog.text


def test_equal_priority_does_not_skip(memdb):
    """同档优先级（p'<p 不成立）→ 不跳过，FIFO 低优机可接。"""
    _high_idle(memdb)
    _low(memdb)
    tid = _task(memdb, "t-prio-equal", prio={"1": 1, "2": 1})
    claimed = service.claim_by_tag("vod", 2)
    assert claimed and claimed["id"] == tid


def test_highest_priority_engine_claims(memdb):
    """本机即最高优先 → 直接认领，不被自己挡。"""
    _high_idle(memdb)
    _low(memdb)
    tid = _task(memdb, "t-prio-high", prio={"1": 1, "2": 9})
    claimed = service.claim_by_tag("vod", 1)
    assert claimed and claimed["id"] == tid


def test_scene_binding_parses_engine_priorities(monkeypatch):
    """场景 node 旁挂 engine_priorities 解析：只保留绑定引擎、值 1..9、空=未配置。"""
    import src.domains.mgmt.pipeline_config as pc
    cfg = {"name": "s", "nodes": [
        {"id": "coding", "engine_runtime_ids": [11, 12],
         "engine_priorities": {"11": 1, "12": "9", "99": 2, "12x": 3, "11bad": 1}},
    ]}
    monkeypatch.setattr(pc, "get_pipeline_config", lambda *a, **k: cfg)
    b = pc.get_scene_engine_binding("s1", "coding", repo_key="gitcode:o/r")
    assert b["status"] == "bound"
    assert b["engine_ids"] == [11, 12]
    assert b["engine_priorities"] == {"11": 1, "12": 9}


def test_scene_binding_without_priorities_is_empty(monkeypatch):
    """未配置 engine_priorities → 空 dict（旧行为，认领侧整体旁路）。"""
    import src.domains.mgmt.pipeline_config as pc
    cfg = {"name": "s", "nodes": [{"id": "coding", "engine_runtime_ids": [11]}]}
    monkeypatch.setattr(pc, "get_pipeline_config", lambda *a, **k: cfg)
    b = pc.get_scene_engine_binding("s1", "coding", repo_key="gitcode:o/r")
    assert b["engine_priorities"] == {}


def test_scene_pool_priorities_json(monkeypatch):
    """get_scene_engine_priorities 产出稳定 JSON；未配置返回空串。"""
    import src.api.pipeline_config as pc
    import src.domains.runtime.engine.engine_select as es
    monkeypatch.setattr(pc, "get_scene_engine_binding", lambda *a, **k: {
        "status": "bound", "engine_ids": [11, 12],
        "engine_priorities": {"12": 9, "11": 1}})
    got = es.get_scene_engine_priorities("s1", "coding", repo_key="gitcode:o/r")
    assert json.loads(got) == {"11": 1, "12": 9}
    monkeypatch.setattr(pc, "get_scene_engine_binding", lambda *a, **k: {
        "status": "bound", "engine_ids": [11], "engine_priorities": {}})
    assert es.get_scene_engine_priorities("s1", "coding", repo_key="gitcode:o/r") == ""


def test_task_columns_include_engine_priorities():
    """入队列清单包含 engine_priorities（_TASK_COLUMNS 与迁移列必须同源）。"""
    assert "engine_priorities" in service._TASK_COLUMNS_LIST


# ---------- QA #3564 复审补充：健康分闸 / 池交集 / enqueue 写入 / 归一化收敛 ----------

def test_high_priority_low_health_score_low_claims(memdb, monkeypatch):
    """QA-3：高优引擎健康分不达标（其余可达）→ 不得压制低优，低优可接。

    _engine_claimable_now 必须与 claim_by_tag 的健康分闸同一把尺子：
    可达但低分的高优引擎不构成"可接取"，低优引擎直接认领，不被压到 120s 超时。
    """
    import src.domains.runtime.engine.health_scorer as _hs
    _high_idle(memdb)
    _low(memdb)
    # 仅高优引擎（1）低分；认领方（2）健康，避免被 claim_by_tag 自身健康闸拦截
    monkeypatch.setattr(_hs, "is_healthy", lambda eid, *a, **k: int(eid) != 1)
    tid = _task(memdb, "t-prio-lowhealth", prio={"1": 1, "2": 9})
    claimed = service.claim_by_tag("vod", 2)
    assert claimed and claimed["id"] == tid


def test_high_priority_health_check_error_low_claims(memdb, monkeypatch):
    """QA-3：高优引擎健康分判定异常 → 保守视为不可接取（与 claim_by_tag reject 对齐）。"""
    import src.domains.runtime.engine.health_scorer as _hs

    def _boom(eid, *a, **k):
        if int(eid) == 1:  # 仅高优引擎判定异常；认领方（2）正常
            raise RuntimeError("scorer down")
        return True

    _high_idle(memdb)
    _low(memdb)
    monkeypatch.setattr(_hs, "is_healthy", _boom)
    tid = _task(memdb, "t-prio-healtherr", prio={"1": 1, "2": 9})
    claimed = service.claim_by_tag("vod", 2)
    assert claimed and claimed["id"] == tid


def test_high_priority_outside_pool_does_not_suppress(memdb):
    """QA-2：高优引擎被排除出 pool_engine_ids → 不压制池内低优（判定限池内交集）。

    入队池已按健康过滤：池外高优引擎即使物理可达也不参与优先级判定。
    """
    _high_idle(memdb)   # engine 1（p=1，可达空闲）但不在池内
    _low(memdb)         # engine 2（p=9，池内）
    tid = _task(memdb, "t-prio-outpool", prio={"1": 1, "2": 9}, pool="[2]")
    claimed = service.claim_by_tag("vod", 2)
    assert claimed and claimed["id"] == tid


def test_high_priority_inside_pool_still_suppresses(memdb):
    """QA-2 对照组：高优在池内且可达空闲 → 仍压制池内低优（交集语义不误伤正例）。"""
    _high_idle(memdb)
    _low(memdb)
    tid = _task(memdb, "t-prio-inpool", prio={"1": 1, "2": 9}, pool="[1,2]")
    assert service.claim_by_tag("vod", 2) is None
    assert _task_status(memdb, tid) == "queued"
    claimed = service.claim_by_tag("vod", 1)
    assert claimed and claimed["id"] == tid


def test_empty_pool_keeps_legacy_priority_semantics(memdb):
    """QA-2：pool_engine_ids 为空（全量广播）→ 维持旧语义（映射键全量参与判定）。"""
    _high_idle(memdb)
    _low(memdb)
    tid = _task(memdb, "t-prio-emptypool", prio={"1": 1, "2": 9}, pool="")
    assert service.claim_by_tag("vod", 2) is None
    assert _task_status(memdb, tid) == "queued"


def test_enqueue_task_writes_engine_priorities_column():
    """QA 硬回归：enqueue_task 的 INSERT 行必须携带 engine_priorities 列值。

    enqueue_task 全链路依赖路由/快照/凭据，无法单测直跑；本用例与
    tests/contracts/test_agent_tasks_insert_contract.py 的静态守卫同法：
    以源码契约看护 task 行构造必须显式包含 engine_priorities（列清单为唯一真源）。
    """
    import inspect
    import re as _re

    src = inspect.getsource(service.enqueue_task)
    # 行构造必须显式写 engine_priorities（2026-11-20 事故同型：缺列 → INSERT 整条失败）
    assert _re.search(r"[\"']engine_priorities[\"']\s*:", src), \
        "enqueue_task 行构造缺 engine_priorities 列值"
    # INSERT 必须由 _TASK_COLUMNS_LIST 驱动（防列/值数目漂移）
    assert "_TASK_COLUMNS_LIST" in src or "_insert_cols" in src, \
        "enqueue_task INSERT 必须由列清单驱动"


def test_normalize_engine_priorities_single_source_of_truth():
    """QA 判断项：4 处归一化收敛为带 strict 的单一 helper（pipeline_config）。"""
    import src.domains.mgmt.pipeline_config as pc
    # 非 strict：非法/孤儿键静默丢弃
    assert pc.normalize_engine_priorities(
        {"11": 1, "12": 0, "13": 10, "99": 2, "12x": 3}, allowed_ids={11, 12}) == {"11": 1}
    assert pc.normalize_engine_priorities(None) == {}
    assert pc.normalize_engine_priorities("bad") == {}
    # strict：非法条目抛 ValueError（保存侧拒绝）
    with pytest.raises(ValueError):
        pc.normalize_engine_priorities("bad", strict=True)
    with pytest.raises(ValueError):
        pc.normalize_engine_priorities({"11": 0}, strict=True)
    with pytest.raises(ValueError):
        pc.normalize_engine_priorities({"99": 1}, allowed_ids={11}, strict=True)
    # 常量唯一真源：service 认领侧默认档与写侧上限同源
    assert pc.ENGINE_PRIORITY_MIN == 1 and pc.ENGINE_PRIORITY_MAX == 9
    assert pc.ENGINE_PRIORITY_DEFAULT == 9


def test_normalize_task_priority_map_uses_shared_helper():
    """任务侧归一化复用唯一真源 helper：str/dict 入参、非法条目丢弃、键转 int。"""
    assert service._normalize_task_priority_map('{"1": 1, "2": 9}') == {1: 1, 2: 9}
    assert service._normalize_task_priority_map({"1": 1, "2": 0, "x": 5}) == {1: 1}
    assert service._normalize_task_priority_map("not-json") == {}
    assert service._normalize_task_priority_map("") == {}
    assert service._normalize_task_priority_map(None) == {}


def test_get_scene_engine_priorities_signature_no_issue_creator():
    """QA 判断项：get_scene_engine_priorities 不再携带未用的 issue_creator 参数。"""
    import inspect
    import src.domains.runtime.engine.engine_select as es
    params = list(inspect.signature(es.get_scene_engine_priorities).parameters)
    assert "issue_creator" not in params
    assert params[:4] == ["scene_id", "agent_type", "repo_key", "user_id"]


# ---------- QA #3564 二次复审补充：幽灵执行器闸 / 缓存陈旧健康分 / 禁用清优先级 ----------

def test_high_priority_pending_ghost_low_claims(memdb):
    """QA-2（幽灵执行器闸）：pending 且无 worker_version 的远端高优引擎 → 不压制低优。

    _engine_claimable_now 必须与 claim_by_tag 认领前闸同口径：probe_status=pending、
    worker_version 为空的远端引擎没有可信执行器（seq=9491 事故同型），不构成可接取，
    低优引擎直接认领，不被压到 120s 超时旁路。
    """
    _high_idle(memdb, credential_id=13, probe_status="pending", worker_version="")
    _low(memdb)
    tid = _task(memdb, "t-prio-ghost", prio={"1": 1, "2": 9})
    claimed = service.claim_by_tag("vod", 2)
    assert claimed and claimed["id"] == tid


def test_high_priority_spawn_failed_low_claims(memdb):
    """QA-2 对照：spawn_failed 高优引擎（Issue #2133 黑名单状态）同样不构成可接取。"""
    _high_idle(memdb, credential_id=13, probe_status="spawn_failed", worker_version="1.0")
    _low(memdb)
    tid = _task(memdb, "t-prio-spawnfail", prio={"1": 1, "2": 9})
    claimed = service.claim_by_tag("vod", 2)
    assert claimed and claimed["id"] == tid


def test_high_priority_local_uninstalled_low_claims(memdb):
    """QA-2 对照：本机高优引擎未安装（installed=0）→ 与 claim_by_tag 同口径不压制。"""
    _high_idle(memdb, installed=0)
    _low(memdb)
    tid = _task(memdb, "t-prio-local-uninst", prio={"1": 1, "2": 9})
    claimed = service.claim_by_tag("vod", 2)
    assert claimed and claimed["id"] == tid


def test_high_priority_verified_remote_still_suppresses(memdb):
    """QA-2 对照组：远端高优引擎探活 ok 且带 worker_version → 仍正常压制低优。"""
    _high_idle(memdb, credential_id=13, probe_status="ok", worker_version="1.4")
    _low(memdb)
    tid = _task(memdb, "t-prio-remote-ok", prio={"1": 1, "2": 9})
    assert service.claim_by_tag("vod", 2) is None
    assert _task_status(memdb, tid) == "queued"
    claimed = service.claim_by_tag("vod", 1)
    assert claimed and claimed["id"] == tid


def test_high_priority_stale_cached_health_low_claims(memdb, monkeypatch):
    """QA-1（健康分尺子）：优先级闸必须用 force_refresh=True 现算健康分。

    模拟 60s 缓存陈旧分：is_healthy 不带 force_refresh 时返回 False（陈旧 0 分快照），
    force_refresh=True 时返回 True（现算健康）。若闸内读缓存，高优引擎会被误判为
    低分不可接取之外的反例——本用例反向固定：只有现算口径下高优才可接取并压制低优。
    """
    import src.domains.runtime.engine.health_scorer as _hs

    def _stale_cache(eid, *a, **k):
        if int(eid) != 1:
            return True
        return bool(k.get("force_refresh"))

    _high_idle(memdb)
    _low(memdb)
    monkeypatch.setattr(_hs, "is_healthy", _stale_cache)
    tid = _task(memdb, "t-prio-stalecache", prio={"1": 1, "2": 9})
    # 高优引擎现算健康（force_refresh=True → True）→ 构成可接取 → 压制低优
    assert service.claim_by_tag("vod", 2) is None
    assert _task_status(memdb, tid) == "queued"
    claimed = service.claim_by_tag("vod", 1)
    assert claimed and claimed["id"] == tid


def test_priority_gate_health_call_uses_force_refresh(monkeypatch):
    """QA-1 契约看护：_engine_claimable_now 调用 is_healthy 必须显式 force_refresh=True
    （与 claim_by_tag 的健康分调用同一把尺子，不读 60s 缓存陈旧分）。"""
    import inspect
    import re as _re
    src = inspect.getsource(service._engine_claimable_now)
    assert _re.search(r"\w*healthy\w*\(\s*engine_id\s*,\s*force_refresh\s*=\s*True\s*\)", src), \
        "_engine_claimable_now 健康分调用必须 is_healthy(engine_id, force_refresh=True)"


def test_priority_gate_calls_has_verified_executor():
    """QA-2 契约看护：_engine_claimable_now 必须走 _has_verified_executor 幽灵执行器闸。"""
    import inspect
    src = inspect.getsource(service._engine_claimable_now)
    assert "_has_verified_executor(" in src, \
        "_engine_claimable_now 缺幽灵执行器闸（须与 claim_by_tag 同口径）"


def test_priority_default_constant_single_source():
    """QA 判断项：ENGINE_PRIORITY_DEFAULT 模块级唯一真源（service 不再函数内重复兜底）。"""
    import src.domains.mgmt.pipeline_config as pc
    assert service.ENGINE_PRIORITY_DEFAULT == pc.ENGINE_PRIORITY_DEFAULT == 9
    import inspect
    gate_src = inspect.getsource(service._claim_priority_gate)
    assert "from src.api.pipeline_config import ENGINE_PRIORITY_DEFAULT" not in gate_src, \
        "_claim_priority_gate 不得函数内重复 import 常量"


def test_toggle_agent_disable_clears_engine_priorities():
    """QA-3 前端回归：toggleAgent 禁用节点必须同清 engine_priorities 孤儿键。

    禁用后再启用且未重开引擎弹窗时，带孤儿优先级保存会被后端 strict 校验
    （scenes.py 保存校验）以 400 拒绝。前端无单测设施，按仓库静态守卫惯例看护源码契约。
    """
    from pathlib import Path
    import re as _re
    vue = Path(__file__).resolve().parents[2] / (
        "src/api/frontend/app/src/views/DashboardView.vue")
    src = vue.read_text(encoding="utf-8")
    m = _re.search(r"function toggleAgent\(agentType\) \{.*?\n\}", src, _re.S)
    assert m, "toggleAgent not found in DashboardView.vue"
    body = m.group(0)
    assert "node.engine_runtime_ids = []" in body
    assert "node.engine_priorities = {}" in body, \
        "toggleAgent 禁用分支必须同清 engine_priorities（孤儿优先级防 400）"


def test_confirm_engine_pick_uid_mode_clears_priorities():
    """QA rebase 复核：uid 绑定模式（D11 全 uid 提交、ids 清空）必须同清优先级。

    实例模式选中引擎全部带 engine_uid 时前端提交 engine_runtime_uids 并清空
    engine_runtime_ids，此时若仍写 node.engine_priorities，后端 strict 校验
    （allowed 仅取 engine_runtime_ids）必 400。前端无单测设施，按仓库静态守卫惯例看护。
    """
    from pathlib import Path
    import re as _re
    vue = Path(__file__).resolve().parents[2] / (
        "src/api/frontend/app/src/views/DashboardView.vue")
    src = vue.read_text(encoding="utf-8")
    m = _re.search(r"function confirmEnginePick\(\) \{.*?\n\}", src, _re.S)
    assert m, "confirmEnginePick not found in DashboardView.vue"
    body = m.group(0)
    assert _re.search(
        r"mode === 'tag'\s*\|\|\s*\(node\.engine_runtime_uids\s*\|\|\s*\[\]\)\.length", body), \
        "confirmEnginePick 优先级清空条件必须覆盖 uid 绑定模式（tag || uids 非空）"
    assert "node.engine_priorities = {}" in body


def test_uid_binding_with_priorities_rejected_by_strict_validation():
    """QA rebase 复核：uid 绑定 + 非空 engine_priorities 的保存路径必须显式 400。

    后端优先级校验 allowed 只来自 engine_runtime_ids（uid 模式为空），键必然非法。
    前端已在 confirmEnginePick 的 uid 分支同清优先级；本条看护服务端兜底契约，
    防其它客户端提交 uid+priorities 后静默落库或不可诊断。
    """
    from fastapi import HTTPException
    from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids

    nodes = [{
        "id": "vod",
        "tag": "vod",
        "enabled": True,
        "engine_runtime_ids": [],
        "engine_runtime_uids": ["a" * 16],
        "engine_selector": {},
        "engine_priorities": {"1": 1},
    }]
    with pytest.raises(HTTPException) as ei:
        _validate_scene_engine_ids(nodes, "gitcode:o/r", "")
    assert ei.value.status_code == 400
    assert "已绑定引擎列表" in str(ei.value.detail)


# ── QA 2026-11-24 复审：迁移域/后置校验 + SRE 复制回归 ──────────────────────

def test_engine_priorities_migration_declares_runtime_domain_and_postcheck():
    """QA 硬违规回归：agent_tasks owner=runtime，迁移必须声明 runtime 域且带 post_check。

    缺 `domains=("runtime",)` 时 register_migration 默认判 mgmt：三库模式下 mgmt 进程
    跑到跨库视图只盖章不建列、event(runtime) 进程被域过滤永不执行 → owner 库永远缺列。
    """
    from src.api import db_setup

    mig = next(m for m in db_setup._MIGRATIONS
               if m.name == "issue3564-agent-tasks-engine-priorities")
    assert mig.version == "202611240011", "迁移号需为未占用的新号（旧 202611240003 已在 mgmt 盖章）"
    assert mig.domains == ("runtime",), "agent_tasks 归属 runtime 域，迁移必须显式声明"
    assert mig.post_check is not None, "缺列迁移必须带 post_check（Issue #3648）"


def test_engine_priorities_migration_filtered_to_runtime_only(monkeypatch):
    """QA 回归：3 库域过滤只在 runtime 进程执行该迁移，mgmt 进程不得拿到。"""
    from src.api import db_setup

    monkeypatch.setenv("GRAPE_DOMAIN_LIB", "1")
    monkeypatch.setenv("GRAPE_MIGRATION_OWNER", "runtime")
    versions = {m.version for m in db_setup.filter_migration_registry(db_setup._MIGRATIONS)}
    assert "202611240011" in versions

    monkeypatch.setenv("GRAPE_MIGRATION_OWNER", "mgmt")
    versions = {m.version for m in db_setup.filter_migration_registry(db_setup._MIGRATIONS)}
    assert "202611240011" not in versions


def test_engine_priorities_migration_skips_view_and_checks_column(monkeypatch):
    """QA 回归：非 owner 库/视图跳过 ALTER；owner 基表缺列时 post_check 不盖章。"""
    import contextlib
    from src.api import db_setup

    class _DB:
        def cursor(self):
            return object()

        def commit(self):
            pass

    monkeypatch.setattr(db_setup, "db", _DB())
    monkeypatch.setattr(db_setup, "db_lock", contextlib.nullcontext())
    called = {"ensure": 0}
    monkeypatch.setattr(db_setup, "_table_present", lambda c, t: True)
    monkeypatch.setattr(db_setup, "_is_base_table", lambda c, t: False)
    monkeypatch.setattr(db_setup, "_ensure_column",
                        lambda *a, **k: called.__setitem__("ensure", called["ensure"] + 1))
    db_setup._fn_add_agent_tasks_engine_priorities()
    assert called["ensure"] == 0, "跨库视图不得 ALTER（列归属 owner 基表）"
    db_setup._postcheck_agent_tasks_engine_priorities()  # 视图场景放行（无列属正常）

    monkeypatch.setattr(db_setup, "_is_base_table", lambda c, t: True)
    monkeypatch.setattr(db_setup, "_uc_has_column", lambda c, t, col: False)
    with pytest.raises(RuntimeError):
        db_setup._postcheck_agent_tasks_engine_priorities()

    monkeypatch.setattr(db_setup, "_uc_has_column", lambda c, t, col: True)
    db_setup._postcheck_agent_tasks_engine_priorities()  # 列在位 → 盖章


def test_copy_scene_does_not_auto_disable_sre():
    """QA Spec 回归：复制场景不得按 eids 为空自动停用 SRE 节点（Issue #3396）。

    uid 绑定的 SRE 节点（engine_runtime_uids 有值、engine_runtime_ids 为空）在复制
    清洗后 eids 恒空，若前端按空 eids 停用会误停用广播模式节点；启停交服务端。
    """
    from pathlib import Path
    import re as _re

    vue = Path(__file__).resolve().parents[2] / (
        "src/api/frontend/app/src/views/DashboardView.vue")
    src = vue.read_text(encoding="utf-8")
    m = _re.search(r"function doCopyScene\(\) \{.*?\n\}", src, _re.S)
    assert m, "doCopyScene not found in DashboardView.vue"
    body = m.group(0)
    assert not _re.search(r"tag\s*===\s*'sre'.*?enabled\s*=\s*false", body, _re.S), \
        "复制场景不得按空 engine_runtime_ids 自动停用 SRE（空绑定=广播模式）"
