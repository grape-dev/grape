"""Issue #605：engine_select 派发决策可观测性测试。

覆盖：
- get_scene_pool_engine_ids 过滤留痕（dropped 引擎 + 原因进结构化日志）
- pick_engine 决策日志（候选全集 + 每候选过滤原因 + 选中理由）
- explain_engine_selection 取证口径（candidates[].reasons / selected / pool）
- claim_by_tag 池名单不匹配跳过日志（Issue #605 取证关键缺口）
"""
import datetime
import json
import logging
import time as _time

import pytest

from src.session_manager import db, db_lock
from src.runtime import engine_select as es
from src.runtime import service


@pytest.fixture(autouse=True)
def _clean_tables(monkeypatch):
    # pick_engine/explain 默认 plugin 模式会短路返回 0；legacy 断言需强制 legacy
    monkeypatch.setenv("GRAPE_PIPELINE_MODE", "legacy")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM agent_runtimes")
        c.execute("DELETE FROM runtime_credentials")
        c.execute("DELETE FROM devops_pipeline_scene")
        c.execute("DELETE FROM user_repos")
        c.execute("DELETE FROM runtime_health_scores")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM agent_runtimes")
        c.execute("DELETE FROM runtime_credentials")
        c.execute("DELETE FROM devops_pipeline_scene")
        c.execute("DELETE FROM user_repos")
        c.execute("DELETE FROM runtime_health_scores")
        db.commit()
    # 清 claim 跳过日志去重缓存，避免跨用例污染
    service._pool_skip_log_cache.clear()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _stale_ts(seconds_ago):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=seconds_ago)).isoformat()


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


def _mk_scene(scene_id, agent_type, engine_ids, repo_key="owner/repo", user_id=1):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR IGNORE INTO user_repos (user_id, repo_key, owner, name, is_join) VALUES (?, ?, 'owner', 'repo', 1)",
            (user_id, f"gitcode:{repo_key}"))
        db.commit()
    nodes = [{"id": agent_type, "tag": agent_type, "skill": "test-skill",
              "mode": "emit", "next": "done", "handoff": "complete",
              "engine_runtime_ids": engine_ids}]
    from src.api.pipeline_config import create_custom_scene
    create_custom_scene(scene_id=scene_id, name=scene_id, description="",
                        repo_key=repo_key, nodes=nodes)


def _decision_records(caplog):
    """收集 engine_select_decision 结构化日志记录。"""
    return [r.getMessage() for r in caplog.records
            if "engine_select_decision" in r.getMessage()]


class TestPoolEngineIdsDecisionLog:
    def test_stale_heartbeat_engine_dropped_with_reason(self, caplog):
        """Issue #605 场景：hermes 心跳过期 → 被过滤出广播名单且原因落日志。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        cid = _mk_cred("c-605")
        # Issue #658 角色硬过滤后：健康引擎须与 vod 角色匹配（hermes），
        # 心跳过期的换成不限角色的 codex（未知类型，零配置兼容）
        oc = _mk_engine("hermes", credential_id=cid)
        hermes = _mk_engine("codex", credential_id=cid,
                            last_seen=_stale_ts(10000))  # 远古心跳（超 _RUNTIME_STALE_SECONDS）
        _mk_scene("test-605-pool", "vod", [oc, hermes])

        pool = es.get_scene_pool_engine_ids("test-605-pool", "vod", repo_key="owner/repo", user_id=1)
        ids = json.loads(pool)
        assert hermes not in ids
        assert oc in ids

        recs = _decision_records(caplog)
        pool_recs = [r for r in recs if '"pool_engine_ids"' in r]
        assert pool_recs, f"no pool decision log, got: {recs}"
        payload = json.loads(pool_recs[-1].split("engine_select_decision ", 1)[1])
        assert payload["result"] == "healthy_pool"
        assert payload["dropped"], "dropped 列表不能为空（hermes 应被记录原因）"
        dropped_hermes = [d for d in payload["dropped"] if d["rid"] == hermes]
        assert dropped_hermes and "stale_heartbeat" in dropped_hermes[0]["reason"]

    def test_all_offline_logs_none_result(self, caplog):
        caplog.set_level(logging.INFO, logger="devops-bot")
        cid = _mk_cred("c-605b")
        hermes = _mk_engine("hermes", credential_id=cid, status="offline")
        _mk_scene("test-605-none", "vod", [hermes])
        assert es.get_scene_pool_engine_ids("test-605-none", "vod", repo_key="owner/repo", user_id=1) is None
        pool_recs = [r for r in _decision_records(caplog) if '"pool_engine_ids"' in r]
        assert pool_recs
        payload = json.loads(pool_recs[-1].split("engine_select_decision ", 1)[1])
        assert payload["result"] == "all_offline"
        assert payload["dropped"][0]["reason"].startswith("status=")


class TestPickEngineDecisionLog:
    def test_selected_logs_candidates_and_reason(self, caplog):
        """健康引擎被选中 → 日志含候选全集、选中理由（scope/rule）。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        cid = _mk_cred("c-605c")
        # Issue #658：agent_type 用 coding（opencode 默认角色集含 coding；vod 已不可派 opencode）
        oc = _mk_engine("opencode", credential_id=cid)
        hermes = _mk_engine("hermes", credential_id=cid,
                            last_seen=_stale_ts(10000))
        _mk_scene("test-605-pick", "coding", [oc, hermes])

        picked = es.pick_engine("owner/repo", "coding", scene_id="test-605-pick")
        assert picked == oc

        recs = _decision_records(caplog)
        pick_recs = [r for r in recs if r.startswith("engine_select_decision {\"event\": \"pick_engine\"")
                     or '"event": "pick_engine"' in r]
        assert pick_recs
        payload = json.loads(pick_recs[-1].split("engine_select_decision ", 1)[1])
        assert payload["result"] == "selected"
        assert payload["selected"] == oc
        assert "scope=" in payload["reason"]
        # 候选全集必须含 hermes 且带过滤原因（否则 0 派发不可解释）
        cand = {c["rid"]: c for c in payload["candidates"]}
        assert hermes in cand
        assert cand[hermes]["reasons"], "hermes 被过滤但无原因 = 观测缺口回归"
        assert any("stale_heartbeat" in r for r in cand[hermes]["reasons"])
        assert cand[oc]["reasons"] == []

    def test_plugin_mode_logs_broadcast(self, caplog, monkeypatch):
        monkeypatch.setenv("GRAPE_PIPELINE_MODE", "plugin")
        caplog.set_level(logging.INFO, logger="devops-bot")
        es.pick_engine("owner/repo", "vod")
        recs = _decision_records(caplog)
        assert any('"result": "plugin_broadcast"' in r for r in recs)


