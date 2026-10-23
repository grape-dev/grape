"""Issue #134: 机器健康度计算与派发过滤测试。

覆盖：
- health_scorer 健康分计算（失败率、连续失败惩罚、边界值）
- 派发链路健康度过滤（filter_healthy / best_fallback / filter_or_fallback）
- 广播机制无回归验证（plugin 模式返回 0，健康度过滤不干扰广播）
- resource_stack 健康度查询接口
- DB 持久化 + 内存缓存
"""
import os
import re
import time
import json
from datetime import datetime, timedelta, timezone
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
        """50% 失败率 → 75 分（失败率权重 0.5，给引擎试错空间）。"""
        score = health_scorer._compute_health_score(10, 5, 0)
        assert score == 75  # 100*(1-0.5*0.5) - 0 = 75

    def test_consecutive_failure_penalty(self):
        """连续失败惩罚：每次 -15，不设上限，总分 clamp 到 0。"""
        # 1 次失败：75 - 15 = 60
        assert health_scorer._compute_health_score(10, 5, 1) == 60
        # 2 次连续失败：75 - 30 = 45
        assert health_scorer._compute_health_score(10, 5, 2) == 45
        # 3 次连续失败：75 - 45 = 30（健康阈值临界）
        assert health_scorer._compute_health_score(10, 5, 3) == 30
        # 5 次连续失败：75 - 75 = 0
        assert health_scorer._compute_health_score(10, 5, 5) == 0
        # 10 次连续失败：75 - 150 = -75 → clamp 为 0
        assert health_scorer._compute_health_score(10, 5, 10) == 0

    def test_score_clamped_to_0_100(self):
        """健康分 clamp 到 [0, 100]。"""
        assert health_scorer._compute_health_score(10, 10, 10) >= 0
        assert health_scorer._compute_health_score(1, 0, 0) <= 100

    def test_low_failure_rate_high_score(self):
        """低失败率 → 高分。"""
        score = health_scorer._compute_health_score(100, 5, 0)
        assert score == 97.5 or score == 98  # 100*(1-0.05*0.5) = 97.5 → round → 98


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
        assert data["health_score"] == 60  # 100*(1-0.5*0.5) - 1*15 = 60
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


# ===== Issue #394: 健康分重置（health_reset_at 赦免点 + reset_health）=====

