"""组织下派/供给指标（可安装收集器，A13 整改）。

分层约束：kernel 不能 import domains（check-arch R1 HARD）；Prometheus 的
CollectorRegistry 由 insight 域持有。这里只定义「可安装的收集器 + 无副作用的
observe 入口」，由 insight 的 metrics server 启动时调用 ``install(registry)`` 注入。
安装前 observe() 是 no-op。口径与 ``src/kernel/gsb/metrics.py`` 一致。
"""
from __future__ import annotations

from typing import Any

_DISPATCH_TOTAL: Any = None
_DISPATCH_DURATION: Any = None
_PROVISION_TOTAL: Any = None
_PROVISION_DURATION: Any = None
_INFLIGHT: Any = None
_PIPELINE_ITEMS: Any = None
_PIPELINE_RUNS: Any = None

_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 600)


def install(registry: Any) -> None:
    """把组织下派/供给指标注册到 prometheus_client CollectorRegistry（幂等）。"""
    global _DISPATCH_TOTAL, _DISPATCH_DURATION, _PROVISION_TOTAL
    global _PROVISION_DURATION, _INFLIGHT, _PIPELINE_ITEMS, _PIPELINE_RUNS
    if _DISPATCH_TOTAL is not None:
        return
    from prometheus_client import Counter, Gauge, Histogram

    try:
        _DISPATCH_TOTAL = Counter(
            "grape_org_dispatch_total",
            "Organization dispatch events.",
            ["action", "status"],
            registry=registry,
        )
        _DISPATCH_DURATION = Histogram(
            "grape_org_dispatch_duration_seconds",
            "Organization dispatch action duration.",
            ["action"],
            registry=registry,
            buckets=_BUCKETS,
        )
        _PROVISION_TOTAL = Counter(
            "grape_org_provision_total",
            "Organization provisioning attempts.",
            ["result"],
            registry=registry,
        )
        _PROVISION_DURATION = Histogram(
            "grape_org_provision_duration_seconds",
            "Organization provisioning duration.",
            ["result"],
            registry=registry,
            buckets=_BUCKETS,
        )
        _INFLIGHT = Gauge(
            "grape_org_dispatch_inflight",
            "Organization dispatches currently not in a terminal state.",
            registry=registry,
        )
        _PIPELINE_ITEMS = Counter(
            "grape_pipeline_advance_items_total",
            "Pipeline advance items by result.",
            ["result"],
            registry=registry,
        )
        _PIPELINE_RUNS = Counter(
            "grape_pipeline_advance_runs_total",
            "Pipeline advance rounds by outcome.",
            ["result"],
            registry=registry,
        )
    except ValueError:  # pragma: no cover - 同一 registry 重复注册
        pass


def observe_dispatch(action: str, status: str = "", seconds: float | None = None) -> None:
    """记录一次下派动作（created/reused/terminal/cancelled/retried）。"""
    if _DISPATCH_TOTAL is None:
        return
    _DISPATCH_TOTAL.labels(action=str(action), status=str(status or "")).inc()
    if seconds is not None and _DISPATCH_DURATION is not None:
        _DISPATCH_DURATION.labels(action=str(action)).observe(float(seconds))


def observe_provision(result: str, seconds: float | None = None) -> None:
    """记录一次组织供给结果（ready/failed/skipped/lease_lost）。"""
    if _PROVISION_TOTAL is None:
        return
    _PROVISION_TOTAL.labels(result=str(result or "unknown")).inc()
    if seconds is not None and _PROVISION_DURATION is not None:
        _PROVISION_DURATION.labels(result=str(result or "unknown")).observe(float(seconds))


def set_dispatch_inflight(count: int) -> None:
    """设置当前非终态下派批次数。"""
    if _INFLIGHT is None:
        return
    _INFLIGHT.set(max(0, int(count or 0)))


def observe_pipeline_advance(stats: dict | None = None) -> None:
    """记录一轮流水线确定性推进（advanced/scanned/skipped 与轮次结果）。"""
    if _PIPELINE_ITEMS is None or _PIPELINE_RUNS is None:
        return
    stats = stats or {}
    for key in ("advanced", "scanned", "skipped"):
        try:
            value = int(stats.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            _PIPELINE_ITEMS.labels(result=key).inc(value)
    if stats.get("busy"):
        result = "busy"
    elif int(stats.get("advanced") or 0) > 0:
        result = "advanced"
    elif int(stats.get("scanned") or 0) > 0:
        result = "idle"
    else:
        result = "empty"
    _PIPELINE_RUNS.labels(result=result).inc()