class TestExplainEngineSelection:
    def test_explain_returns_forensic_view(self):
        """主控取证口径：candidates[].reasons / selected / pool 一应俱全。"""
        cid = _mk_cred("c-605d")
        # Issue #658：agent_type 用 coding（vod 已不可派 opencode/codearts）
        oc = _mk_engine("opencode", credential_id=cid)
        hermes = _mk_engine("hermes", credential_id=cid,
                            last_seen=_stale_ts(10000))
        _mk_scene("test-605-explain", "coding", [oc, hermes])

        out = es.explain_engine_selection("owner/repo", "coding", scene_id="test-605-explain")
        assert out["mode"] == "legacy"
        assert out["bound_engine_ids"] == [oc, hermes]
        cand = {c["runtime_id"]: c for c in out["candidates"]}
        assert cand[oc]["reasons"] == []
        assert any("stale_heartbeat" in r for r in cand[hermes]["reasons"])
        assert out["selected"] == oc
        assert hermes not in json.loads(out["pool_engine_ids"])

    def test_explain_empty_binding(self):
        out = es.explain_engine_selection("owner/repo", "vod", scene_id="")
        assert out["bound_engine_ids"] == []
        assert out["candidates"] == []
        assert out["selected"] is None


class TestClaimPoolSkipLog:
    def test_claim_by_tag_pool_mismatch_logged(self, caplog):
        """Issue #605 关键缺口：引擎不在 pool 名单 → 跳过必须留痕（否则 hermes 0 派发无从解释）。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        cid = _mk_cred("c-605e")
        oc = _mk_engine("opencode", credential_id=cid)
        hermes = _mk_engine("codex", credential_id=cid)  # Issue #658：名单外引擎用不限角色类型
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, agent_type, pool_tag, "
                "pool_agent_type, pool_engine_ids, status, created_at, repo_key, issue_session_id, prompt, cwd, skill) "
                "VALUES ('t-605', 's-605', 0, 'coding', 'coding', 'coding', ?, 'queued', ?, 'owner/repo', '', '', '', '')",
                (json.dumps([oc]), _now()))
            db.commit()

        task = service.claim_by_tag("coding", hermes)
        assert task is None, "hermes 不在广播名单 → 不应认领成功"
        msgs = [r.getMessage() for r in caplog.records]
        assert any("claim pool filter" in m and "t-605" in m and str(hermes) in m for m in msgs), \
            f"pool 名单跳过未留痕: {msgs[-5:]}"
        # 去重：重复 claim 轮询不刷屏
        caplog.clear()
        service.claim_by_tag("vod", hermes)
        again = [r.getMessage() for r in caplog.records if "claim pool filter" in r.getMessage()]
        assert again == []

        # opencode 在名单内 → 正常认领（回归：观测改动不影响派发语义）
        task = service.claim_by_tag("coding", oc)
        assert task and task["id"] == "t-605"
