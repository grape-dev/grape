"""Issue #134: 机器健康度计算与派发过滤测试。

覆盖：
- health_scorer 健康分计算（失败率、连续失败惩罚、边界值）
- 派发链路健康度过滤（filter_healthy / best_fallback / filter_or_fallback）
- 广播机制无回归验证（plugin 模式返回 0，健康度过滤不干扰广播）
- resource_stack 健康度查询接口
- DB 持久化 + 内存缓存
"""
import os
import time
import json
import pytest
from unittest.mock import patch, MagicMock

from src.session_manager import db, db_lock
from src.runtime import health_scorer
from src.runtime.engine_select import pick_engine, _filter_by_health, _best_health_fallback


# ===== 健康分计算测试 =====

class TestHealthScoreComputation:
    """测试 _compute_health_score 的计算逻辑。"""

    def test_no_tasks_returns_full_score(self):
        """无历史数据 → 100 分（默认健康）。"""
        assert health_scorer._compute_health_score(0, 0, 0) == 100

    def test_all_completed_returns_full_score(self):
        """全部完成 → 100 分。"""
        assert health_scorer._compute_health_score(10, 0, 0) == 100

    def test_all_failed_returns_zero(self):
        """全部失败 + 连续失败多 → 低分。"""
        score = health_scorer._compute_health_score(10, 10, 10)
        assert score == 0  # 0% 成功率 + 连续失败惩罚 → 0

    def test_half_failure(self):
        """50% 失败率 → 约 50 分（减去连续失败惩罚）。"""
        score = health_scorer._compute_health_score(10, 5, 0)
        assert score == 50  # 100*(1-0.5) - 0 = 50

    def test_consecutive_failure_penalty(self):
        """连续失败惩罚：每次 -5，上限 20。"""
        # 1 次失败：50 - 5 = 45
        assert health_scorer._compute_health_score(10, 5, 1) == 45
        # 4 次连续失败：50 - 20 = 30
        assert health_scorer._compute_health_score(10, 5, 4) == 30
        # 5 次连续失败：50 - 20 = 30（上限 20）
        assert health_scorer._compute_health_score(10, 5, 5) == 30

    def test_score_clamped_to_0_100(self):
        """健康分 clamp 到 [0, 100]。"""
        assert health_scorer._compute_health_score(10, 10, 10) >= 0
        assert health_scorer._compute_health_score(1, 0, 0) <= 100

    def test_low_failure_rate_high_score(self):
        """低失败率 → 高分。"""
        score = health_scorer._compute_health_score(100, 5, 0)
        assert score == 95  # 100*(1-0.05) = 95


# ===== 过滤逻辑测试 =====

