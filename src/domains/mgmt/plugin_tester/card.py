"""统一插件卡片分数字段数据源（Issue #1093）。

回归分数渲染到统一插件卡片（B3 #1086 组件）：本模块产出卡片数据源的
回归分数字段（CARD_FIELD_REGRESSION），供卡片组件消费/渲染。

字段契约（JSON 可序列化，卡片数据源为 JSON 字段）：
- plugin / plugin_version：插件标识与版本
- regression_score：回归分数（0-100，一位小数）
- gate_status：passed / blocked
- gate_threshold / gate_reasons：门禁阈值与阻断原因（运营可见不达标）
- trigger：core_update / release
- benchmark_version：基准版本
- categories：{category: {passed, total}} 类别统计
- run_at：回归执行时间（ISO8601）
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from src.domains.mgmt.plugin_tester.gate import GateDecision
from src.domains.mgmt.plugin_tester.runner import BenchmarkReport

# 统一插件卡片回归分数字段名（B3 组件按此字段读取）
CARD_FIELD_REGRESSION = "regression_score"


def build_card_score_payload(report: BenchmarkReport, decision: GateDecision) -> Dict[str, Any]:
    """回归报告 + 门禁判定 → 卡片分数字段数据。"""
    return {
        "plugin": report.plugin,
        "plugin_version": report.plugin_version,
        CARD_FIELD_REGRESSION: report.score,
        "gate_status": "passed" if decision.passed else "blocked",
        "gate_threshold": decision.threshold,
        "gate_reasons": list(decision.reasons),
        "trigger": decision.trigger,
        "benchmark_version": report.benchmark_version,
        "categories": {
            cat: {"passed": passed, "total": total}
            for cat, (passed, total) in report.category_stats().items()
        },
        "run_at": datetime.now(timezone.utc).isoformat(),
    }
