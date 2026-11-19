"""Issue #1208 追加修复：selector 候选集必须叠加 claim 第 ⑥ 级 scope 隔离。

2026-11-19 线上现象（beta，场景 356339a6…「通用开发-copy」issue 节点，selector
= engine:opencode）：场景详情页徽章「标签命中 6 个引擎 · 6 在线」，弹窗命中预览
「4 个引擎 · 4 在线」。差出来的 2 台是别人的 user 档引擎与别的仓库的 repo 档引擎
——claim_by_tag 的 scope SQL 永远认领不到本场景任务，但 selector_candidate_engines
此前对非 SRE 角色不做 owner/scope 判定，把它们算成命中。

后果不止展示：入队 fail-closed 门禁（selector_enqueue_failure_reason）用的是同一把
尺子，若某 selector 只有外档引擎命中，任务会照常入队、无人认领、卡到超时。

本文件锁定修复口径：候选集 = 真正可能认领到本场景任务的引擎集合。
"""
import datetime as _dt
import sqlite3
import threading

import pytest


@pytest.fixture()
def scope_db(monkeypatch):
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


def _mk_engine(c, rid, name="opencode", cred=7, scope="repo", owner="o/r",
               status="online"):
    seen = _dt.datetime.now(_dt.timezone.utc).isoformat()
    c.execute("INSERT INTO runtime_credentials (id, scope, owner_id, status) "
              "VALUES (?,?,?,'active')", (cred, scope, owner))
    c.execute("INSERT INTO agent_runtimes (id, name, credential_id, scope, owner_id, status, "
              "healthy, installed, last_seen_at) VALUES (?,?,?,?,?,?,1,1,?)",
              (rid, name, cred, scope, owner, status, seen))
    # 能力行：'all' 让 claim 第 1 级能力匹配放行（selector 派生标签由 sync 补齐）
    c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)",
              (rid, "all"))
    from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
    st = c.execute("SELECT scene_tags FROM agent_runtimes WHERE id=?", (rid,)).fetchone()[0]
    sync_derived_agent_tags(c.cursor(), rid, name, scope, st)
    c.commit()


def _ids(matched):
    return [m["runtime_id"] for m in matched]


def _reason(dropped, rid):
    return next((d["reason"] for d in dropped if d["runtime_id"] == rid), "")


SEL = {"tags": ["engine:opencode"]}
REPO = "gitcode:And-ZJ2/snake-game8081"


def test_foreign_user_engine_not_counted(scope_db):
    """别人的 user 档引擎：标签命中但认领不到本用户任务 → reason=scope，不计命中。"""
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=11, scope="user", owner="3")
    matched, dropped = selector_candidate_engines(SEL, "issue", repo_key=REPO, user_id=57)
    assert _ids(matched) == []
    assert _reason(dropped, 1) == "scope"


def test_own_user_engine_counted(scope_db):
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=11, scope="user", owner="57")
    matched, dropped = selector_candidate_engines(SEL, "issue", repo_key=REPO, user_id=57)
    assert _ids(matched) == [1] and dropped == []


def test_other_repo_engine_not_counted(scope_db):
    """别的仓库的 repo 档引擎：claim 只认领本仓任务 → 不计命中。"""
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=21, scope="repo",
               owner="gitcode:And-ZJ2/hello-calculator")
    matched, dropped = selector_candidate_engines(SEL, "issue", repo_key=REPO, user_id=57)
    assert _ids(matched) == []
    assert _reason(dropped, 1) == "scope"


def test_same_repo_engine_counted_with_or_without_platform_prefix(scope_db):
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=21, scope="repo",
               owner="And-ZJ2/snake-game8081")          # 不带前缀
    _mk_engine(scope_db, 2, name="opencode", cred=22, scope="repo", owner=REPO)  # 带前缀
    matched, _dropped = selector_candidate_engines(SEL, "issue", repo_key=REPO, user_id=57)
    assert _ids(matched) == [1, 2]


def test_local_and_global_engines_counted(scope_db):
    """本机直连（credential_id=0）与 global 档不受 scope 隔离影响。"""
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=0, scope="global", owner="")
    _mk_engine(scope_db, 2, name="opencode", cred=31, scope="global", owner="And-ZJ2")
    matched, _dropped = selector_candidate_engines(SEL, "issue", repo_key=REPO, user_id=57)
    assert _ids(matched) == [1, 2]


def test_missing_context_keeps_previous_behavior(scope_db):
    """调用方没给 user_id / repo_key 时该维度不设限（宁放行不误杀）。"""
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    _mk_engine(scope_db, 1, name="opencode", cred=11, scope="user", owner="3")
    _mk_engine(scope_db, 2, name="opencode", cred=21, scope="repo", owner="gitcode:o/other")
    matched, _dropped = selector_candidate_engines(SEL, "issue")
    assert _ids(matched) == [1, 2]


def test_enqueue_gate_rejects_when_only_foreign_engines_match(scope_db):
    """只有外档引擎时，入队 fail-closed 必须生效（修复前会误判「有命中」而入队）。"""
    from src.domains.runtime.engine.engine_select import selector_enqueue_failure_reason
    _mk_engine(scope_db, 1, name="opencode", cred=11, scope="user", owner="3")
    _mk_engine(scope_db, 2, name="opencode", cred=21, scope="repo",
               owner="gitcode:And-ZJ2/hello-calculator")
    reason = selector_enqueue_failure_reason(SEL, "issue", repo_key=REPO, user_id=57)
    assert reason, "外档引擎不应被算作可承接命中"


def test_preview_endpoint_marks_foreign_engines_scope(scope_db):
    """预览接口与候选集同尺：外档引擎 hit=False / reason=scope（徽章不再多报）。"""
    from src.domains.mgmt.routes.scenes import EngineSelectorPreviewBody, engine_selector_preview
    _mk_engine(scope_db, 1, name="opencode", cred=11, scope="user", owner="3")
    _mk_engine(scope_db, 2, name="opencode", cred=21, scope="repo",
               owner="gitcode:And-ZJ2/hello-calculator")
    _mk_engine(scope_db, 3, name="opencode", cred=31, scope="global", owner="And-ZJ2")
    out = engine_selector_preview(
        EngineSelectorPreviewBody(tags=SEL["tags"], agent_type="issue",
                                  repo="And-ZJ2/snake-game8081"),
        user={"id": 57})
    assert out["matched"] == [3]
    assert out["engines"]["1"] == {"hit": False, "reason": "scope"}
    assert out["engines"]["2"] == {"hit": False, "reason": "scope"}
    assert out["engines"]["3"] == {"hit": True, "reason": ""}