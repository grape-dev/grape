"""plugin-tester QA 门禁（Issue #1093）。

回归分数纳入发布/升级放行判定：不达标不放行。
- 触发点（双触发）：core_update（芯更新）/ release（新版本发布）
- 判定口径：report.score ≥ threshold → 放行；否则阻断并给出可读原因
  （含分数、阈值、失败用例清单）
- 默认阈值 80，可经环境变量 PLUGIN_QA_GATE_THRESHOLD 调优（门禁配置
  显式非法值直接拒绝，不做静默放行）

芯更新与新版本发布流程（调度侧）在回归执行后调用 evaluate_gate 依判定
结果放行/阻断；本模块不感知调度实现，只提供统一判定口径。
"""
from __future__ import annotations

import os
from typing import List, Optional

from src.domains.mgmt.plugin_tester.runner import BenchmarkReport

# ===== 双触发点（Spec 决策：芯更新 + 新版本发布）=====

REGRESSION_TRIGGER_CORE_UPDATE = "core_update"
REGRESSION_TRIGGER_RELEASE = "release"
REGRESSION_TRIGGERS = (REGRESSION_TRIGGER_CORE_UPDATE, REGRESSION_TRIGGER_RELEASE)

# 默认门禁阈值（0-100）
DEFAULT_GATE_THRESHOLD = 80.0
_GATE_THRESHOLD_ENV = "PLUGIN_QA_GATE_THRESHOLD"


def _resolve_threshold(threshold: Optional[float]) -> float:
    if threshold is None:
        raw = os.environ.get(_GATE_THRESHOLD_ENV)
        threshold = float(raw) if raw not in (None, "") else DEFAULT_GATE_THRESHOLD
    threshold = float(threshold)
    if not (0.0 <= threshold <= 100.0):
        raise ValueError("门禁阈值越界: %s（应在 0-100）" % threshold)
    return threshold


def _resolve_trigger(trigger: Optional[str]) -> str:
    # 触发点必须显式可审计：缺失/空值直接报错，不静默回落 release
    if not isinstance(trigger, str) or not trigger.strip():
        raise ValueError("回归触发点缺失（应为 %s）" % "/".join(REGRESSION_TRIGGERS))
    if trigger not in REGRESSION_TRIGGERS:
        raise ValueError("未知回归触发点: %r（合法：%s）" % (trigger, "/".join(REGRESSION_TRIGGERS)))
    return trigger


class GateDecision:
    """门禁判定结果（放行/阻断 + 依据）。"""

    __slots__ = ("passed", "score", "threshold", "trigger", "reasons", "plugin", "benchmark_version")

    def __init__(self, passed: bool, score: float, threshold: float, trigger: str, reasons: List[str],
                 plugin: str, benchmark_version: str):
        self.passed = passed
        self.score = score
        self.threshold = threshold
        self.trigger = trigger
        self.reasons = reasons
        self.plugin = plugin
        self.benchmark_version = benchmark_version

    def as_dict(self):
        return {
            "passed": self.passed,
            "score": self.score,
            "threshold": self.threshold,
            "trigger": self.trigger,
            "reasons": list(self.reasons),
            "plugin": self.plugin,
            "benchmark_version": self.benchmark_version,
        }


def evaluate_gate(
    report: BenchmarkReport,
    threshold: Optional[float] = None,
    trigger: Optional[str] = None,
) -> GateDecision:
    """按阈值判定回归分数是否放行（发布/升级门禁）。

    trigger 缺省（None）取报告自带触发点（report.trigger）；显式传空值报错，
    不静默回落 release——双触发点（core_update/release）判定必须可审计。
    """
    resolved_threshold = _resolve_threshold(threshold)
    resolved_trigger = _resolve_trigger(trigger if trigger is not None else report.trigger)

    reasons: List[str] = []
    if report.score < resolved_threshold:
        reasons.append(
            "回归分数 %.1f 低于门禁阈值 %.1f（触发点：%s）" % (report.score, resolved_threshold, resolved_trigger)
        )
        for failed in report.failed_cases():
            reasons.append("失败用例 %s（%s）：%s" % (failed.case_id, failed.category, failed.detail))

    return GateDecision(
        passed=not reasons,
        score=report.score,
        threshold=resolved_threshold,
        trigger=resolved_trigger,
        reasons=reasons,
        plugin=report.plugin,
        benchmark_version=report.benchmark_version,
    )
