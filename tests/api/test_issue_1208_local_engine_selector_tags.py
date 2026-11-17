"""Issue #1208 回归：本机直连引擎（credential_id=0）必须带 engine:/scope:/scene: 派生标签。

缺陷现场：本机直连引擎只带平铺 'all' 能力行，没有 engine:<name> —— 场景按 engine:<name>
圈池时命中预览把它判成 reason=selector（标签不匹配），而远端 worker 注册的同名引擎却能命中。

根因：本机直连引擎不走 worker 注册链，也没有 worker 心跳（心跳链派生刷新按
`r.credential_id=?` 过滤，0 恒不匹配）。upsert_local_runtimes 是这些行唯一写入口，
本测试锁定它必须在 INSERT/UPDATE 两条分支都补齐派生标签。
"""
import datetime as _dt
import sqlite3
import threading

import pytest

from src.domains.runtime import public as rt


@pytest.fixture()
def local_db(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.executescript("""
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT DEFAULT '', display_name TEXT DEFAULT '',
        credential_id INTEGER DEFAULT 0, scope TEXT DEFAULT 'global',
        owner_id TEXT DEFAULT '', owner_name TEXT DEFAULT '', status TEXT DEFAULT 'offline',
        healthy INTEGER DEFAULT 1, installed INTEGER DEFAULT 1, last_seen_at TEXT,
        updated_at TEXT DEFAULT '', cli_path TEXT DEFAULT '', version TEXT DEFAULT '',
        pip_package TEXT DEFAULT '', local_skills TEXT DEFAULT '[]',
        skill_scan_result TEXT DEFAULT '', host_name TEXT DEFAULT '',
        probe_status TEXT DEFAULT 'ok', probe_latency INTEGER DEFAULT 0,
        probe_error TEXT DEFAULT '', probe_last TEXT DEFAULT '',
        probe_ok_streak INTEGER DEFAULT 0, engine_uid VARCHAR(36) DEFAULT NULL,
        scene_tags TEXT DEFAULT '["default"]', runtime_info TEXT DEFAULT '',
        role_tags TEXT DEFAULT '', agent_tags TEXT DEFAULT '[]', org_id INTEGER DEFAULT 0
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
    """)
    import src.kernel.session_manager as sm
    from src.kernel import db_registry
    monkeypatch.setattr(sm, "db", c)
    monkeypatch.setattr(sm, "db_lock", threading.RLock())
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda domain: False)
    yield c
    c.close()


def _now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _agent(name):
    return {"name": name, "installed": True, "healthy": True,
            "display_name": name.capitalize(), "cli_path": f"/usr/bin/{name}",
            "version": "1.0"}


def test_new_local_engine_gets_derived_tags_and_hits_selector(local_db):
    from src.domains.runtime.engine.engine_select import selector_candidate_engines

    out = rt.upsert_local_runtimes([_agent("hermes")], "global", "", "host-a", _now())
    rid = out["ids"]["hermes"]

    tags = {r[0] for r in local_db.execute(
        "SELECT tag FROM engine_capabilities WHERE runtime_id=?", (rid,)).fetchall()}
    assert {"engine:hermes", "scope:global", "scene:default"} <= tags
    assert "all" in tags  # 平铺能力行不被派生逻辑吞掉

    snapshot = local_db.execute("SELECT agent_tags FROM agent_runtimes WHERE id=?", (rid,)).fetchone()[0]
    assert "engine:hermes" in snapshot

    matched, dropped = selector_candidate_engines({"tags": ["engine:hermes"]}, "issue")
    assert rid in [m["runtime_id"] for m in matched]
    assert not [d for d in dropped if d["runtime_id"] == rid]


def test_existing_local_engine_without_derived_tags_is_backfilled(local_db):
    """缺陷现场复刻：行已存在且只有 'all'（旧版 upsert 的产物）。"""
    from src.domains.runtime.engine.engine_select import selector_candidate_engines

    seen = _now()
    local_db.execute(
        "INSERT INTO agent_runtimes (id, name, credential_id, scope, status, healthy, installed, "
        "last_seen_at, host_name, scene_tags, agent_tags) VALUES (17365,'hermes',0,'global',"
        "'online',1,1,?,'host-a','[\"default\"]','[\"all\"]')", (seen,))
    local_db.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (17365,'all',1)")
    local_db.commit()

    out = rt.upsert_local_runtimes([_agent("hermes")], "global", "", "host-a", seen)
    assert out["upserted"] == 1 and out["ids"]["hermes"] == 17365

    two_db, dropped_before = selector_candidate_engines({"tags": ["engine:hermes"]}, "issue")
    assert 17365 in [m["runtime_id"] for m in two_db]

    tags = {r[0] for r in local_db.execute(
        "SELECT tag FROM engine_capabilities WHERE runtime_id=17365").fetchall()}
    assert {"engine:hermes", "scope:global", "scene:default"} <= tags


def test_derived_tags_idempotent_on_repeated_probe(local_db):
    for _ in range(3):
        rt.upsert_local_runtimes([_agent("hermes")], "global", "", "host-a", _now())
    rows = local_db.execute(
        "SELECT tag FROM engine_capabilities WHERE tag LIKE '%:%'").fetchall()
    assert sorted(r[0] for r in rows) == ["engine:hermes", "scene:default", "scope:global"]
