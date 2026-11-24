"""结果评论投递指标（Issue #4065）：可安装收集器 + 无副作用 observe。

背景：autopilot 任务（`autopilot-*` 合成 issue_id）的结果回帖在生产上 100% 失败——
`_post_result_to_issue` 的 try/except 把远程 400 吞成一条 WARNING，1 小时 7 次结果丢失、
值守无从感知。本模块把「投递失败」升级成可按 reason 聚合的指标，并对连续失败给告警。

分层约束（与 `src/kernel/org_metrics.py` 同口径）：
- kernel 不 import domains（check-arch R1 HARD）；
- `CollectorRegistry` 由 insight 域持有，这里只定义「可安装收集器」，
  由 `src/domains/insight/observability/__init__.py` 启动时 `install(registry)`，
  未安装时所有 observe 是 no-op；
- 一个进程可以注册进多个 registry（主控 /metrics 是独立 registry，测试各自 new 一个），
  observe 对**所有已注册**的收集器同步计数，互不影响；
- 连续失败告警不依赖 Prometheus：worker 进程不暴露 /metrics，因此达阈值时同时打一条
  ERROR 结构化日志行 `RESULT_COMMENT_ALERT`（Loki 可检索），两条通道都不断。

reason 限枚举（控制标签基数）：non_numeric_ident / http_4xx / http_5xx / network / unknown。
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any

FAIL_REASONS = ("non_numeric_ident", "http_4xx", "http_5xx", "network", "unknown")

FAILED_TOTAL_METRIC = "grape_comment_post_failed_total"
SKIPPED_TOTAL_METRIC = "grape_comment_post_skipped_total"
CONSECUTIVE_METRIC = "grape_comment_post_consecutive_failures"

# 连续失败告警阈值：默认 5（Issue #4065 spec 口径），夹到 1..1000 防误配成 0 让告警恒发/恒不发
ALERT_THRESHOLD_ENV = "GRAPE_COMMENT_POST_FAIL_ALERT_THRESHOLD"
DEFAULT_ALERT_THRESHOLD = 5
_MAX_THRESHOLD = 1000

# HTTP 状态码优先从响应体（`{'status_code': 400}` / `HTTP 400`）取
_STATUS_RE = re.compile(r"(?:status_code['\"]?\s*[:=]\s*|\bHTTP[ /])(\d{3})", re.I)
_NETWORK_HINTS = ("timeout", "timed out", "connection", "econnrefused", "econnreset",
                  "socket", "network is unreachable", "temporary failure in name resolution")

_LOCK = threading.Lock()
# 已注册的 registry 及其收集器：[{"registry", "failed", "skipped", "consecutive"}]
_INSTALLS: list[dict] = []
_STREAKS: dict[str, int] = {}


def install(registry: Any) -> bool:
    """把投递指标注册进 prometheus_client CollectorRegistry（按 registry 幂等）。

    返回 True 表示本次完成注册；同一 registry 二次调用返回 False（不重复建收集器）。
    注册失败（重名等）只降级为 no-op，不影响投递主流程。
    """
    with _LOCK:
        if any(inst["registry"] is registry for inst in _INSTALLS):
            return False
        try:
            from prometheus_client import Counter, Gauge

            _inst = {
                "registry": registry,
                "failed": Counter(
                    FAILED_TOTAL_METRIC,
                    "Result comment post failures by enum reason (non_numeric_ident/"
                    "http_4xx/http_5xx/network/unknown).",
                    ["reason"],
                    registry=registry,
                ),
                "skipped": Counter(
                    SKIPPED_TOTAL_METRIC,
                    "Result comment posts skipped before any VCS request by enum reason.",
                    ["reason"],
                    registry=registry,
                ),
                "consecutive": Gauge(
                    CONSECUTIVE_METRIC,
                    "Consecutive result comment post failures per repo (reset on success).",
                    ["repo_key"],
                    registry=registry,
                ),
            }
        except ValueError:  # pragma: no cover - 同一 registry 重复注册
            return False
        _INSTALLS.append(_inst)
        return True


def classify_failure(detail) -> str:
    """把投递异常/响应文本归到枚举 reason（未知一律 unknown，不丢数、不涨基数）。"""
    text = "" if detail is None else str(detail)
    m = _STATUS_RE.search(text)
    if m:
        code = int(m.group(1))
        if 400 <= code < 500:
            return "http_4xx"
        if 500 <= code < 600:
            return "http_5xx"
    low = text.lower()
    if any(h in low for h in _NETWORK_HINTS):
        return "network"
    return "unknown"


def _norm_reason(reason) -> str:
    r = (reason or "").strip().lower()
    return r if r in FAIL_REASONS else "unknown"


def alert_threshold() -> int:
    """连续失败告警阈值（环境变量可配，缺省 5，夹到 1..1000）。"""
    raw = os.environ.get(ALERT_THRESHOLD_ENV)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_ALERT_THRESHOLD
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_ALERT_THRESHOLD
    return max(1, min(_MAX_THRESHOLD, v))


def _streak_key(repo_key: str) -> str:
    return str(repo_key or "").strip() or "unknown"


def observe_failure(reason, repo_key: str = "", threshold: int | None = None) -> dict:
    """记录一次投递失败，返回 {reason, consecutive, threshold, alert}。

    连续计数按 repo_key 维度累加；达阈值时 alert=True，调用方据此打结构化告警行。
    """
    r = _norm_reason(reason)
    key = _streak_key(repo_key)
    thr = alert_threshold() if threshold is None else max(1, int(threshold))
    with _LOCK:
        for inst in _INSTALLS:
            inst["failed"].labels(reason=r).inc()
        n = _STREAKS.get(key, 0) + 1
        _STREAKS[key] = n
        for inst in _INSTALLS:
            inst["consecutive"].labels(repo_key=key).set(n)
    return {"reason": r, "consecutive": n, "threshold": thr, "alert": n >= thr}


def observe_delivered(repo_key: str = "") -> None:
    """一次成功投递 → 该仓库连续失败计数清零（Gauge 归零）。"""
    key = _streak_key(repo_key)
    with _LOCK:
        _STREAKS[key] = 0
        for inst in _INSTALLS:
            inst["consecutive"].labels(repo_key=key).set(0)


def observe_skipped(reason, repo_key: str = "") -> str:
    """记录一次「发请求前就跳过」（非数字 ident 等），返回归一后的 reason。"""
    r = _norm_reason(reason)
    with _LOCK:
        for inst in _INSTALLS:
            inst["skipped"].labels(reason=r).inc()
    return r


def consecutive_failures(repo_key: str = "") -> int:
    """当前连续失败次数（0 = 上次投递成功或从未失败）。"""
    with _LOCK:
        return _STREAKS.get(_streak_key(repo_key), 0)


def reset_streaks() -> None:
    """清空连续失败计数（测试与运维复位用）。"""
    with _LOCK:
        _STREAKS.clear()
        for inst in _INSTALLS:
            try:
                inst["consecutive"].clear()
            except Exception as _e:  # pragma: no cover - prometheus 版本差异
                # 清标签失败不影响计数语义（Gauge 只是观测面），显式记一行代替静默吞
                from src.kernel.config import log as _log
                _log.debug(f"comment_metrics.reset_streaks: gauge clear skipped: {_e}")