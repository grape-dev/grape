"""级联终态收敛指标（Issue #3877）：缺列降级 / 收敛失败的计数器。

背景
----
分库（三库）拓扑下存量 runtime 库的 ``events`` 表缺 ``cascade_*`` 五列，
``_cascade_event_terminal_impl`` 的终态 UPDATE 每次抛 ``1054 Unknown column``：
事件终态收敛 100% 失败、``event_active_guard`` 不释放（只能等 TTL 30min），
而失败被外层 ``except`` 吞成一条 WARNING —— 72 小时 54 次，值守无从感知。

本模块把「收敛失败 / 缺列降级」升级为可按 reason/stage 聚合的计数指标；
日志侧由 ``service`` 打结构化行（Loki 可检索），两条通道互不依赖。

分层约束（与 ``src/kernel/comment_metrics.py`` 同口径）
----------------------------------------------------
- kernel 不 import domains（``scripts/check-arch.py`` R1）；
- ``CollectorRegistry`` 由 insight 域持有，这里只定义「可安装收集器」，
  由 ``src/domains/insight/observability/__init__.py`` 启动时 ``install(registry)``；
  未安装时所有 observe 是 no-op（worker 进程不暴露 /metrics，也不影响主流程）；
- 标签取值限枚举，控制基数：``reason``/``stage`` 固定枚举；``missing`` 是
  ``events`` 五列的子集拼串（最多 32 种组合）。
"""
from __future__ import annotations

import threading
from typing import Any, Iterable

FAIL_METRIC = "grape_event_cascade_converge_failed_total"
DEGRADED_METRIC = "grape_event_cascade_converge_degraded_total"

#: 收敛失败原因（枚举）：未知列 / 无可写列 / 其它 DB 错误 / 未知
FAIL_REASONS = ("unknown_column", "no_writable_columns", "db_error", "unknown")
#: 失败阶段（枚举）：prepare（算终态/取真实结论）/ update（终态 UPDATE）/ guard（守卫释放）
FAIL_STAGES = ("prepare", "update", "guard")

_LOCK = threading.Lock()
#: 已注册的 registry 及其收集器：[{"registry", "failed", "degraded"}]
_INSTALLS: list[dict] = []


def install(registry: Any) -> bool:
    """把收敛指标注册进 prometheus_client CollectorRegistry（按 registry 幂等）。

    返回 True 表示本次完成注册；同一 registry 二次调用返回 False（不重复建收集器）。
    注册失败（重名等）只降级为 no-op，不影响收敛主流程。
    """
    with _LOCK:
        if any(inst["registry"] is registry for inst in _INSTALLS):
            return False
        try:
            from prometheus_client import Counter

            _inst = {
                "registry": registry,
                "failed": Counter(
                    FAIL_METRIC,
                    "Event cascade terminal convergence failures by enum reason/stage.",
                    ["reason", "stage"],
                    registry=registry,
                ),
                "degraded": Counter(
                    DEGRADED_METRIC,
                    "Event cascade terminal convergence degraded to writing existing "
                    "columns only (missing cascade_* columns).",
                    ["missing"],
                    registry=registry,
                ),
            }
        except ValueError:  # pragma: no cover - 同一 registry 重复注册
            return False
        _INSTALLS.append(_inst)
        return True


def classify_failure(detail) -> str:
    """把收敛异常归到枚举 reason（未知一律 unknown，不丢数、不涨基数）。"""
    text = "" if detail is None else str(detail)
    low = text.lower()
    if "no writable column" in low:
        return "no_writable_columns"
    if "1054" in low or "unknown column" in low or "no such column" in low:
        return "unknown_column"
    if text:
        return "db_error"
    return "unknown"


def _norm_reason(reason) -> str:
    r = (reason or "").strip().lower()
    return r if r in FAIL_REASONS else "unknown"


def _norm_stage(stage) -> str:
    s = (stage or "").strip().lower()
    return s if s in FAIL_STAGES else "update"


#: 缺列标签最大长度（PR #2157 复审 C3 数值证据门）：Prometheus label 值越长基数越大，
#: cascade 五列全缺时拼接约 90 字符，96 覆盖实际取值且留一点余量后即截断保基数可控。
MISSING_LABEL_MAX_CHARS = 96


def missing_label(columns: Iterable[str] | None) -> str:
    """缺列标签值：列名按固定顺序拼串（空 → ``-``，超长截断防标签爆炸）。"""
    _cols = sorted({str(c).strip() for c in (columns or ()) if str(c).strip()})
    if not _cols:
        return "-"
    return ",".join(_cols)[:MISSING_LABEL_MAX_CHARS]


def observe_failed(reason, stage: str = "update") -> dict:
    """记录一次收敛失败，返回归一后的 ``{"reason", "stage"}``（调用方据此打日志）。"""
    r = _norm_reason(reason)
    s = _norm_stage(stage)
    with _LOCK:
        for inst in _INSTALLS:
            try:
                inst["failed"].labels(reason=r, stage=s).inc()
            except Exception:  # pragma: no cover - prometheus 版本差异
                pass
    return {"reason": r, "stage": s}


def observe_degraded(missing_columns: Iterable[str] | None) -> str:
    """记录一次「缺列降级」（只写存在列），返回归一后的 missing 标签值。"""
    label = missing_label(missing_columns)
    with _LOCK:
        for inst in _INSTALLS:
            try:
                inst["degraded"].labels(missing=label).inc()
            except Exception:  # pragma: no cover - prometheus 版本差异
                pass
    return label


def reset() -> None:
    """清空计数（测试用；不影响已安装的收集器对象本体）。"""
    with _LOCK:
        _INSTALLS.clear()