class TestHealthFiltering:
    """测试 filter_healthy / best_fallback / filter_or_fallback。"""

    def test_filter_healthy_with_mock(self):
        """filter_healthy 过滤低健康分引擎。"""
        with patch.object(health_scorer, 'get_health_score') as mock_get:
            def _mock_score(rid, force_refresh=False):
                if rid == 1:
                    return {"health_score": 90, "consecutive_failures": 0}
                elif rid == 2:
                    return {"health_score": 10, "consecutive_failures": 0}
                elif rid == 3:
                    return {"health_score": 50, "consecutive_failures": 0}
                return {"health_score": 100, "consecutive_failures": 0}
            mock_get.side_effect = _mock_score
            result = health_scorer.filter_healthy([1, 2, 3])
            assert 1 in result  # score=90 >= 30 → healthy
            assert 2 not in result  # score=10 < 30 → unhealthy
            assert 3 in result  # score=50 >= 30 → healthy

    def test_filter_healthy_consecutive_failures(self):
        """连续失败超限 → 不健康。"""
        with patch.object(health_scorer, 'get_health_score') as mock_get:
            mock_get.return_value = {"health_score": 90, "consecutive_failures": 10}
            result = health_scorer.filter_healthy([1])
            assert 1 not in result  # consecutive_failures=10 >= 5 → unhealthy

    def test_best_fallback_returns_highest_score(self):
        """best_fallback 返回健康分最高的引擎。"""
        with patch.object(health_scorer, 'get_health_scores') as mock_scores:
            mock_scores.return_value = {
                1: {"health_score": 20},
                2: {"health_score": 50},
                3: {"health_score": 10},
            }
            result = health_scorer.best_fallback([1, 2, 3])
            assert result == 2  # 最高分

    def test_filter_or_fallback_all_unhealthy_returns_best(self):
        """filter_or_fallback 全部不健康时返回最优。"""
        with patch.object(health_scorer, 'is_healthy', return_value=False), \
             patch.object(health_scorer, 'best_fallback', return_value=2):
            result = health_scorer.filter_or_fallback([1, 2, 3])
            assert result == [2]  # 降级兜底

    def test_filter_or_fallback_some_healthy(self):
        """filter_or_fallback 有健康引擎时只返回健康的。"""
        with patch.object(health_scorer, 'is_healthy') as mock_healthy:
            mock_healthy.side_effect = lambda rid, threshold=None, force_refresh=False: rid == 1
            result = health_scorer.filter_or_fallback([1, 2, 3])
            assert result == [1]

    def test_filter_or_fallback_empty_input(self):
        """空输入 → 空输出。"""
        assert health_scorer.filter_or_fallback([]) == []

    def test_runtime_id_zero_not_filtered(self):
        """runtime_id=0（广播/未绑定）不参与健康过滤。"""
        assert health_scorer.is_healthy(0) is True

    def test_filter_healthy_empty_input(self):
        """空列表 → 空列表。"""
        assert health_scorer.filter_healthy([]) == []


# ===== 缓存测试 =====

class TestHealthCache:
    """测试内存缓存 TTL。"""

    def test_cache_invalidation(self):
        """invalidate_cache 清除缓存。"""
        health_scorer._cache[999] = {"score": 50, "data": {}, "expires": time.time() + 100}
        health_scorer.invalidate_cache(999)
        assert 999 not in health_scorer._cache

    def test_cache_invalidation_all(self):
        """invalidate_cache(0) 清空所有缓存。"""
        health_scorer._cache[1] = {"score": 50, "data": {}, "expires": time.time() + 100}
        health_scorer._cache[2] = {"score": 60, "data": {}, "expires": time.time() + 100}
        health_scorer.invalidate_cache(0)
        assert len(health_scorer._cache) == 0


# ===== DB 持久化测试 =====

class TestHealthPersistence:
    """测试 DB 持久化（runtime_health_scores 表）。"""

    def test_table_exists(self):
        """runtime_health_scores 表应存在。"""
        with db_lock:
            c = db.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='runtime_health_scores'")
            row = c.fetchone()
        assert row is not None

    def test_persist_and_read(self):
        """持久化健康分后可从 DB 读回。"""
        data = {
            "runtime_id": 99999,
            "health_score": 75,
            "total_tasks": 20,
            "failed_tasks": 5,
            "completed_tasks": 15,
            "consecutive_failures": 1,
            "failure_rate": 0.25,
            "last_status": "completed",
            "window_days": 7,
            "updated_at": "2026-09-13T12:00:00+00:00",
        }
        health_scorer._persist_score(data)
        # 从 DB 读回
        with db_lock:
            c = db.cursor()
            c.execute("SELECT health_score FROM runtime_health_scores WHERE runtime_id=?", (99999,))
            row = c.fetchone()
        assert row is not None
        assert row[0] == 75
        # 清理
        with db_lock:
            db.cursor().execute("DELETE FROM runtime_health_scores WHERE runtime_id=?", (99999,))
            db.commit()


# ===== engine_select 健康度过滤测试 =====

