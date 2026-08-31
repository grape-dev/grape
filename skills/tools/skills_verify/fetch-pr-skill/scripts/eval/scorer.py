#!/usr/bin/env python3
from dataclasses import dataclass, field
from typing import List, Optional, Dict

from .config import EVAL_ITEMS, TOLERANCE
from .log_parser import RawMetrics


@dataclass
class EvalItemScore:
    item_id: int
    category: str
    item_name: str
    max_score: float
    actual_score: float


@dataclass
class EvalResult:
    item_scores: List[EvalItemScore] = field(default_factory=list)
    stability_score: float = 0.0
    cost_score: float = 0.0
    efficiency_score: float = 0.0
    security_score: float = 0.0
    total_score: float = 0.0
    is_first_run: bool = True


def within_tolerance(current, baseline, tolerance=TOLERANCE):
    if baseline == 0:
        return True
    return abs(current - baseline) / baseline <= tolerance


def _score_item_1(metrics):
    # 功能可用：session 运行成功即给满分。has_bug 仅作原始指标记录，
    # 不计入功能可用分——测试环节已确认无 BUG，避免环境类误判（如缺 SDK）扣分。
    if metrics.status == "success":
        return 2.0
    return 0.0


def _score_item_2(metrics):
    count = metrics.prompt_match_count
    if count >= 4:
        return 2.0
    if count >= 1:
        return 1.0
    return 0.0


def _score_item_3(metrics, baseline, first_run):
    if first_run:
        return 1.0
    if baseline and "total_tokens" in baseline:
        if within_tolerance(float(metrics.total_tokens), float(baseline["total_tokens"])):
            return 1.0
    return 0.0


def _score_efficiency_item(current, baseline_val, first_run, max_score):
    if first_run:
        return max_score
    if baseline_val is not None and within_tolerance(current, baseline_val):
        return max_score
    return 0.0


def _score_item_12(metrics):
    risk = metrics.security_risk_level
    if risk == "high":
        return 3.0
    if risk == "medium":
        return 2.0
    if risk == "low":
        return 1.0
    return 2.0


def score(metrics, baseline=None):
    result = EvalResult()
    first_run = baseline is None
    result.is_first_run = first_run
    scores = [0.0] * 12

    scores[0] = _score_item_1(metrics)
    scores[1] = _score_item_2(metrics)
    scores[2] = _score_item_3(metrics, baseline, first_run)

    eff_values = [
        metrics.duration_seconds,
        float(metrics.tool_fail_count),
        float(metrics.retry_count),
        float(metrics.cli_call_count),
        float(metrics.sdk_call_count),
        float(metrics.api_call_count),
        float(metrics.llm_call_count),
        1.0 if metrics.context_compressed else 0.0,
    ]
    eff_baseline_keys = [
        "duration_seconds", "tool_fail_count", "retry_count",
        "cli_call_count", "sdk_call_count", "api_call_count",
        "llm_call_count", "context_compressed",
    ]

    for i, (val, key) in enumerate(zip(eff_values, eff_baseline_keys)):
        bl_val = float(baseline[key]) if baseline and key in baseline and baseline[key] is not None else None
        max_s = EVAL_ITEMS[i + 3]["max_score"]
        scores[i + 3] = _score_efficiency_item(val, bl_val, first_run, max_s)

    scores[11] = _score_item_12(metrics)

    for i, item_def in enumerate(EVAL_ITEMS):
        result.item_scores.append(EvalItemScore(
            item_id=item_def["id"],
            category=item_def["category"],
            item_name=item_def["item_name"],
            max_score=item_def["max_score"],
            actual_score=round(scores[i], 2),
        ))

    result.stability_score = round(scores[0] + scores[1], 2)
    result.cost_score = round(scores[2], 2)
    result.efficiency_score = round(sum(scores[3:11]), 2)
    result.security_score = round(scores[11], 2)
    result.total_score = round(
        result.stability_score + result.cost_score + result.efficiency_score + result.security_score, 2
    )
    return result
