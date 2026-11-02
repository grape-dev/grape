"""Issue #653：场景节点 engine_runtime_ids 派发硬约束（空值语义单点收口）。

验证点：
1. get_scene_engine_binding 四态：no_scene / no_node / unbound / bound
2. get_scene_pool_engine_ids 严格模式（默认开启）：
   - bound 且引擎行缺失 → None（显式报错，绝不广播）
   - bound 且全部不健康 → None（原 #259 语义保持）
   - bound 且健康 → JSON 限定名单
   - unbound / no_node → ""（全量广播，存量行为不变）
   - 场景查询异常 → None（fail-closed）
3. strict_scene_engines=False：bound 解析为空回退 "" 广播 + 告警日志
4. enqueue_task：pool_engine_ids="[]" scene 来源拒绝入队；auto 来源降级广播
5. scene_dispatch_failure_reason：引擎注销 / 查询失败的新原因文案
"""
import datetime
import json

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
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' "
                          f"OR repo_key LIKE 'gitcode:653/%' OR repo_key LIKE '653/%' OR repo_key LIKE 'owner/repo' "
                          f"OR repo_key LIKE 'gitcode:owner/repo' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:o/r'")
            except Exception:
                pass
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "events", "devops_agent_session", "agent_runtimes",
                  "runtime_credentials", "devops_pipeline_scene", "unified_user_mapping",
                  "user_repos", "engine_capabilities", "task_messages"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' "
                          f"OR repo_key LIKE 'gitcode:653/%' OR repo_key LIKE '653/%' OR repo_key LIKE 'owner/repo' "
                          f"OR repo_key LIKE 'gitcode:owner/repo' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:o/r'")
            except Exception:
                pass
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


def _stale_ts(seconds_ago):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=seconds_ago)).isoformat()


def _mk_scene(scene_id, agent_type, engine_ids, user_id=1, repo_key="owner/repo",
              omit_engine_key=False):
    with db_lock:
        c = db.cursor()
        try:
            c.execute("INSERT OR IGNORE INTO user_repos (user_id, repo_key, owner, name, is_join) "
                      "VALUES (?, 'gitcode:owner/repo', 'owner', 'repo', 1)", (user_id,))
        except Exception:
            pass
        db.commit()
    node = {"id": agent_type, "tag": agent_type, "skill": "test-skill",
            "mode": "emit", "next": "done", "handoff": "complete"}
    if not omit_engine_key:
        node["engine_runtime_ids"] = engine_ids
    from src.api.pipeline_config import create_custom_scene
    _rk = repo_key.split(":")[-1] if ":" in repo_key else repo_key
    create_custom_scene(scene_id=scene_id, name=scene_id, description="",
                        repo_key=_rk, nodes=[node])


def _strict_off(monkeypatch):
    """关闭严格模式（engine-selection.json strict_scene_engines=False 的等价注入）。"""
    _cfg = dict(es._DEFAULT_CFG)
    _cfg["strict_scene_engines"] = False
    monkeypatch.setattr(es, "load_engine_selection", lambda: _cfg)


class TestSceneEngineBinding:
    """Issue #653：get_scene_engine_binding 空值语义四态区分。"""

    def test_bound(self):
        eng = _mk_engine("opencode-653a", credential_id=_mk_cred("c-653a"))
        _mk_scene("s653-bound", "vod", [eng])
        from src.api.pipeline_config import get_scene_engine_binding
        b = get_scene_engine_binding("s653-bound", "vod", repo_key="owner/repo", user_id=1)
        assert b["status"] == "bound"
        assert b["engine_ids"] == [eng]

    def test_unbound(self):
        _mk_scene("s653-unbound", "vod", [])
        from src.api.pipeline_config import get_scene_engine_binding
        b = get_scene_engine_binding("s653-unbound", "vod", repo_key="owner/repo", user_id=1)
        assert b["status"] == "unbound"
        assert b["engine_ids"] == []

    def test_unbound_key_missing(self):
        _mk_scene("s653-nok", "vod", [], omit_engine_key=True)
        from src.api.pipeline_config import get_scene_engine_binding
        b = get_scene_engine_binding("s653-nok", "vod", repo_key="owner/repo", user_id=1)
        assert b["status"] == "unbound"

    def test_no_node(self):
        eng = _mk_engine("opencode-653b", credential_id=_mk_cred("c-653b"))
        _mk_scene("s653-nonode", "vod", [eng])
        from src.api.pipeline_config import get_scene_engine_binding
        b = get_scene_engine_binding("s653-nonode", "qa", repo_key="owner/repo", user_id=1)
        assert b["status"] == "no_node"
        assert b["engine_ids"] == []

    def test_no_scene(self):
        from src.api.pipeline_config import get_scene_engine_binding
        b = get_scene_engine_binding("s653-nonexistent", "vod", repo_key="owner/repo", user_id=1)
        assert b["status"] == "no_scene"
        assert b["engine_ids"] == []

    def test_get_scene_engine_ids_wrapper_compat(self):
        eng = _mk_engine("opencode-653c", credential_id=_mk_cred("c-653c"))
        _mk_scene("s653-wrap", "vod", [eng])
        from src.api.pipeline_config import get_scene_engine_ids
        assert get_scene_engine_ids("s653-wrap", "vod", repo_key="owner/repo", user_id=1) == [eng]
        assert get_scene_engine_ids("s653-wrap", "qa", repo_key="owner/repo", user_id=1) == []