class TestEngineSelectHealthFilter:
    """测试 pick_engine 中的健康度过滤增强层。"""

    def test_filter_by_health_passes_healthy(self):
        """_filter_by_health 保留健康引擎。"""
        members = [{"runtime_id": 1}, {"runtime_id": 2}]
        with patch.object(health_scorer, 'filter_healthy', return_value=[1, 2]):
            result = _filter_by_health(members)
            assert len(result) == 2

    def test_filter_by_health_filters_unhealthy(self):
        """_filter_by_health 过滤不健康引擎。"""
        members = [{"runtime_id": 1}, {"runtime_id": 2}]
        with patch.object(health_scorer, 'filter_healthy', return_value=[1]):
            result = _filter_by_health(members)
            assert len(result) == 1
            assert result[0]["runtime_id"] == 1

    def test_best_health_fallback(self):
        """_best_health_fallback 返回最优引擎。"""
        members = [{"runtime_id": 1}, {"runtime_id": 2}]
        with patch.object(health_scorer, 'best_fallback', return_value=2):
            result = _best_health_fallback(members)
            assert result == 2

    def test_filter_by_health_exception_returns_original(self):
        """_filter_by_health 异常时返回原列表（不阻塞派发）。"""
        members = [{"runtime_id": 1}]
        with patch.object(health_scorer, 'filter_healthy', side_effect=RuntimeError("boom")):
            result = _filter_by_health(members)
            assert result == members  # 返回原列表


# ===== 广播机制无回归测试 =====

class TestBroadcastNoRegression:
    """验证健康度过滤不破坏广播机制（核心约束）。"""

    def test_plugin_mode_returns_zero(self):
        """plugin 模式下 pick_engine 返回 0（广播），健康度过滤不干扰。"""
        with patch("src.api.event_pipeline.is_plugin_mode", return_value=True):
            result = pick_engine("owner/repo", "coding")
        assert result == 0  # 广播模式返回 0

    def test_plugin_mode_not_affected_by_health(self):
        """plugin 模式下即使所有引擎不健康，仍返回 0（广播不被过滤）。"""
        with patch("src.api.event_pipeline.is_plugin_mode", return_value=True), \
             patch.object(health_scorer, 'is_healthy', return_value=False):
            result = pick_engine("owner/repo", "coding")
        assert result == 0  # 广播不受健康度过滤影响

    def test_pick_engine_all_unhealthy_fallback(self):
        """全部不健康时降级兜底取最优（不返回 None 导致无机器可用）。"""
        # 构造测试引擎
        with db_lock:
            c = db.cursor()
            # 清理旧测试数据
            c.execute("DELETE FROM agent_runtimes WHERE id IN (900001, 900002)")
            c.execute("DELETE FROM runtime_credentials WHERE id IN (900001)")
            c.execute("DELETE FROM engine_capabilities WHERE runtime_id IN (900001, 900002)")
            # 插入两个引擎
            c.execute(
                "INSERT INTO runtime_credentials (token_hash, name, scope, status) "
                "VALUES ('test134hash', 'test134-cred', 'global', 'active')")
            cred_id = c.lastrowid
            c.execute(
                "INSERT INTO agent_runtimes (id, name, credential_id, status, healthy, installed, "
                "scope, last_seen_at) VALUES (900001, 'opencode', ?, 'online', 1, 1, 'global', datetime('now'))",
                (cred_id,))
            c.execute(
                "INSERT INTO agent_runtimes (id, name, credential_id, status, healthy, installed, "
                "scope, last_seen_at) VALUES (900002, 'hermes', ?, 'online', 1, 1, 'global', datetime('now'))",
                (cred_id,))
            c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (900001, 'all', 1)")
            c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (900002, 'all', 1)")
            db.commit()

        try:
            # mock 健康度：900001 不健康（低分），900002 更健康
            with patch("src.api.event_pipeline.is_plugin_mode", return_value=False), \
                 patch.object(health_scorer, 'is_healthy', return_value=False), \
                 patch.object(health_scorer, 'best_fallback', return_value=900002), \
                 patch.object(health_scorer, 'get_health_score') as mock_score:
                mock_score.return_value = {"health_score": 50, "consecutive_failures": 0}
                # 不走场景，直接传 engine_ids 让 pick_engine 走多引擎选择路径
                with patch("src.api.pipeline_config.get_scene_engine_ids",
                           return_value=[900001, 900002]):
                    result = pick_engine("test/repo", "coding", scene_id="test-scene")
            # 全部不健康时降级兜底，应返回 900002（best_fallback mock）
            assert result == 900002
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM agent_runtimes WHERE id IN (900001, 900002)")
                c.execute("DELETE FROM runtime_credentials WHERE id = ?", (cred_id,))
                c.execute("DELETE FROM engine_capabilities WHERE runtime_id IN (900001, 900002)")
                db.commit()


