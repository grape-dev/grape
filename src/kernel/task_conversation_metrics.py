"""任务会话 get-or-create 指标（Issue #4183）：可安装收集器 + 无副作用 observe。

背景：旧 Issue 重派时 `_ensure_task_conversation` 曾用「最近 50 条」窗口判断存在性，
命中部分唯一索引 `idx_chat_conv_repo_issue` 后 1062 兜底又复用同一窗口重查，
最终只留一条 WARNING 并返回空串——任务侧会话绑定丢失且监控上只是噪音。

本模块把「1062 后仍按业务键查不到」升级为可按 reason 聚合的指标：
    grape_task_conversation_ensure_failed_total{reason}

分层约束（与 `src/kernel/comment_metrics.py` 同口径）：
- kernel 不 import domains；`CollectorRegistry` 由 insight 域持有，
  这里只定义「可安装收集器」，由 `src/domains/insight/observability/__init__.py`
  启动时 `install(registry)`，未安装时 observe 是 no-op。
"""
from __future__ import annotations

import threading
from typing import Any

ENSURE_FAILED_TOTAL_METRIC = "grape_task_conversation_ensure_failed_total"

REASONS = ("duplicate_unresolved",)

_LOCK = threading.Lock()
_INSTALLS: list[dict] = []


def install(registry: Any) -> bool:
    """把任务会话指标注册进 prometheus_client CollectorRegistry（按 registry 幂等）。"""
    with _LOCK:
        if any(inst["registry"] is registry for inst in _INSTALLS):
            return False
        try:
            from prometheus_client import Counter
            counter = Counter(
                ENSURE_FAILED_TOTAL_METRIC,
                "Task conversation get-or-create failures that stayed unresolved "
                "after duplicate-key detection, by enum reason.",
                ["reason"],
                registry=registry,
            )
        except ValueError:  # pragma: no cover - 同一 registry 重复注册
            return False
        _INSTALLS.append({"registry": registry, "failed": counter})
        return True


def _norm_reason(reason) -> str:
    r = (reason or "").strip().lower()
    return r if r in REASONS else "duplicate_unresolved"


def observe_unresolved(reason: str = "duplicate_unresolved") -> str:
    """记录一次「1062 后仍查不到」的可观测事件，返回归一后的 reason。"""
    r = _norm_reason(reason)
    with _LOCK:
        for inst in _INSTALLS:
            inst["failed"].labels(reason=r).inc()
    return r


def reset() -> None:
    """清空已安装收集器（测试隔离用；不影响 register registry 本身）。"""
    with _LOCK:
        _INSTALLS.clear()
