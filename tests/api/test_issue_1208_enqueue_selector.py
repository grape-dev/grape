"""Issue #1208 原子契约测试：入队三分叉 + 零命中 fail-closed 文案分流。

覆盖（Issue 正文 §2.3 / §3 验收 1/2/5 / §4 实施顺序 4-5）：
- get_scene_engine_selector / get_scene_engine_uids：node 读侧（uids 优先回落 ids）；
- selector 候选集：agent_tags 派生匹配 → #658 角色硬过滤（正交勿合并）→ 健康 → SRE scope；
- selector_enqueue_failure_reason：配置死/瞬时死文案分流（单函数封装）；
- get_scene_pool_engine_ids：uid 绑定节点 uid 优先解析（uid 池路径）；id 节点行为零变化；
- _scene_pool_gate（event_pipeline 挂点 seam）：三分叉判据 + 零命中 fail-closed 不入队。
"""
import json
import sqlite3
import threading

import pytest


@pytest.fixture()
def sel_db(monkeypatch):
    """独立测试库：场景表 + 引擎三表（selector 入队链最小集）。"""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.executescript("""
    CREATE TABLE devops_pipeline_scene (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scene_id TEXT, name TEXT DEFAULT '',
        description TEXT DEFAULT '', nodes_json TEXT DEFAULT '[]', onboarding TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1, repo_key TEXT DEFAULT ''
    );
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
    # pipeline_config 进程级 60s 场景缓存按 (scene_id, repo_key) 命中——跨测试清空防串扰
    from src.api import pipeline_config as pc
    monkeypatch.setattr(pc, "_SCENE_CACHE", {})
    yield c
    c.close()


def _mk_scene(c, scene_id="sc-sel", nodes=None, repo_key="gitcode:o/r"):
    c.execute("INSERT INTO devops_pipeline_scene (scene_id, name, nodes_json, is_active, repo_key) "
              "VALUES (?,?,?,1,?)", (scene_id, "s", json.dumps(nodes or []), repo_key))


def _mk_engine(c, rid=1, name="opencode", cred=7, scope="repo", owner="o/r",
               status="online", seen=None, caps=("all",), role="",
               scene_tags='["default"]', uid=None):
    import datetime as _dt
    seen = seen or _dt.datetime.now(_dt.timezone.utc).isoformat()
    c.execute("INSERT INTO runtime_credentials (id, scope, owner_id, status) VALUES (?,?,?, 'active')",
              (cred, scope, owner))
    c.execute("INSERT INTO agent_runtimes (id, name, credential_id, scope, owner_id, status, healthy, "
              "installed, last_seen_at, role_tags, engine_uid, scene_tags) VALUES (?,?,?,?,?,?,1,1,?,?,?,?)",
              (rid, name, cred, scope, owner, status, seen, role, uid, scene_tags))
    for t in caps:
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)", (rid, t))


# ===== 读侧：get_scene_engine_selector / get_scene_engine_uids =====

class TestSceneSelectorRead:
    def test_selector_present(self, sel_db):
        from src.domains.runtime.engine.engine_select import get_scene_engine_selector
        _mk_scene(sel_db, nodes=[{"id": "coding", "tag": "coding", "enabled": True,
                                  "engine_selector": {"tags": ["scope:repo"]}}])
        sel = get_scene_engine_selector("sc-sel", "coding", repo_key="gitcode:o/r")
        assert sel == {"tags": ["scope:repo"]}

    def test_selector_absent_none(self, sel_db):
        from src.domains.runtime.engine.engine_select import get_scene_engine_selector
        _mk_scene(sel_db, nodes=[{"id": "coding", "tag": "coding", "enabled": True,
                                  "engine_runtime_ids": [1]}])
        assert get_scene_engine_selector("sc-sel", "coding", repo_key="gitcode:o/r") is None
        assert get_scene_engine_selector("sc-none", "coding", repo_key="gitcode:o/r") is None
        assert get_scene_engine_selector("sc-sel", "sre", repo_key="gitcode:o/r") is None

    def test_uids_priority_fallback_ids(self, sel_db):
        from src.domains.runtime.engine.engine_select import get_scene_engine_uids
        from src.api.pipeline_config import get_scene_engine_binding
        _mk_scene(sel_db, nodes=[{"id": "coding", "tag": "coding", "enabled": True,
                                  "engine_runtime_ids": [1, 2],
                                  "engine_runtime_uids": ["aaaaaaaa-1111-4111-8111-111111111111"]}])
        b = get_scene_engine_binding("sc-sel", "coding", repo_key="gitcode:o/r")
        assert b["status"] == "bound"
        assert b["engine_uids"] == ["aaaaaaaa-1111-4111-8111-111111111111"]  # uids 优先
        assert get_scene_engine_uids("sc-sel", "coding", repo_key="gitcode:o/r") == \
            ["aaaaaaaa-1111-4111-8111-111111111111"]
        # ids 回落：uids 空 → binding.engine_ids 走 ids（读侧零变化）；engine_uids 空
        _mk_scene(sel_db, scene_id="sc-id", nodes=[{"id": "coding", "tag": "coding",
                                                    "enabled": True, "engine_runtime_ids": [3]}])
        b2 = get_scene_engine_binding("sc-id", "coding", repo_key="gitcode:o/r")
        assert b2["engine_ids"] == [3] and b2["engine_uids"] == []


# ===== 候选集：tags 匹配 → 角色 → 健康 → SRE scope =====

class TestSelectorCandidates:
    def test_matched_and_dropped_reasons(self, sel_db):
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        from src.domains.runtime.engine.engine_select import selector_candidate_engines
        # 引擎 A：scope=repo 凭据 → scope:repo 命中；引擎 B：global → 不命中
        _mk_engine(sel_db, rid=1, name="opencode", cred=7, scope="repo", owner="o/r", caps=("all",))
        _mk_engine(sel_db, rid=2, name="hermes", cred=8, scope="global", owner="", caps=("all",))
        for rid, name, sc in ((1, "opencode", "repo"), (2, "hermes", "global")):
            st = sel_db.execute("SELECT scene_tags FROM agent_runtimes WHERE id=?", (rid,)).fetchone()[0]
            sync_derived_agent_tags(sel_db.cursor(), rid, name, sc, st)
        sel_db.commit()
        sel = {"tags": ["scope:repo"]}
        matched, dropped = selector_candidate_engines(sel, "coding", repo_key="gitcode:o/r")
        assert [m["runtime_id"] for m in matched] == [1]
        assert any(d["runtime_id"] == 2 and d["reason"] == "selector" for d in dropped)

    def test_role_filter_after_selector(self, sel_db):
        # #658 正交：selector 命中但角色不符 → 角色排除（reason=role），不与 selector 混淆
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        from src.domains.runtime.engine.engine_select import selector_candidate_engines
        _mk_engine(sel_db, rid=1, name="opencode", cred=7, scope="repo", owner="o/r",
                   caps=("all",), role='["coding"]')
        st = sel_db.execute("SELECT scene_tags FROM agent_runtimes WHERE id=1").fetchone()[0]
        sync_derived_agent_tags(sel_db.cursor(), 1, "opencode", "repo", st)
        sel_db.commit()
        matched, dropped = selector_candidate_engines({"tags": ["scope:repo"]}, "sre",
                                                      repo_key="gitcode:o/r")
        assert matched == []
        assert any(d["runtime_id"] == 1 and d["reason"] == "role" for d in dropped)

    def test_health_filter(self, sel_db):
        # 命中引擎全离线 → 0 命中 + reason=offline（瞬时死判定输入）
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        from src.domains.runtime.engine.engine_select import selector_candidate_engines
        _mk_engine(sel_db, rid=1, name="opencode", cred=7, scope="repo", owner="o/r",
                   status="offline", caps=("all",))
        st = sel_db.execute("SELECT scene_tags FROM agent_runtimes WHERE id=1").fetchone()[0]
        sync_derived_agent_tags(sel_db.cursor(), 1, "opencode", "repo", st)
        sel_db.commit()
        matched, dropped = selector_candidate_engines({"tags": ["scope:repo"]}, "coding",
                                                      repo_key="gitcode:o/r")
        assert matched == []
        assert any(d["runtime_id"] == 1 and d["reason"] in ("offline", "stale") for d in dropped)

    def test_sre_scope_filter_after_selector(self, sel_db):
        # SRE：global 档不放行（在 selector 匹配之后叠加，语义不变）
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        from src.domains.runtime.engine.engine_select import selector_candidate_engines
        _mk_engine(sel_db, rid=1, name="hermes", cred=7, scope="global", owner="", caps=("all",))
        st = sel_db.execute("SELECT scene_tags FROM agent_runtimes WHERE id=1").fetchone()[0]
        sync_derived_agent_tags(sel_db.cursor(), 1, "hermes", "global", st)
        sel_db.commit()
        matched, dropped = selector_candidate_engines({"tags": ["scope:global"]}, "sre",
                                                      repo_key="gitcode:o/r")
        assert matched == []
        assert any(d["runtime_id"] == 1 and d["reason"] == "scope" for d in dropped)


# ===== 文案分流（单函数封装）=====

class TestSelectorFailureReason:
    def test_config_dead(self, sel_db):
        # 引擎从来没带过该标签（写错永远不会好）→ 配置死文案
        from src.domains.runtime.engine.engine_select import selector_enqueue_failure_reason
        _mk_engine(sel_db, rid=1, name="opencode", cred=7, scope="repo", owner="o/r", caps=("all",))
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        st = sel_db.execute("SELECT scene_tags FROM agent_runtimes WHERE id=1").fetchone()[0]
        sync_derived_agent_tags(sel_db.cursor(), 1, "opencode", "repo", st)
        sel_db.commit()
        reason = selector_enqueue_failure_reason({"tags": ["engine:codearts"]}, "coding",
                                                 repo_key="gitcode:o/r")
        assert "无引擎满足标签" in reason
        assert "engine:codearts" in reason

    def test_transient_dead(self, sel_db):
        # 标签有引擎带过但全离线 → 瞬时死文案（不下死刑结论）
        from src.domains.runtime.engine.engine_select import selector_enqueue_failure_reason
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        _mk_engine(sel_db, rid=1, name="opencode", cred=7, scope="repo", owner="o/r",
                   status="offline", caps=("all",))
        st = sel_db.execute("SELECT scene_tags FROM agent_runtimes WHERE id=1").fetchone()[0]
        sync_derived_agent_tags(sel_db.cursor(), 1, "opencode", "repo", st)
        sel_db.commit()
        reason = selector_enqueue_failure_reason({"tags": ["scope:repo"]}, "coding",
                                                 repo_key="gitcode:o/r")
        assert "均不在线" in reason or "稍后" in reason
        assert "无引擎满足标签" not in reason


# ===== 入队池解析三分叉 =====

class TestPoolGateFork:
    def test_selector_mode_healthy_pool(self, sel_db):
        # selector 非空 → selector 路径：pool_selector 落 JSON、pool_engine_ids 置空
        from src.domains.runtime.pipeline.event_pipeline import _scene_pool_gate
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        _mk_scene(sel_db, nodes=[{"id": "coding", "tag": "coding", "enabled": True,
                                  "engine_selector": {"tags": ["scope:repo"]}}])
        _mk_engine(sel_db, rid=1, name="opencode", cred=7, scope="repo", owner="o/r", caps=("all",))
        st = sel_db.execute("SELECT scene_tags FROM agent_runtimes WHERE id=1").fetchone()[0]
        sync_derived_agent_tags(sel_db.cursor(), 1, "opencode", "repo", st)
        sel_db.commit()
        mode, payload, fail = _scene_pool_gate("sc-sel", "coding", repo_key="gitcode:o/r")
        assert mode == "selector"
        assert json.loads(payload["pool_selector"]) == {"tags": ["scope:repo"]}
        assert payload["pool_engine_ids"] == ""
        assert fail is None

    def test_selector_mode_zero_hit_fail_closed(self, sel_db):
        # 零命中 → fail-closed（不入队 + 配置死文案）
        from src.domains.runtime.pipeline.event_pipeline import _scene_pool_gate
        _mk_scene(sel_db, nodes=[{"id": "coding", "tag": "coding", "enabled": True,
                                  "engine_selector": {"tags": ["engine:codearts"]}}])
        mode, payload, fail = _scene_pool_gate("sc-sel", "coding", repo_key="gitcode:o/r")
        assert mode is None and payload is None
        assert "无引擎满足标签" in (fail or "")

    def test_uid_mode(self, sel_db):
        # uids 非空 → uid 池路径（健康过滤后写 pool_engine_ids，uid 镜像由快照链路双写）
        from src.domains.runtime.pipeline.event_pipeline import _scene_pool_gate
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        _uid1 = "aaaaaaaa-1111-4111-8111-111111111111"
        _mk_scene(sel_db, nodes=[{"id": "coding", "tag": "coding", "enabled": True,
                                  "engine_runtime_uids": [_uid1]}])
        _mk_engine(sel_db, rid=1, name="opencode", cred=7, scope="repo", owner="o/r",
                   caps=("all",), uid=_uid1)
        st = sel_db.execute("SELECT scene_tags FROM agent_runtimes WHERE id=1").fetchone()[0]
        sync_derived_agent_tags(sel_db.cursor(), 1, "opencode", "repo", st)
        sel_db.commit()
        mode, payload, fail = _scene_pool_gate("sc-sel", "coding", repo_key="gitcode:o/r")
        assert mode == "pool" and json.loads(payload["pool_engine_ids"]) == [1]

    def test_id_mode_zero_change(self, sel_db):
        # legacy id 路径行为零变化（绑定全灭 → None）
        from src.domains.runtime.pipeline.event_pipeline import _scene_pool_gate
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        _mk_scene(sel_db, nodes=[{"id": "coding", "tag": "coding", "enabled": True,
                                  "engine_runtime_ids": [1]}])
        _mk_engine(sel_db, rid=1, name="opencode", cred=7, scope="repo", owner="o/r", caps=("all",))
        st = sel_db.execute("SELECT scene_tags FROM agent_runtimes WHERE id=1").fetchone()[0]
        sync_derived_agent_tags(sel_db.cursor(), 1, "opencode", "repo", st)
        sel_db.commit()
        mode, payload, fail = _scene_pool_gate("sc-sel", "coding", repo_key="gitcode:o/r")
        assert mode == "pool" and json.loads(payload["pool_engine_ids"]) == [1]
        # 全灭 → None（#259 语义不变）
        sel_db.execute("UPDATE agent_runtimes SET status='offline' WHERE id=1")
        sel_db.commit()
        mode2, payload2, fail2 = _scene_pool_gate("sc-sel", "coding", repo_key="gitcode:o/r")
        assert mode2 == "pool" and payload2 is not None and payload2["pool_engine_ids"] is None
