"""后台循环健康（可观测性自身的可观测性）。

审计（2026-10-17）发现：告警求值 / 指标聚合 / 设备采样 / 日志清理这些循环**失败时只打一条
日志**，连续失败时用户面与管理台都看不到——观测系统自己瞎了。本模块给每个循环记
「跑过多少次 / 失败多少次 / 连续失败几次 / 上次成功时间 / 上次错误」，三个出口：

1. `/metrics`（Prometheus，`METRICS_ADDR` 启用时）：`grape_obs_loop_*` 四个指标；
2. `GET /api/admin/obs/loops`（L2，管理台展示）；
3. 调用方可据此自己决定要不要告警（当前不自动建规则，见审计 §七.6）。

设计取舍：**纯进程内状态**（`_STATE`），不落库。理由：循环的"是否在跑"本来就是进程级事实，
落库反而引入"循环挂了 → 没人更新健康表 → 表看着还很健康"的悖论。进程重启后计数归零是预期行为
（Prometheus 侧用 `_total` 计数器 + 重启后重置属正常语义）。

用法（每个循环体统一两行）：

    from src.observability import loops
    ...
    try:
        do_work()
        loops.beat("obs_alert")
    except Exception as e:
        loops.fail("obs_alert", e)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# 已知循环名（供 snapshot 排序/展示用；未知名字也接受，只是排在后面）
KNOWN = ("obs_alert", "obs_gap", "obs_rollup", "obs_sample", "obs_log_cleanup",
         "obs_alert_retention", "obs_usage_archive")

_STATE: Dict[str, Dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _st(name: str) -> Dict[str, Any]:
    st = _STATE.get(name)
    if st is None:
        st = {"loop": name, "runs": 0, "failures": 0, "consecutive_failures": 0,
              "last_success_at": "", "last_error": "", "last_error_at": ""}
        _STATE[name] = st
    return st


# ---------------------------------------------------------------- Prometheus
# 独立 Registry 在 src.observability 里，这里只挂自己的一组指标（未启用 METRICS_ADDR 时
# 也照常存在，只是没有 exporter 去抓 → 零额外开销）。
_PROM: Optional[Dict[str, Any]] = None


def _prom():
    global _PROM
    if _PROM is None:
        try:
            from prometheus_client import Counter, Gauge
            from src.observability import registry
            _PROM = {
                "runs": Counter("grape_obs_loop_runs_total", "后台循环执行次数", ["loop"], registry=registry),
                "failures": Counter("grape_obs_loop_failures_total", "后台循环失败次数", ["loop"], registry=registry),
                "consecutive": Gauge("grape_obs_loop_consecutive_failures",
                                     "后台循环连续失败次数（0=健康）", ["loop"], registry=registry),
                "last_ok": Gauge("grape_obs_loop_last_success_unixtime",
                                 "后台循环上次成功的时间戳（秒）", ["loop"], registry=registry),
            }
        except Exception as e:            # prometheus 不可用不影响循环本身
            log.debug(f"obs loops: prometheus wiring skipped: {e}")
            _PROM = {}
    return _PROM


def beat(name: str) -> None:
    """循环成功跑完一次。"""
    st = _st(name)
    st["runs"] += 1
    st["consecutive_failures"] = 0
    st["last_success_at"] = _now_iso()
    p = _prom()
    if p:
        try:
            p["runs"].labels(loop=name).inc()
            p["consecutive"].labels(loop=name).set(0)
            p["last_ok"].labels(loop=name).set(datetime.now(timezone.utc).timestamp())
        except Exception:
            pass


def fail(name: str, err: Any = "") -> None:
    """循环失败一次。`err` 只取前 300 字符（避免把整段栈塞进快照）。"""
    st = _st(name)
    st["failures"] += 1
    st["consecutive_failures"] += 1
    st["last_error"] = str(err)[:300]
    st["last_error_at"] = _now_iso()
    p = _prom()
    if p:
        try:
            p["failures"].labels(loop=name).inc()
            p["consecutive"].labels(loop=name).set(st["consecutive_failures"])
        except Exception:
            pass


def snapshot() -> List[Dict[str, Any]]:
    """给 L2/管理台的快照（含"多久没成功"的秒数，方便前端直接显示）。"""
    now = datetime.now(timezone.utc)
    order = {n: i for i, n in enumerate(KNOWN)}
    out: List[Dict[str, Any]] = []
    for name in sorted(_STATE.keys(), key=lambda n: (order.get(n, 99), n)):
        st = dict(_STATE[name])
        stale = None
        if st["last_success_at"]:
            try:
                stale = round((now - datetime.fromisoformat(st["last_success_at"])).total_seconds(), 1)
            except Exception:
                stale = None
        st["seconds_since_success"] = stale
        st["healthy"] = (st["consecutive_failures"] == 0)
        out.append(st)
    return out


def reset() -> None:
    """测试用：清空状态。"""
    _STATE.clear()