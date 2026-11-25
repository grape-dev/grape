# -*- coding: utf-8 -*-
"""Issue #3508 回归：场景引擎健康双指标（健康分不足 / 物理不可达）与报错兜底。

覆盖（对应 Spec 验收）：
1. 物理不可达：离线引擎 → engines[].reachability=unreachable、reason 含「物理不可达」，
   节点计入 offline_nodes。
2. 健康分不足：引擎在线可达但健康分低于派发阈值 → reason 含「健康分不足」，
   节点计入 low_score_nodes（与 offline_nodes 分列）。
3. 接口报错兜底：get_pipeline_config 抛异常 → 2xx {degraded, error}，不再裸 500。
4. 绑定修改校验：非法/悬空 engine id 落库前被清洗（_sanitize_scene_engine_ids）；
   QA 节点提取对非 dict 脏节点不再 AttributeError（_scene_node_field）。
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

_REPO_KEY = "gitcode:i3508/repo1"


@pytest.fixture()
def client():
    from src.api.main import app
    from src.api.deps import get_current_user_or_api_key, require_no_session_token
    from src.kernel.session_manager import db, db_lock

    _mock_user = {"id": 1, "username": "i3508-user", "auth_type": "api_key"}
    app.dependency_overrides[get_current_user_or_api_key] = lambda: dict(_mock_user)
    app.dependency_overrides[require_no_session_token] = lambda: dict(_mock_user)

    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        # 两个测试引擎：一台离线（物理不可达）、一台在线低分（健康分不足）
        c.execute("DELETE FROM runtime_health_scores WHERE runtime_id IN "
                  "(SELECT id FROM agent_runtimes WHERE name LIKE 'i3508-rt-%')")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id IN "
                  "(SELECT id FROM agent_runtimes WHERE name LIKE 'i3508-rt-%')")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'i3508-rt-%'")
        c.execute("INSERT INTO agent_runtimes "
                  "(name, display_name, credential_id, status, healthy, installed, last_seen_at) "
                  "VALUES ('i3508-rt-offline', '离线引擎', 1, 'offline', 0, 1, ?)", (now,))
        c.execute("INSERT INTO agent_runtimes "
                  "(name, display_name, credential_id, status, healthy, installed, last_seen_at) "
                  "VALUES ('i3508-rt-lowscore', '低分引擎', 1, 'online', 1, 1, ?)", (now,))
        db.commit()
        c.execute("SELECT id, name FROM agent_runtimes WHERE name LIKE 'i3508-rt-%'")
        ids = {r[1]: r[0] for r in c.fetchall()}
        # 能力标签：两台测试引擎均登记 all 通配（写侧角色能力校验用）
        for _rid in ids.values():
            c.execute("INSERT OR REPLACE INTO engine_capabilities "
                      "(runtime_id, tag, enabled) VALUES (?, 'all', 1)", (_rid,))
        # 健康分：低分引擎 10 分（阈值默认 30）
        c.execute("INSERT OR REPLACE INTO runtime_health_scores "
                  "(runtime_id, health_score, total_tasks, failed_tasks, completed_tasks, "
                  "consecutive_failures, failure_rate, last_status, window_days, updated_at) "
                  "VALUES (?, 10, 50, 40, 10, 0, 0.8, '', 7, ?)",
                  (ids["i3508-rt-lowscore"], now))
        # 离线引擎给一个满分档（健康分高但物理不可达——两类异常语义必须分列）
        c.execute("INSERT OR REPLACE INTO runtime_health_scores "
                  "(runtime_id, health_score, total_tasks, failed_tasks, completed_tasks, "
                  "consecutive_failures, failure_rate, last_status, window_days, updated_at) "
                  "VALUES (?, 100, 50, 0, 50, 0, 0.0, '', 7, ?)",
                  (ids["i3508-rt-offline"], now))
        db.commit()

    yield {"ids": ids}

    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_health_scores WHERE runtime_id IN "
                  "(SELECT id FROM agent_runtimes WHERE name LIKE 'i3508-rt-%')")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id IN "
                  "(SELECT id FROM agent_runtimes WHERE name LIKE 'i3508-rt-%')")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'i3508-rt-%'")
        db.commit()
    app.dependency_overrides.clear()


def _scene_status(scene_id, nodes, repo_key=_REPO_KEY):
    from src.domains.mgmt.routes.scenes import _scene_engine_status
    return _scene_engine_status(scene_id, nodes, repo_key=repo_key)


class TestPhysicallyUnreachable:
    """回归①：物理不可达（健康分达标但引擎不可达，对应线上事件，序号已脱敏）。"""

    def test_offline_engine_unreachable_with_reason(self, client):
        rid = client["ids"]["i3508-rt-offline"]
        nodes = [{"id": "sre", "tag": "sre", "enabled": True, "engine_runtime_ids": [rid]}]
        with patch("src.domains.runtime.engine.engine_select.get_scene_pool_engine_ids",
                   return_value=None):
            status = _scene_status("i3508-s1", nodes)
        assert status["offline_nodes"] == ["sre"]
        assert status["all_offline"] is True
        entry = next(e for e in status["engines"] if e["id"] == rid)
        assert entry["reachability"] == "unreachable"
        assert "物理不可达" in entry["reason"]
        # 健康分字段恒在（结构化返回 {health_score, reachability, reason}）
        assert entry["health_score"] == 100
        # 全离线节点不误入「健康分不足」口径（双指标分列）
        assert status["low_score_nodes"] == []


class TestHealthScoreLow:
    """回归②：健康分不足（引擎在线可达但健康分低于派发阈值，对应线上事件，序号已脱敏）。"""

    def test_reachable_low_score_node_classified(self, client):
        rid = client["ids"]["i3508-rt-lowscore"]
        nodes = [{"id": "sre", "tag": "sre", "enabled": True, "engine_runtime_ids": [rid]}]
        with patch("src.domains.runtime.engine.engine_select.get_scene_pool_engine_ids",
                   return_value=[rid]):
            status = _scene_status("i3508-s2", nodes)
        entry = next(e for e in status["engines"] if e["id"] == rid)
        assert entry["reachability"] == "reachable"
        assert entry["health_score"] == 10
        assert "健康分不足" in entry["reason"]
        # 双指标分列：健康分不足计入 low_score_nodes，不计入 offline_nodes
        assert status["low_score_nodes"] == ["sre"]
        assert status["offline_nodes"] == []


class TestDegradedNot500:
    """回归③：配置读取异常 → 2xx {degraded, error}，不再裸 500（接口错误兜底）。"""

    def test_route_degrades_on_config_error(self, client):
        from fastapi.testclient import TestClient
        from src.api.main import app

        with patch("src.api.pipeline_config.get_pipeline_config",
                   side_effect=RuntimeError("db down")):
            with TestClient(app) as tc:
                r = tc.get("/api/scenes/i3508-s1/engine-status?repo=i3508/repo1")
        assert r.status_code == 200
        body = r.json()
        assert body.get("degraded") is True
        assert body.get("error")
        assert body.get("engines") == []


class TestSanitizeAndNodeField:
    """回归④：绑定修改候选范围清洗 + QA 节点提取不再对脏节点 500。"""

    def test_sanitize_drops_invalid_and_missing_ids(self, client):
        from src.domains.mgmt.routes.scenes import _sanitize_scene_engine_ids
        good = client["ids"]["i3508-rt-lowscore"]
        nodes = [
            {"id": "sre", "tag": "sre", "enabled": True,
             "engine_runtime_ids": ["abc", 999999, good]},
            "legacy-dirty-node",
        ]
        out = _sanitize_scene_engine_ids(nodes)
        assert out[0]["engine_runtime_ids"] == [good]
        # 非 dict 节点原样保留（历史数据兼容）
        assert out[1] == "legacy-dirty-node"

    def test_scene_node_field_safe_for_dirty_nodes(self, client):
        from src.domains.mgmt.routes.scenes import _scene_node_field
        assert _scene_node_field(None, "tag", "") == ""
        assert _scene_node_field("legacy", "tag", "") == ""
        assert _scene_node_field({"tag": "qa"}, "tag", "") == "qa"

        class _N:
            tag = "qa"

        assert _scene_node_field(_N(), "tag", "") == "qa"


class TestUpdateSceneBindingSanitize:
    """回归⑤：绑定修改——悬空引擎 ID 清洗 + SRE 清洗后为空显式 400（保存生效语义）。"""

    def test_update_scene_drops_dangling_engine_ids(self, client):
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.kernel.session_manager import db, db_lock

        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id LIKE 'i3508-%'")
            c.execute(
                "INSERT OR REPLACE INTO devops_pipeline_scene "
                "(scene_id, name, repo_key, nodes_json, is_active, pr_subscription_enabled) "
                "VALUES ('i3508-s3', 'I3508 S3', ?, '[]', 1, 0)", (_REPO_KEY,))
            db.commit()
        try:
            with TestClient(app) as tc:
                # 非 SRE 节点全部悬空：显式 400（不允许静默清空为空绑定=广播）
                r = tc.put("/api/scenes/i3508-s3?repo=i3508/repo1", json={
                    "nodes": [{"id": "coding", "tag": "coding", "skill": "",
                               "engine_runtime_ids": [999999, "abc"]}],
                })
                assert r.status_code == 400, r.text
                assert "不存在" in (r.json().get("detail") or {}).get("message", "")
                # SRE 节点全部悬空：同样显式 400 让用户重选
                r2 = tc.put("/api/scenes/i3508-s3?repo=i3508/repo1", json={
                    "nodes": [{"id": "sre", "tag": "sre", "skill": "",
                               "engine_runtime_ids": [999999]}],
                })
                assert r2.status_code == 400
                assert "不存在" in (r2.json().get("detail") or {}).get("message", "")
                # 部分悬空 + 部分有效：保存成功，落库仅保留有效 ID（真实生效）
                good = client["ids"]["i3508-rt-lowscore"]
                r3 = tc.put("/api/scenes/i3508-s3?repo=i3508/repo1", json={
                    "nodes": [{"id": "coding", "tag": "coding", "skill": "",
                               "engine_runtime_ids": [999999, good]}],
                })
                assert r3.status_code == 200, r3.text
            with db_lock:
                c = db.cursor()
                c.execute("SELECT nodes_json FROM devops_pipeline_scene "
                          "WHERE scene_id='i3508-s3' AND repo_key=?", (_REPO_KEY,))
                import json as _json
                nodes = _json.loads(c.fetchone()[0])
                coding = next(n for n in nodes if (n.get("id") or n.get("tag")) == "coding")
                assert coding["engine_runtime_ids"] == [good]
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id LIKE 'i3508-%'")
                db.commit()

class TestMultiEngineClassification:
    """回归⑥：多引擎节点双指标分类完整 + reason 采用结构化 health_reason。"""

    def test_mixed_reachable_low_and_unreachable_classified_low_score(self, client):
        offline = client["ids"]["i3508-rt-offline"]
        low = client["ids"]["i3508-rt-lowscore"]
        nodes = [{"id": "qa", "tag": "qa", "enabled": True,
                  "engine_runtime_ids": [offline, low]}]
        status = _scene_status("i3508-mix", nodes)
        # 有可达低分引擎 → 归入健康分不足；不是全部物理不可达
        assert status["low_score_nodes"] == ["qa"]
        assert status["offline_nodes"] == []

    def test_one_low_many_healthy_node_classified_low_score(self, client):
        from src.kernel.session_manager import db, db_lock
        now = datetime.now(timezone.utc).isoformat()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO agent_runtimes "
                      "(name, display_name, credential_id, status, healthy, installed, last_seen_at) "
                      "VALUES ('i3508-rt-healthy', '健康引擎', 1, 'online', 1, 1, ?)", (now,))
            db.commit()
            c.execute("SELECT id FROM agent_runtimes WHERE name='i3508-rt-healthy'")
            healthy = c.fetchone()[0]
            c.execute("INSERT OR REPLACE INTO runtime_health_scores "
                      "(runtime_id, health_score, total_tasks, failed_tasks, completed_tasks, "
                      "consecutive_failures, failure_rate, last_status, window_days, updated_at) "
                      "VALUES (?, 100, 50, 0, 50, 0, 0.0, '', 7, ?)", (healthy, now))
            db.commit()
        low = client["ids"]["i3508-rt-lowscore"]
        nodes = [{"id": "qa", "tag": "qa", "enabled": True,
                  "engine_runtime_ids": [low, healthy]}]
        status = _scene_status("i3508-mix2", nodes)
        assert status["low_score_nodes"] == ["qa"]
        assert status["offline_nodes"] == []

    def test_reason_uses_structured_health_reason_not_score_guess(self, client):
        from src.domains.mgmt.routes.scenes import _engine_health_entry
        from src.kernel.session_manager import db, db_lock
        now = datetime.now(timezone.utc).isoformat()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO agent_runtimes "
                      "(name, display_name, credential_id, status, healthy, installed, last_seen_at) "
                      "VALUES ('i3508-rt-cf', '连败引擎', 1, 'online', 1, 1, ?)", (now,))
            db.commit()
            c.execute("SELECT id FROM agent_runtimes WHERE name='i3508-rt-cf'")
            rid = c.fetchone()[0]
            # 分数满分但连败超限 → health_reason=consecutive_failures（非低分）
            c.execute("INSERT OR REPLACE INTO runtime_health_scores "
                      "(runtime_id, health_score, total_tasks, failed_tasks, completed_tasks, "
                      "consecutive_failures, failure_rate, last_status, window_days, updated_at) "
                      "VALUES (?, 100, 50, 0, 50, 6, 0.0, '', 7, ?)", (rid, now))
            db.commit()
        entry = _engine_health_entry(rid, "qa")
        assert entry["health_abnormal"] is True
        assert "连续失败" in entry["reason"]
        assert "健康分不足" not in entry["reason"]

    def test_route_engine_status_multi_engine_low_score(self, client):
        from fastapi.testclient import TestClient
        from src.api.main import app
        offline = client["ids"]["i3508-rt-offline"]
        low = client["ids"]["i3508-rt-lowscore"]
        cfg = {"scene_id": "i3508-route-mix", "nodes": [
            {"id": "qa", "tag": "qa", "enabled": True,
             "engine_runtime_ids": [offline, low]}]}
        with patch("src.api.pipeline_config.get_pipeline_config", return_value=cfg):
            with TestClient(app) as tc:
                r = tc.get("/api/scenes/i3508-route-mix/engine-status?repo=i3508/repo1")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["low_score_nodes"] == ["qa"]
        assert body["offline_nodes"] == []
        assert any(e.get("health_abnormal") for e in body["engines"])


class TestUidBindingPreserved:
    """回归⑦：engine_runtime_ids 内的合法 36 位 UID 不被清洗丢弃（#646 兼容）。"""

    def test_update_scene_preserves_uid_string_binding(self, client):
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.kernel.session_manager import db, db_lock

        good = client["ids"]["i3508-rt-lowscore"]
        uid = str(uuid.uuid4())
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id LIKE 'i3508-%'")
            c.execute(
                "INSERT OR REPLACE INTO devops_pipeline_scene "
                "(scene_id, name, repo_key, nodes_json, is_active, pr_subscription_enabled) "
                "VALUES ('i3508-s4', 'I3508 S4', ?, '[]', 1, 0)", (_REPO_KEY,))
            db.commit()
        try:
            # uid→id 解析成功（模拟 #646 UID 绑定）；升格 uid 回读不可用 → 保留 ids 落库
            with patch("src.domains.mgmt.user.db.get_runtime_id_by_engine_uid",
                       return_value=good), \
                 patch("src.domains.mgmt.user.db.get_engine_uid_by_runtime_id",
                       return_value=None):
                with TestClient(app) as tc:
                    r = tc.put("/api/scenes/i3508-s4?repo=i3508/repo1", json={
                        "nodes": [{"id": "coding", "tag": "coding", "skill": "",
                                   "engine_runtime_ids": [uid]}],
                    })
            assert r.status_code == 200, r.text
            with db_lock:
                c = db.cursor()
                c.execute("SELECT nodes_json FROM devops_pipeline_scene "
                          "WHERE scene_id='i3508-s4' AND repo_key=?", (_REPO_KEY,))
                import json as _json
                node = next(n for n in _json.loads(c.fetchone()[0])
                            if (n.get("id") or n.get("tag")) == "coding")
            assert node["engine_runtime_ids"] == [good]
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id LIKE 'i3508-%'")
                db.commit()


