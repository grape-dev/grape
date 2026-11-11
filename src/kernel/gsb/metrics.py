"""GSB 客户端侧指标（跨域调用流向）。

分层约束：kernel 不能 import domains（check-arch R1 HARD），而 Prometheus 的
CollectorRegistry 由 insight 域持有。所以这里只定义「可安装的收集器 + 无副作用的
observe 入口」，由 insight 的 metrics server 启动时调用 ``install(registry)`` 注入。

安装前 observe() 是 no-op（METRICS_ADDR 未设时零开销）。
"""
from __future__ import annotations

from typing import Any

_CLIENT_REQUESTS: Any = None
_CLIENT_DURATION: Any = None

_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)


def install(registry: Any) -> None:
    """把 GSB 客户端指标注册到给定的 prometheus_client CollectorRegistry。

    幂等：重复调用不会重复注册（重复注册会抛 ValueError，这里直接忽略）。
    """
    global _CLIENT_REQUESTS, _CLIENT_DURATION
    if _CLIENT_REQUESTS is not None:
        return
    from prometheus_client import Counter, Histogram

    try:
        _CLIENT_REQUESTS = Counter(
            "grape_gsb_client_requests_total",
            "Cross-domain calls issued through the GSB client.",
            ["target", "caller", "method", "status", "instance"],
            registry=registry,
        )
        _CLIENT_DURATION = Histogram(
            "grape_gsb_client_duration_seconds",
            "GSB client call duration.",
            ["target", "method", "status", "instance"],
            registry=registry,
            buckets=_BUCKETS,
        )
    except ValueError:  # pragma: no cover - 同一 registry 重复注册
        pass


def observe(
    target: str,
    caller: str,
    method: str,
    status: str,
    seconds: float,
    instance: str = "",
) -> None:
    """记录一次跨域调用（未安装指标时为 no-op）。

    ``instance`` 是本次命中的实例（host:port）。多实例负载均衡后，
    没有这个维度就看不出发往哪个实例、各实例的错误率与延迟差异。
    """
    if _CLIENT_REQUESTS is None or _CLIENT_DURATION is None:
        return
    _CLIENT_REQUESTS.labels(
        target=target, caller=caller, method=method, status=str(status), instance=instance
    ).inc()
    _CLIENT_DURATION.labels(
        target=target, method=method, status=str(status), instance=instance
    ).observe(seconds)