class TestPoolStrictSemantics:
    """Issue #653 核心：bound 空候选绝不静默回退全局池（默认严格）。"""

    def test_bound_healthy_pool(self):
        eng = _mk_engine("opencode-653h", credential_id=_mk_cred("c-653h"))
        _mk_scene("s653-pool", "vod", [eng])
        result = es.get_scene_pool_engine_ids("s653-pool", "vod", repo_key="owner/repo", user_id=1)
        assert result is not None and json.loads(result) == [eng]

    def test_bound_engines_missing_returns_none(self):
        """配置的引擎在 agent_runtimes 已注销 → None（显式报错，不广播）。"""
        _mk_scene("s653-missing", "vod", [987654])
        assert es.get_scene_pool_engine_ids("s653-missing", "vod", repo_key="owner/repo", user_id=1) is None

    def test_bound_all_offline_returns_none(self):
        dead = _mk_engine("opencode-653d", credential_id=_mk_cred("c-653d"), status="offline")
        _mk_scene("s653-offline", "vod", [dead])
        assert es.get_scene_pool_engine_ids("s653-offline", "vod", repo_key="owner/repo", user_id=1) is None

    def test_unbound_broadcast_unchanged(self):
        _mk_scene("s653-unbound2", "vod", [])
        assert es.get_scene_pool_engine_ids("s653-unbound2", "vod", repo_key="owner/repo", user_id=1) == ""

    def test_no_node_broadcast_unchanged(self):
        eng = _mk_engine("opencode-653e", credential_id=_mk_cred("c-653e"))
        _mk_scene("s653-nonode2", "vod", [eng])
        assert es.get_scene_pool_engine_ids("s653-nonode2", "qa", repo_key="owner/repo", user_id=1) == ""

    def test_no_scene_broadcast_unchanged(self):
        assert es.get_scene_pool_engine_ids("s653-nonexistent", "vod", repo_key="owner/repo", user_id=1) == ""

    def test_query_exception_strict_returns_none(self, monkeypatch):
        """场景查询异常：严格模式 fail-closed → None（宁失败不跑飞）。"""
        import src.api.pipeline_config as pc
        _boom = RuntimeError("db exploded")
        monkeypatch.setattr(pc, "get_scene_engine_binding",
                            lambda *a, **k: (_ for _ in ()).throw(_boom))
        assert es.get_scene_pool_engine_ids("s653-err", "vod", repo_key="owner/repo", user_id=1) is None

    def test_query_exception_non_strict_broadcast(self, monkeypatch):
        import src.api.pipeline_config as pc
        _strict_off(monkeypatch)
        _boom = RuntimeError("db exploded")
        monkeypatch.setattr(pc, "get_scene_engine_binding",
                            lambda *a, **k: (_ for _ in ()).throw(_boom))
        assert es.get_scene_pool_engine_ids("s653-err2", "vod", repo_key="owner/repo", user_id=1) == ""