class TestHealthQueryFailure:
    """回归⑧：健康行查询异常 → unknown 降级，不误报物理不可达 / 不计入 offline_nodes。"""

    def test_query_failure_not_counted_as_offline(self, client):
        rid = client["ids"]["i3508-rt-lowscore"]
        nodes = [{"id": "sre", "tag": "sre", "enabled": True, "engine_runtime_ids": [rid]}]
        with patch("src.domains.runtime.public.runtime_health_row",
                   side_effect=RuntimeError("runtime db down")):
            status = _scene_status("i3508-qf", nodes)
        assert status["offline_nodes"] == []
        assert status["all_offline"] is False
        assert status["low_score_nodes"] == []
        entry = next(e for e in status["engines"] if e["id"] == rid)
        assert entry["reachability"] == "unknown"
        assert entry["query_failed"] is True
        assert "暂不可用" in entry["reason"]

    def test_health_read_is_side_effect_free(self, client):
        """GET 查询路径不现算落表：无健康档时 health_score=None 且不写 runtime_health_scores。"""
        from src.domains.mgmt.routes.scenes import _engine_health_entry
        from src.kernel.session_manager import db, db_lock

        rid = client["ids"]["i3508-rt-lowscore"]
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM runtime_health_scores WHERE runtime_id=?", (rid,))
            db.commit()
        entry = _engine_health_entry(rid, "qa")
        assert entry["health_score"] is None
        assert entry["reachability"] == "reachable"
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM runtime_health_scores WHERE runtime_id=?", (rid,))
            assert c.fetchone()[0] == 0