class TestHealthReset:
    """测试 `_effective_window_start` 的重置赦免语义与 `reset_health` 写入口。

    Issue #394：为仓库级/个人级 worker 设备的 runtime 引擎提供重置健康分功能。
    消费侧（_effective_window_start）读取 agent_runtimes.health_reset_at：
    - 重置点晚于 7 天窗口起点 → 统计窗口从重置点开始（赦免重置前失败）
    - 重置点早于窗口起点（已滚出窗口）→ 恢复普通 7 天窗口
    - 从未重置 / 非法时间戳 → 普通 7 天窗口
    """

    def test_effective_window_start_no_reset(self):
        """无重置记录 → 普通 7 天窗口起点。"""
        fixed = "2026-10-13T00:00:00+00:00"
        with patch.object(health_scorer, '_window_start', return_value=fixed), \
             patch.object(health_scorer, '_db') as mock_db, \
             patch.object(health_scorer, '_lock') as mock_lock:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = ("",)   # health_reset_at 为空
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            start = health_scorer._effective_window_start(920001)
        assert start == fixed

    def test_effective_window_start_recent_reset(self):
        """重置点晚于 7 天前 → 窗口从重置点开始（赦免）。"""
        from datetime import datetime, timezone, timedelta
        reset_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        fixed = "2026-10-13T00:00:00+00:00"
        with patch.object(health_scorer, '_window_start', return_value=fixed), \
             patch.object(health_scorer, '_db') as mock_db, \
             patch.object(health_scorer, '_lock') as mock_lock:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = (reset_ts,)
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            start = health_scorer._effective_window_start(920002)
        assert start > fixed

    def test_effective_window_start_old_reset(self):
        """重置点早于 7 天前（已滚出窗口）→ 回到普通 7 天窗口。"""
        old_ts = "2020-01-01T00:00:00+00:00"
        fixed = "2026-10-13T00:00:00+00:00"
        with patch.object(health_scorer, '_window_start', return_value=fixed), \
             patch.object(health_scorer, '_db') as mock_db, \
             patch.object(health_scorer, '_lock') as mock_lock:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = (old_ts,)
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            start = health_scorer._effective_window_start(920003)
        assert start == fixed

    def test_effective_window_start_invalid_timestamp(self):
        """非法时间戳 → 回到普通 7 天窗口（不抛异常）。"""
        fixed = "2026-10-13T00:00:00+00:00"
        with patch.object(health_scorer, '_window_start', return_value=fixed), \
             patch.object(health_scorer, '_db') as mock_db, \
             patch.object(health_scorer, '_lock') as mock_lock:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = ("not-a-date",)
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            start = health_scorer._effective_window_start(920004)
        assert start == fixed

    def test_reset_health_writes_reset_at_and_refreshes(self):
        """reset_health 写入 health_reset_at 并强制重算返回新健康分。"""
        test_rid = 920010
        with patch.object(health_scorer, '_db') as mock_db, \
             patch.object(health_scorer, '_lock') as mock_lock, \
             patch.object(health_scorer, 'invalidate_cache') as mock_inv, \
             patch.object(health_scorer, 'get_health_score') as mock_get:
            mock_cursor = MagicMock()
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            mock_get.return_value = {"runtime_id": test_rid, "health_score": 100,
                                     "consecutive_failures": 0, "heartbeat_alive": True}
            result = health_scorer.reset_health(test_rid)
        # 写入 health_reset_at（UPDATE agent_runtimes ... health_reset_at=...）
        assert mock_cursor.execute.called
        updates = [c for c in mock_cursor.execute.call_args_list
                   if "UPDATE agent_runtimes" in str(c.args[0])]
        assert updates, "reset_health 应执行 UPDATE agent_runtimes"
        assert "health_reset_at" in updates[0].args[0]
        # 清缓存 + 强制重算
        mock_inv.assert_called_once_with(test_rid)
        mock_get.assert_called_once_with(test_rid, force_refresh=True)
        assert result["health_score"] == 100

    def test_reset_health_returns_none_for_invalid_rid(self):
        """runtime_id=0 时 reset_health 返回 None（不做无意义写入）。"""
        with patch.object(health_scorer, '_db') as mock_db, \
             patch.object(health_scorer, '_lock') as mock_lock, \
             patch.object(health_scorer, 'get_health_score') as mock_get:
            mock_cursor = MagicMock()
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            result = health_scorer.reset_health(0)
        assert result is None
        mock_get.assert_not_called()


# ===== 健康分重置：赦免窗口的时区语义回归（2026-10-21 实测修复） =====

class TestResetWindowTimezone:
    """health-reset 的赦免窗口必须按纯 UTC 字面量与 agent_tasks.created_at 比较。

    背景（2026-10-21 dev 实测）：health_reset_at 写入的是带偏移的 ISO 串
    （...T07:48:42.916425+00:00），而 created_at 由 UTC_TIMESTAMP() 写入（纯 UTC datetime）。
    MySQL 会把该 ISO 串按会话时区解释 → 实测被抬高 8 小时
    （CAST('2026-10-21T07:49:24+00:00' AS DATETIME) = 15:49:25）→ 重置后 8 小时内的
    新失败全部落在窗口之外，健康分不降（赦免期变成"免疫期"）。
    """

    RID = 99998
    TID = "t_healthreset_tz_regression"

    def _cleanup(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE id=?", (self.TID,))
            c.execute("DELETE FROM runtime_health_scores WHERE runtime_id=?", (self.RID,))
            c.execute("DELETE FROM agent_runtimes WHERE id=?", (self.RID,))
            db.commit()

    def setup_method(self):
        self._cleanup()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO agent_runtimes (id, name, busy_slots, max_concurrent) VALUES (?,?,?,?)",
                      (self.RID, "healthreset-tz", 0, 4))
            # created_at 用 Python `datetime.now(timezone.utc).isoformat()` 写入（T 格式 ISO，
            # 与生产 event_pipeline 写入口径一致；agent_tasks 时间列为 TEXT，MySQL/SQLite 下
            # 同一字面量语义一致）——不依赖 DB 方言函数
            _now_lit = datetime.now(timezone.utc).isoformat()
            c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, attempts, status, created_at, prompt) "
                      "VALUES (?,?,?,?,?,?,?)", (self.TID, self.TID, self.RID, 0, "failed", _now_lit, ""))
            db.commit()

    def teardown_method(self):
        self._cleanup()

    def _set_reset(self, seconds_ago):
        reset_at = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
        with db_lock:
            db.cursor().execute("UPDATE agent_runtimes SET health_reset_at=? WHERE id=?", (reset_at, self.RID))
            db.commit()
        return reset_at

    def test_reset_window_counts_failure_after_reset(self):
        """重置点之后产生的失败必须被统计（修复前在 MySQL 上 total=0）。"""
        self._set_reset(2)
        stats = health_scorer._fetch_task_stats(self.RID)
        assert stats["total"] == 1, (
            "窗口起点被 MySQL 按会话时区误解释（ISO 带 +00:00 被抬高 8 小时）→ "
            "重置后的失败任务落在窗口外，健康分不会下降"
        )
        assert stats["failed"] == 1

    def test_window_literal_is_plain_utc_datetime(self):
        """传给 SQL 的窗口起点必须是 'YYYY-MM-DDTHH:MM:SS' 纯 UTC 字面量，且不在未来。

        Issue #428：T 分隔与 agent_tasks 时间列存量（Python isoformat 写入）对齐；
        无时区偏移后缀，不会再被 MySQL 按会话时区抬高 8 小时。
        """
        self._set_reset(5)
        literal = health_scorer._to_sql_utc(health_scorer._effective_window_start(self.RID))
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", literal), literal
        now_literal = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        assert literal <= now_literal, "窗口起点不应落在未来: %s > %s（时区误解释的典型症状）" % (literal, now_literal)


