# -*- coding: utf-8 -*-
"""health_scorer Decimal 崩溃修复回归测试（2026-10-23）。

背景：commit 910b38baf 把健康分公式加上 * 0.5 小数常量后，MySQL 后端
SUM()/COUNT() 返回的 decimal.Decimal 与 float 混算抛 TypeError
（Decimal 故意不允许与 float 隐式混合），claim_by_tag → _is_engine_healthy
→ _compute_health_score 崩溃导致引擎每轮认领必拒。

T1 纯 Decimal 入参不崩且数值正确
T2 Decimal/int/None 混合入参不崩
T3 全 None 入参 → 100（无历史数据默认健康）
T4 total=0（Decimal）→ 100
T5 数值口径：total=6 failed=2 consec=3 → base 85 - penalty 45 = 40
T6 _consecutive_failure_penalty Decimal/None 入口归一
T7 _fetch_task_stats 归一化：DB 返回 Decimal 时 result 为 int
"""
from decimal import Decimal
from unittest.mock import patch

from src.runtime.health_scorer import (
    _compute_health_score,
    _consecutive_failure_penalty,
    _fetch_task_stats,
)


class TestDecimalCrashFix:
    def test_T1_pure_decimal_inputs(self):
        # total=20 failed=6 consec=3：base=round(100*0.85)=85，penalty=45 → 40
        assert _compute_health_score(Decimal("20"), Decimal("6"), Decimal("3")) == 40

    def test_T2_mixed_types(self):
        # total=6 failed=2 consec=3：base=round(100*(1-2/12))=83，penalty=45 → 38
        assert _compute_health_score(6, Decimal("2"), 3) == 38
        assert _compute_health_score(Decimal("6"), 2, Decimal("3")) == 38

    def test_T3_none_inputs_return_100(self):
        assert _compute_health_score(None, None, None) == 100

    def test_T4_zero_total_decimal(self):
        assert _compute_health_score(Decimal("0"), Decimal("0"), Decimal("0")) == 100

    def test_T5_numeric_semantics(self):
        # total=20 failed=6：failure_rate=0.3，base=round(100*0.85)=85；penalty=3*15=45
        assert _compute_health_score(Decimal("20"), Decimal("6"), Decimal("3")) == 40
        # total=6 failed=2：base=round(83.33)=83；penalty 45 → 38
        assert _compute_health_score(6, 2, 3) == 38
        # total=6 failed=2 consec=5：base=83，penalty=75 → 8（未触下界 clamp）
        assert _compute_health_score(6, 2, 5) == 8
        # total=6 failed=2 consec=6：base=83，penalty=90 → clamp 0
        assert _compute_health_score(6, 2, 6) == 0
        # clamp 上界：全成功 + 无连挂 = 100
        assert _compute_health_score(6, 0, 0) == 100

    def test_T6_penalty_normalization(self):
        assert _consecutive_failure_penalty(Decimal("3")) == 45
        assert _consecutive_failure_penalty(None) == 0
        assert _consecutive_failure_penalty(Decimal("0")) == 0

    def test_T7_fetch_task_stats_normalizes_decimal(self):
        class _FakeCursor:
            def execute(self, *a, **kw):
                pass

            def fetchone(self):
                if not getattr(self, "_first", False):
                    self._first = True
                    return (Decimal("20"), Decimal("6"), Decimal("3"))
                return None

            def fetchall(self):
                return []

        class _FakeDb:
            def cursor(self):
                return _FakeCursor()

        with patch("src.runtime.health_scorer._db", return_value=_FakeDb()), \
             patch("src.runtime.health_scorer._effective_window_start", return_value="2026-10-16T00:00:00"):
            stats = _fetch_task_stats(1)
        assert stats["total"] == 20 and isinstance(stats["total"], int)
        assert stats["failed"] == 6 and isinstance(stats["failed"], int)
        assert stats["completed"] == 3 and isinstance(stats["completed"], int)
