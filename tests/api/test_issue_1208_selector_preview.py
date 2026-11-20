"""Issue #1208 v3 一致性契约测试：前端命中预览与后端判定同源同尺。

预览接口 /api/scenes/engine-selector-preview 必须复用 selector_candidate_engines
（入队/认领同一判定），因此同一 selector 下：
- 接口 engines 中 hit=True 的 runtime_id 集合 == selector_candidate_engines matched 集合；
- hit=False 的引擎都带分层 reason（selector/role/offline/stale/scope/capability）。
"""
import datetime as _dt
import json
import sqlite3
import threading

import pytest


@pytest.fixture()
def preview_db(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.executescript("""
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT DEFAULT '', credential_id INTEGER DEFAULT 0,
        scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '', owner_name TEXT DEFAULT '',
        status TEXT DEFAULT 'offline', healthy INTEGER DEFAULT 1, installed INTEGER DEFAULT 1,
        last_seen_at TEXT, scene_tags TEXT DEFAULT '["default"]', role_tags TEXT DEFAULT '',
        engine_uid VARCHAR(36) DEFAULT NULL, org_id INTEGER DEFAULT 0
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
    monkeypatch.setattr(sm, "db", c)
    monkeypatch.setattr(sm, "db_lock", threading.RLock())
    yield c
    c.close()


def _mk_engine(c, rid, name, cred, scope, owner, status="online", caps=("all",), role="",
               scene_tags='["default"]'):
    seen = _dt.datetime.now(_dt.timezone.utc).isoformat()
    c.execute("INSERT INTO runtime_credentials (id, scope, owner_id, status) VALUES (?,?,?,'active')",
              (cred, scope, owner))
    c.execute("INSERT INTO agent_runtimes (id, name, credential_id, scope, owner_id, status, healthy, "
              "installed, last_seen_at, role_tags, scene_tags) VALUES (?,?,?,?,?,?,1,1,?,?,?)",
              (rid, name, cred, scope, owner, status, seen, role, scene_tags))
    for t in caps:
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)", (rid, t))


def _sync(c):
    from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
    rows = c.execute("SELECT id, name, scope, scene_tags FROM agent_runtimes").fetchall()
    for rid, name, scope, st in rows:
        sync_derived_agent_tags(c.cursor(), rid, name, scope, st)
    c.commit()


def test_preview_matches_backend_candidate_set(preview_db):
    from src.domains.mgmt.routes.scenes import EngineSelectorPreviewBody, engine_selector_preview
    from src.domains.runtime.engine.engine_select import selector_candidate_engines

    # A: scope=repo 命中；B: scope=global 不命中；C: 命中但角色不符
    _mk_engine(preview_db, 1, "opencode", 7, "repo", "o/r")
    _mk_engine(preview_db, 2, "hermes", 8, "global", "")
    _mk_engine(preview_db, 3, "opencode", 9, "repo", "o/r", role='["qa"]')
    _sync(preview_db)

    selector = {"tags": ["scope:repo"]}
    body = EngineSelectorPreviewBody(tags=selector["tags"], agent_type="coding", repo="o/r")
    out = engine_selector_preview(body, user={"id": 0})
    matched, _dropped = selector_candidate_engines(selector, "coding", repo_key="gitcode:o/r")

    hit_ids = {int(k) for k, v in out["engines"].items() if v.get("hit")}
    assert hit_ids == {m["runtime_id"] for m in matched}
    assert hit_ids == {1}
    assert out["engines"]["2"]["hit"] is False and out["engines"]["2"]["reason"] == "selector"
    assert out["engines"]["3"]["hit"] is False and out["engines"]["3"]["reason"] == "role"
    assert set(out["matched"]) == hit_ids


def test_preview_empty_selector_returns_no_constraint(preview_db):
    from src.domains.mgmt.routes.scenes import EngineSelectorPreviewBody, engine_selector_preview
    _mk_engine(preview_db, 1, "opencode", 7, "repo", "o/r")
    _sync(preview_db)
    out = engine_selector_preview(
        EngineSelectorPreviewBody(tags=[], agent_type="coding", repo="o/r"), user={"id": 0})
    # 空 selector 不约束：接口返回空 maps，前端按全命中处理（不落库）
    assert out["engines"] == {}