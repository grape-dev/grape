"""GSB 自检循环：周期性跨域读一次，验证总线可用并给流量看板提供稳定信号。

只在 ``GRAPE_PROVIDER_MODE=remote`` 时启用（Local 模式下没有跨域调用，探它没意义）。
全部是只读调用，异常吞掉只告警，不影响业务；单次最坏耗时 ~10s（客户端超时）。
"""
from __future__ import annotations

import asyncio
import os

from src.kernel.config import log
from src.kernel.gsb.provider import get_service, is_remote_mode

SELFTEST_INTERVAL = int(os.environ.get("GRAPE_GSB_SELFTEST_INTERVAL", "300"))

# 服务名 → (方法名, 说明)。只挑无参只读方法，避免影响业务数据。
_PROBES = (
    ("runtime", "get_runtimes"),
    ("insight", "aggregate_runtime_stats"),
)


def selftest_enabled() -> bool:
    """remote 模式 + 未显式关闭时启用。"""
    if os.environ.get("GRAPE_GSB_SELFTEST", "").strip().lower() in ("0", "false", "off"):
        return False
    return is_remote_mode()


def run_selftest_once() -> dict:
    """同步执行一轮自检，返回 {service: "ok" | 错误类型}。"""
    result: dict[str, str] = {}
    for service, method in _PROBES:
        try:
            svc = get_service(service)
            getattr(svc, method)()
            result[service] = "ok"
        except Exception as exc:  # noqa: BLE001 - 自检失败不能拖垮进程
            result[service] = type(exc).__name__
            log.warning(f"GSB selftest failed: service={service} method={method} err={type(exc).__name__}: {exc}")
    return result


async def gsb_selftest_loop(interval: int | None = None) -> None:
    """后台循环：每 interval 秒跑一次自检（默认 300s）。"""
    if not selftest_enabled():
        return
    wait = interval or SELFTEST_INTERVAL
    log.info(f"GSB selftest loop started (interval={wait}s, mode=remote)")
    while True:
        try:
            result = await asyncio.to_thread(run_selftest_once)
            if all(v == "ok" for v in result.values()):
                log.info(f"GSB selftest ok: {result}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(f"GSB selftest loop error: {type(exc).__name__}: {exc}")
        await asyncio.sleep(wait)