# ===== Issue #428: 重置赦免失效回归（混格式时间字符串比较） =====

class TestResetAmnestySameDay:
    """重置点同日存在 failed 任务时，赦免窗口必须把重置前的失败排除。

    背景（Issue #428）：`_to_sql_utc` 曾输出空格分隔 'YYYY-MM-DD HH:MM:SS'，而
    agent_tasks.created_at 是 TEXT 列、存量为 T 分隔 ISO（'2026-10-22T05:52:42.090948+00:00'）。
    SQL `created_at >= ?` 逐字符比较：日期前 10 位相同时第 11 位 T(0x54) > 空格(0x20)
    → 重置点同日的旧失败被误判「在窗口内」→ total=1/failed=1/consecutive=1 → 得分 0，
    「重置健康分」失效。修复后窗口字面量为 T 格式，与存量对齐。
    """

    RID = 99997
    TIDS = ["t_428_old_failed", "t_428_after_failed", "t_428_after_completed"]

    def _cleanup(self):
        with db_lock:
            c = db.cursor()
            for tid in self.TIDS:
                c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
            c.execute("DELETE FROM runtime_health_scores WHERE runtime_id=?", (self.RID,))
            c.execute("DELETE FROM agent_runtimes WHERE id=?", (self.RID,))
            db.commit()

    def setup_method(self):
        self._cleanup()

    def teardown_method(self):
        self._cleanup()

    def _insert_task(self, tid, status, created_at, completed_at):
        with db_lock:
            db.cursor().execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, attempts, status, "
                "created_at, completed_at, prompt) VALUES (?,?,?,?,?,?,?,?)",
                (tid, tid, self.RID, 0, status, created_at, completed_at, ""))
            db.commit()

    def _set_reset(self, reset_at):
        with db_lock:
            db.cursor().execute("UPDATE agent_runtimes SET health_reset_at=? WHERE id=?",
                                (reset_at, self.RID))
            db.commit()

    def test_same_day_failed_before_reset_excluded(self):
        """重置点同日、重置之前的 failed 任务不入窗（修复前漏进窗口 → 赦免失效）。"""
        with db_lock:
            db.cursor().execute(
                "INSERT INTO agent_runtimes (id, name, busy_slots, max_concurrent) VALUES (?,?,?,?)",
                (self.RID, "issue428-amnesty", 0, 4))
            db.commit()
        # 重置点：今天 12:00（与旧失败同一天，复现 Issue #428 的关键条件）
        reset_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        reset_at = f"{reset_day}T12:00:00+00:00"
        self._set_reset(reset_at)
        # 重置前 29 分钟的同日失败（存量格式：T + 微秒 + 偏移后缀，复刻真实 task id 形态）
        old_failed = f"{reset_day}T11:31:00.090948+00:00"
        self._insert_task(self.TIDS[0], "failed", old_failed, old_failed)

        stats = health_scorer._fetch_task_stats(self.RID)
        assert stats["total"] == 0, (
            "重置点同日的旧失败被计入窗口（混格式比较赦免失效）: created_at=%s window=%s"
            % (old_failed, health_scorer._to_sql_utc(reset_at))
        )
        assert stats["failed"] == 0
        assert stats["consecutive_failures"] == 0
        assert stats["last_status"] == "failed"  # last_status 不受窗口限制，仍如实反映最近终态

    def test_same_day_failure_after_reset_counted(self):
        """重置点之后的同日失败正常入窗（不制造免疫期）。"""
        with db_lock:
            db.cursor().execute(
                "INSERT INTO agent_runtimes (id, name, busy_slots, max_concurrent) VALUES (?,?,?,?)",
                (self.RID, "issue428-amnesty", 0, 4))
            db.commit()
        reset_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        reset_at = f"{reset_day}T12:00:00+00:00"
        self._set_reset(reset_at)
        new_failed = f"{reset_day}T13:05:00.123456+00:00"
        self._insert_task(self.TIDS[1], "failed", new_failed, new_failed)

        stats = health_scorer._fetch_task_stats(self.RID)
        assert stats["total"] == 1
        assert stats["failed"] == 1
        assert stats["consecutive_failures"] == 1

    def test_score_100_after_reset_with_same_day_old_failures(self):
        """验收主路径：重置点同日存在 failed 任务时，重置后 score=100 / 无历史。"""
        with db_lock:
            db.cursor().execute(
                "INSERT INTO agent_runtimes (id, name, busy_slots, max_concurrent) VALUES (?,?,?,?)",
                (self.RID, "issue428-amnesty", 0, 4))
            db.commit()
        reset_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        reset_at = f"{reset_day}T12:00:00+00:00"
        self._set_reset(reset_at)
        # 重置前同日连续两条失败（含微秒/偏移后缀的存量变体）
        self._insert_task(self.TIDS[0], "failed",
                          f"{reset_day}T10:00:00.000111+00:00",
                          f"{reset_day}T10:30:00.000222+00:00")
        with patch.object(health_scorer, '_fetch_heartbeat', return_value=True), \
             patch.object(health_scorer, '_persist_score'):
            data = health_scorer._compute_and_persist(self.RID)
        assert data["health_score"] == 100
        assert data["total_tasks"] == 0
        assert data["failed_tasks"] == 0
        assert data["consecutive_failures"] == 0

    def test_ordering_with_mixed_format_history(self):
        """混合历史数据（微秒/偏移后缀/纯秒等 T 格式变体）下 consecutive/last_status 正确。

        修复前：窗口字面量为空格格式，与 T 格式存量在第 11 位交错；修复后两侧同为
        T 格式，`ORDER BY completed_at DESC` 的字符串排序口径与比较一致（同秒内
        带后缀的存量串字典序略大，属保守方向，不影响秒级以上排序正确性）。
        """
        with db_lock:
            db.cursor().execute(
                "INSERT INTO agent_runtimes (id, name, busy_slots, max_concurrent) VALUES (?,?,?,?)",
                (self.RID, "issue428-amnesty", 0, 4))
            db.commit()
        reset_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # 不设 health_reset_at → 普通 7 天窗口
        # 时间倒序（旧→新）：failed(10:00) → failed(11:00) → completed(13:00) → failed(14:00)
        self._insert_task(self.TIDS[0] + "_a", "failed",
                          f"{reset_day}T10:00:00.000111+00:00",
                          f"{reset_day}T10:00:00.000111+00:00")
        self._insert_task(self.TIDS[0] + "_b", "failed",
                          f"{reset_day}T11:00:00+00:00",
                          f"{reset_day}T11:00:00.999999+00:00")
        self._insert_task(self.TIDS[2] + "_c", "completed",
                          f"{reset_day}T13:00:00Z",
                          f"{reset_day}T13:00:00")
        self._insert_task(self.TIDS[1] + "_d", "failed",
                          f"{reset_day}T14:00:00.500000+00:00",
                          f"{reset_day}T14:00:00.500000+00:00")

        stats = health_scorer._fetch_task_stats(self.RID)
        assert stats["total"] == 4
        assert stats["failed"] == 3
        # 最近一条是 failed(14:00)，其前一条是 completed(13:00) → 连续失败 = 1
        assert stats["consecutive_failures"] == 1, (
            "混合格式历史下连续失败统计错误: %s" % stats
        )
        assert stats["last_status"] == "failed"