# ===== resource_stack 健康度接口测试 =====

class TestResourceStackHealth:
    """测试 TaskResourceStack 的健康度查询接口。"""

    def test_get_engine_health(self):
        """get_engine_health 返回健康度数据。"""
        from src.runtime.resource_stack import TaskResourceStack
        res = TaskResourceStack(task_id="test", cwd="/tmp")
        with patch.object(health_scorer, 'get_health_score') as mock:
            mock.return_value = {"health_score": 85, "total_tasks": 10}
            result = res.get_engine_health(1)
        assert result["health_score"] == 85

    def test_is_engine_healthy(self):
        """is_engine_healthy 返回布尔。"""
        from src.runtime.resource_stack import TaskResourceStack
        res = TaskResourceStack(task_id="test", cwd="/tmp")
        with patch.object(health_scorer, 'is_healthy', return_value=True):
            assert res.is_engine_healthy(1) is True

    def test_filter_healthy_engines(self):
        """filter_healthy_engines 返回过滤后列表。"""
        from src.runtime.resource_stack import TaskResourceStack
        res = TaskResourceStack(task_id="test", cwd="/tmp")
        with patch.object(health_scorer, 'filter_or_fallback', return_value=[1, 3]):
            result = res.filter_healthy_engines([1, 2, 3])
        assert result == [1, 3]

    def test_filter_healthy_engines_exception(self):
        """filter_healthy_engines 异常时返回原列表（不阻塞派发）。"""
        from src.runtime.resource_stack import TaskResourceStack
        res = TaskResourceStack(task_id="test", cwd="/tmp")
        with patch.object(health_scorer, 'filter_or_fallback', side_effect=RuntimeError("boom")):
            result = res.filter_healthy_engines([1, 2, 3])
        assert result == [1, 2, 3]  # 返回原列表


# ===== record_task_completion 测试 =====

class TestRecordTaskCompletion:
    """测试任务终态后的缓存刷新。"""

    def test_record_completion_refreshes_cache(self):
        """record_task_completion 即时重算并刷新缓存（Issue #85 即时模式）。"""
        health_scorer._cache[888] = {"score": 50, "data": {"health_score": 50}, "expires": time.time() + 100}
        health_scorer.record_task_completion(888, "completed")
        # 缓存应被刷新为新值，而非清空
        assert 888 in health_scorer._cache
        assert health_scorer._cache[888]["data"]["health_score"] == 0  # 引擎不存在→失联→0

    def test_record_failure_refreshes_cache(self):
        """record_task_completion 失败状态也即时刷新缓存。"""
        health_scorer._cache[777] = {"score": 50, "data": {"health_score": 50}, "expires": time.time() + 100}
        health_scorer.record_task_completion(777, "failed")
        assert 777 in health_scorer._cache
        assert health_scorer._cache[777]["data"]["health_score"] == 0  # 引擎不存在→失联→0

    def test_record_zero_runtime_noop(self):
        """runtime_id=0 时不清缓存（广播任务无特定引擎）。"""
        health_scorer._cache[666] = {"score": 50, "data": {}, "expires": time.time() + 100}
        health_scorer.record_task_completion(0, "completed")
        assert 666 in health_scorer._cache  # 不受影响


