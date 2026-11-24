"""启动期降级指标（可安装收集器）。

为什么要这一层：启动期"降级"原先只有一个落点——WARNING 日志。运维要发现「这次启动
降级了」只能 grep 关键词，且无法区分两类完全不同的现象（Issue #3848）：

- **预期时序**：装配层（``src/api/contracts_wiring.py``）还没 ``bind()`` 端口，依赖端口的
  启动期工作本就该推迟（例如卡片源读 ``repo_platforms`` 需要 pipeline 端口）——
  这不是故障，告警会掩盖真故障；
- **真故障**：端口已在、库/契约读不了（表缺失、DSN 错、通道坏）。

两类都要**可打点**，但只有后者必须继续响亮。因此本模块提供一个带
``component`` / ``reason`` 两个维度的计数器，由日志之外的第二条巡检信号承载。

分层约束（同 ``src/kernel/gsb/metrics.py`` / ``src/kernel/org_metrics.py``）：kernel 不能
import domains（check-arch R1 HARD），而 Prometheus 的 ``CollectorRegistry`` 由 insight 域
持有。所以这里只定义「可安装的收集器 + 无副作用的 record 入口」，由 insight 的 metrics
server 启动时调用 ``install(registry)`` 注入。安装前 ``record_startup_degraded()`` 是
no-op（未启用 METRICS_ADDR / worker 包不带 prometheus_client 时零开销）。
"""
from __future__ import annotations

from typing import Any

_DEGRADED: Any = None


def install(registry: Any) -> None:
    """把 startup_degraded 计数器注册到给定 prometheus_client CollectorRegistry。

    幂等：重复调用不会重复注册（重复注册会抛 ValueError，这里直接忽略）。
    """
    global _DEGRADED
    if _DEGRADED is not None:
        return
    try:
        from prometheus_client import Counter
    except Exception:  # pragma: no cover - worker 包无 prometheus_client
        return
    try:
        _DEGRADED = Counter(
            "grape_startup_degraded",
            "Startup-phase degradations, split by expected ordering and real failures.",
            ["component", "reason"],
            registry=registry,
        )
    except ValueError:  # pragma: no cover - 同一 registry 重复注册
        pass


def record_startup_degraded(component: str, reason: str) -> None:
    """记录一次启动期降级（未安装指标时为 no-op，绝不抛）。

    Args:
        component: 降级发生的位置，如 ``"card_source"`` / ``"credential_usage_log"``。
        reason: 降级原因，如 ``"port_not_bound"``（预期时序）/ ``"read_failed"``（真故障）。
    """
    if _DEGRADED is None:
        return
    try:
        _DEGRADED.labels(component=str(component or ""),
                         reason=str(reason or "")).inc()
    except Exception:  # pragma: no cover - 指标不得影响主流程
        pass


def _reset_for_tests() -> None:
    """测试复位入口：让 install() 能把计数器重新装进一个新的 registry。"""
    global _DEGRADED
    _DEGRADED = None