class TestStrictModeSwitch:
    """strict_scene_engines=False：bound 空候选回退广播 + 告警日志（可审计）。"""

    def test_bound_missing_non_strict_broadcast_with_warning(self, monkeypatch, caplog):
        _strict_off(monkeypatch)
        _mk_scene("s653-relax", "vod", [987655])
        with caplog.at_level("WARNING"):
            result = es.get_scene_pool_engine_ids("s653-relax", "vod", repo_key="owner/repo", user_id=1)
        assert result == ""
        assert any("strict_scene_engines=off" in r.message for r in caplog.records)

    def test_bound_offline_non_strict_still_none(self, monkeypatch):
        """关闭严格模式不影响「引擎存在但全部不健康」——#259 方案A 语义保持。"""
        _strict_off(monkeypatch)
        dead = _mk_engine("opencode-653f", credential_id=_mk_cred("c-653f"), status="offline")
        _mk_scene("s653-relax2", "vod", [dead])
        assert es.get_scene_pool_engine_ids("s653-relax2", "vod", repo_key="owner/repo", user_id=1) is None

    def test_strict_default_on(self):
        """默认配置 strict_scene_engines=True（存量部署未写该键也为严格）。"""
        assert es._DEFAULT_CFG["strict_scene_engines"] is True
        assert es.load_engine_selection().get("strict_scene_engines") is True


class TestEnqueueEmptyPoolGuard:
    """Issue #653：入队前置空候选校验（防 "[]" 绕过 claim 守卫卡死任务）。"""

    def test_scene_source_empty_pool_rejected(self):
        with pytest.raises(ValueError, match="empty engine list"):
            svc.enqueue_task(agent_session_id="sess-653", runtime_id=0, pool_agent_type="vod",
                             prompt="p", pool_engine_ids="[]", pool_engine_source="scene")

    def test_unknown_source_empty_pool_rejected(self):
        with pytest.raises(ValueError, match="empty engine list"):
            svc.enqueue_task(agent_session_id="sess-653", runtime_id=0, pool_agent_type="vod",
                             prompt="p", pool_engine_ids="[]")

    def test_auto_source_empty_pool_degrades_to_broadcast(self):
        """auto 来源空名单 → 降级 ""（全量广播），任务正常入队。"""
        tid = svc.enqueue_task(agent_session_id="sess-653", runtime_id=0, pool_agent_type="vod",
                                prompt="p", pool_engine_ids="[]", pool_engine_source="auto")
        assert tid
        with db_lock:
            c = db.cursor()
            c.execute("SELECT pool_engine_ids FROM agent_tasks WHERE id=?", (tid,))
            assert c.fetchone()[0] == ""

    def test_non_json_pool_unchanged(self):
        """非 JSON 值保持原语义（历史调用方兼容）。"""
        tid = svc.enqueue_task(agent_session_id="sess-653", runtime_id=0, pool_agent_type="vod",
                                prompt="p", pool_engine_ids="legacy-format")
        with db_lock:
            c = db.cursor()
            c.execute("SELECT pool_engine_ids FROM agent_tasks WHERE id=?", (tid,))
            assert c.fetchone()[0] == "legacy-format"


class TestDispatchFailureReason:
    """Issue #653：报错文案区分「引擎注销 / 查询失败」。"""

    def test_bound_engines_missing_reason(self):
        _mk_scene("s653-r1", "vod", [987656])
        reason = es.scene_dispatch_failure_reason("s653-r1", "vod", repo_key="owner/repo", user_id=1)
        assert "已注销或不存在" in reason
        assert "987656" in reason

    def test_all_offline_reason_contains_engine_list(self):
        dead = _mk_engine("opencode-653g", credential_id=_mk_cred("c-653g"), status="offline")
        _mk_scene("s653-r2", "vod", [dead])
        reason = es.scene_dispatch_failure_reason("s653-r2", "vod", repo_key="owner/repo", user_id=1)
        assert "全部离线" in reason
        assert str(dead) in reason

    def test_query_failure_reason(self, monkeypatch):
        import src.api.pipeline_config as pc
        _mk_scene("s653-r3", "vod", [987657])  # 场景仓内存在（过 #541 存在性检查）
        _boom = RuntimeError("db exploded")
        monkeypatch.setattr(pc, "get_scene_engine_binding",
                            lambda *a, **k: (_ for _ in ()).throw(_boom))
        reason = es.scene_dispatch_failure_reason("s653-r3", "vod", repo_key="owner/repo", user_id=1)
        assert "查询失败" in reason

    def test_unbound_reason(self):
        _mk_scene("s653-r4", "vod", [])
        reason = es.scene_dispatch_failure_reason("s653-r4", "vod", repo_key="owner/repo", user_id=1)
        assert "未绑定引擎" in reason