# ===== 心跳检查测试（Issue #135）=====

class TestHeartbeatCheck:
    """测试心跳因素对健康分的影响（Issue #135）。

    覆盖三种场景：
    - 心跳超时（last_seen_at 超过阈值）→ score=0
    - 无心跳记录（last_seen_at 为 NULL / 引擎从未上线）→ score=0
    - 心跳正常 → 按原失败率公式计算
    """

    def test_heartbeat_timeout_score_zero(self):
        """心跳超时（last_seen_at 超过 _HEARTBEAT_STALE_SECONDS）→ 健康分=0。"""
        # mock _fetch_task_stats 返回满分数据（total=0 → 原公式 100）
        with patch.object(health_scorer, '_fetch_task_stats', return_value={
                "total": 0, "failed": 0, "completed": 0,
                "consecutive_failures": 0, "last_status": ""}), \
             patch.object(health_scorer, '_fetch_heartbeat', return_value=False), \
             patch.object(health_scorer, '_persist_score'):
            data = health_scorer._compute_and_persist(910001)
        assert data["health_score"] == 0
        assert data["heartbeat_alive"] is False

    def test_no_heartbeat_record_score_zero(self):
        """无心跳记录（last_seen_at 为 NULL / 引擎从未上线）→ 健康分=0。"""
        with patch.object(health_scorer, '_fetch_task_stats', return_value={
                "total": 10, "failed": 0, "completed": 10,
                "consecutive_failures": 0, "last_status": "completed"}), \
             patch.object(health_scorer, '_fetch_heartbeat', return_value=False), \
             patch.object(health_scorer, '_persist_score'):
            data = health_scorer._compute_and_persist(910002)
        assert data["health_score"] == 0  # 即使全部成功，失联也强制 0
        assert data["heartbeat_alive"] is False

    def test_heartbeat_alive_normal_score(self):
        """心跳正常 → 按原失败率公式计算（total=0 → 100）。"""
        with patch.object(health_scorer, '_fetch_task_stats', return_value={
                "total": 0, "failed": 0, "completed": 0,
                "consecutive_failures": 0, "last_status": ""}), \
             patch.object(health_scorer, '_fetch_heartbeat', return_value=True), \
             patch.object(health_scorer, '_persist_score'):
            data = health_scorer._compute_and_persist(910003)
        assert data["health_score"] == 100  # total=0 + 心跳正常 → 满分
        assert data["heartbeat_alive"] is True

    def test_heartbeat_alive_with_failures(self):
        """心跳正常但有失败 → 按原失败率公式计算（非满分）。"""
        with patch.object(health_scorer, '_fetch_task_stats', return_value={
                "total": 10, "failed": 5, "completed": 5,
                "consecutive_failures": 1, "last_status": "failed"}), \
             patch.object(health_scorer, '_fetch_heartbeat', return_value=True), \
             patch.object(health_scorer, '_persist_score'):
            data = health_scorer._compute_and_persist(910004)
        assert data["health_score"] == 45  # 100*(1-0.5) - 5 = 45
        assert data["heartbeat_alive"] is True

    def test_heartbeat_timeout_overrides_good_stats(self):
        """心跳失联优先于失败率：即使统计数据满分，失联也返回 0。"""
        with patch.object(health_scorer, '_fetch_task_stats', return_value={
                "total": 100, "failed": 0, "completed": 100,
                "consecutive_failures": 0, "last_status": "completed"}), \
             patch.object(health_scorer, '_fetch_heartbeat', return_value=False), \
             patch.object(health_scorer, '_persist_score'):
            data = health_scorer._compute_and_persist(910005)
        assert data["health_score"] == 0
        assert data["total_tasks"] == 100  # 统计数据保留便于排障
        assert data["heartbeat_alive"] is False

    def test_heartbeat_unhealthy_engine_filtered(self):
        """失联引擎被 filter_healthy 过滤（score=0 < 阈值 30）。"""
        with patch.object(health_scorer, 'get_health_score') as mock_get:
            def _mock(rid, force_refresh=False):
                if rid == 1:
                    return {"health_score": 0, "consecutive_failures": 0,
                            "heartbeat_alive": False}
                return {"health_score": 80, "consecutive_failures": 0,
                        "heartbeat_alive": True}
            mock_get.side_effect = _mock
            result = health_scorer.filter_healthy([1, 2])
        assert 1 not in result  # 失联 → score=0 < 30 → 被过滤
        assert 2 in result      # 心跳正常 → score=80 → 保留

    def test_all_lost_heartbeat_fallback(self):
        """全部失联时 filter_or_fallback 降级取最优（不阻塞派发）。"""
        with patch.object(health_scorer, 'is_healthy', return_value=False), \
             patch.object(health_scorer, 'best_fallback', return_value=1):
            result = health_scorer.filter_or_fallback([1, 2, 3])
        assert result == [1]  # 降级兜底

    def test_fetch_heartbeat_null_last_seen(self):
        """_fetch_heartbeat: last_seen_at 为 NULL → False。"""
        with patch.object(health_scorer, '_db') as mock_db, \
             patch.object(health_scorer, '_lock') as mock_lock:
            mock_cursor = MagicMock()
            # (credential_id, status, last_seen_at)：远端引擎 last_seen NULL → 失联
            mock_cursor.fetchone.return_value = (99, "online", None)
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            result = health_scorer._fetch_heartbeat(910006)
        assert result is False

    def test_fetch_heartbeat_fresh(self):
        """_fetch_heartbeat: 远端引擎 last_seen_at 新鲜 → True。"""
        from datetime import datetime, timezone
        fresh = datetime.now(timezone.utc).isoformat()
        with patch.object(health_scorer, '_db') as mock_db, \
             patch.object(health_scorer, '_lock') as mock_lock:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = (99, "online", fresh)
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            result = health_scorer._fetch_heartbeat(910007)
        assert result is True

    def test_fetch_heartbeat_stale(self):
        """_fetch_heartbeat: 远端引擎 last_seen_at 超时 → False。"""
        from datetime import datetime, timezone, timedelta
        stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
        with patch.object(health_scorer, '_db') as mock_db, \
             patch.object(health_scorer, '_lock') as mock_lock:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = (99, "online", stale)
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            result = health_scorer._fetch_heartbeat(910008)
        assert result is False

    def test_fetch_heartbeat_local_engine_online_true(self):
        """_fetch_heartbeat: 本机 direct（credential_id=0）+ online → True（方案 A：probe 判定）。"""
        with patch.object(health_scorer, '_db') as mock_db, \
             patch.object(health_scorer, '_lock') as mock_lock:
            mock_cursor = MagicMock()
            # 本机引擎 last_seen 很旧（仅 local probe 600s 写一次）仍应在线
            mock_cursor.fetchone.return_value = (0, "online", "2020-01-01T00:00:00+00:00")
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            result = health_scorer._fetch_heartbeat(910010)
        assert result is True

    def test_fetch_heartbeat_local_engine_offline_false(self):
        """_fetch_heartbeat: 本机 direct + offline → False。"""
        with patch.object(health_scorer, '_db') as mock_db, \
             patch.object(health_scorer, '_lock') as mock_lock:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = (0, "offline", None)
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            result = health_scorer._fetch_heartbeat(910011)
        assert result is False

    def test_fetch_heartbeat_engine_not_exist(self):
        """_fetch_heartbeat: 引擎不存在（fetchone=None）→ False。"""
        with patch.object(health_scorer, '_db') as mock_db, \
             patch.object(health_scorer, '_lock') as mock_lock:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = None
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            result = health_scorer._fetch_heartbeat(910009)
        assert result is False
