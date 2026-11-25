"""Issue #4349：v2 engine_uid 失效（stale）观测治理——限频 ERROR、指标与报告注册表。

背景（Issue #4349 实测）：引擎内存持有的 engine_uid 失效后（重新注册新建行、
uid 回填窗口等），v2 claim `_resolve_runtime_by_uid` 与 `runtime_heartbeat_v2`
逐请求 WARNING 刷屏（vod-dev-002 三引擎 1 小时 2,733 条，占该小时 worker
WARNING 的 36%）；且「stale-only 且无 runtime_ids 回退」的心跳被静默丢弃，
SRE 只能靠日志行数发现。

三条治理（Spec 见 Issue #4349 评论 203688397）：
1. **限频 ERROR**：每个 (uid, credential_id) 在日志窗口
   （`GRAPE_STALE_UID_LOG_WINDOW`，默认 3600s）内只输出一条 ERROR；
   窗口内后续命中只计数不输出（首现可见，Loki 1h 窗口口径收敛为一次性 ERROR）。
2. **指标**（挂 insight prometheus registry，与 runtime.py `_relay_total`
   同模式，注册失败降级 None 不影响请求路径）：
   - `grape_runtime_v2_stale_uid_total{source=resolve|heartbeat}` 按次累加
     （不受日志限频影响，观测口径完整）；
   - `grape_runtime_v2_heartbeat_uid_lost_total` 在「stale_uids 非空且无
     runtime_ids 回退」的心跳丢失分支累加，并按凭据限频输出 ERROR。
3. **报告注册表**：进程内 TTL 表（`GRAPE_STALE_UID_RETENTION`，默认 86400s），
   `/settings/agent-runtimes`（列表+详情共用 get_agent_runtimes）经
   `reports_for_credential()` 读它，展示「DB engine_uid vs 引擎上报 uid」
   比对结果；claim/heartbeat 同请求携带的 runtime_ids 会尽力关联到报告上
   （关联到本行的报告 → 该引擎行标 stale，否则 matched）。

拓扑注记：注册表为进程内状态（与 runtime.py `_relay_warn_ts` 等既有缓存同
模式）；runtime 域 lib 当前为「进程内 import」（见 runtime/public 模块头），
拆分三节点部署时需迁 DB 持久化（届时随迁移补，不在本 Issue 范围）。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Iterable

from src.kernel.config import log

_LOCK = threading.Lock()
# (uid_lower, credential_id) -> 报告记录（count/first_seen/last_seen/last_error_at/sources/runtime_ids）
_REPORTS: dict[tuple[str, int], dict[str, Any]] = {}
# credential_id -> 心跳丢失分支最近一次 ERROR 时间戳（限频用）
_LOST_LOG_AT: dict[int, float] = {}
_LOST_TOTAL = 0
_MAX_REPORTS = 10000


def log_window_s() -> float:
    """stale uid 日志限频窗口（秒）；非法/非正值回退默认 3600。"""
    try:
        v = float(os.environ.get("GRAPE_STALE_UID_LOG_WINDOW", "3600"))
        return v if v > 0 else 3600.0
    except (TypeError, ValueError):
        return 3600.0


def retention_s() -> float:
    """报告注册表 TTL（秒）；非法/非正值回退默认 86400。"""
    try:
        v = float(os.environ.get("GRAPE_STALE_UID_RETENTION", "86400"))
        return v if v > 0 else 86400.0
    except (TypeError, ValueError):
        return 86400.0


# prometheus 计数器（注册失败降级 None，不阻断请求路径；同名 Counter 重复
# 注册抛 ValueError 的历史教训见 runtime.py `_relay_total` 注释——本模块是唯一
# 定义点，tests 直接 import 本模块，不经第二处注册）。
_stale_uid_total = None
_heartbeat_lost_total = None
try:  # pragma: no cover - 主控环境必有 prometheus_client，降级分支难在单测覆盖
    from prometheus_client import Counter as _PC
    from src.domains.insight import public as _obs_public
    _stale_uid_total = _PC(
        "grape_runtime_v2_stale_uid_total",
        "v2 stale engine_uid occurrences by source (claim resolve / heartbeat)",
        ["source"], registry=_obs_public.prometheus_registry())
except Exception as _e:  # noqa: BLE001
    _stale_uid_total = None
    log.warning(f"stale uid counter unavailable: {type(_e).__name__}: {_e}")
try:  # pragma: no cover
    from prometheus_client import Counter as _PC2
    from src.domains.insight import public as _obs_public2
    _heartbeat_lost_total = _PC2(
        "grape_runtime_v2_heartbeat_uid_lost_total",
        "v2 heartbeats whose engine_uids were all stale with no runtime_ids fallback (dropped)",
        registry=_obs_public2.prometheus_registry())
except Exception as _e2:  # noqa: BLE001
    _heartbeat_lost_total = None
    log.warning(f"heartbeat lost counter unavailable: {type(_e2).__name__}: {_e2}")


def _prune_locked(now: float) -> None:
    """TTL 剪枝 + 容量上限（_LOCK 内调用）。"""
    ttl = retention_s()
    expired = [k for k, v in _REPORTS.items() if now - v["last_seen"] > ttl]
    for k in expired:
        del _REPORTS[k]
    for cid in [c for c, ts in _LOST_LOG_AT.items() if now - ts > ttl]:
        del _LOST_LOG_AT[cid]
    if len(_REPORTS) > _MAX_REPORTS:
        oldest = sorted(_REPORTS.items(), key=lambda kv: kv[1]["last_seen"])
        for k, _ in oldest[:len(_REPORTS) - _MAX_REPORTS]:
            del _REPORTS[k]


def record_stale(uid: str, credential_id: int, source: str,
                 runtime_ids: Iterable[int] = ()) -> bool:
    """记录一次 stale uid 命中：报告表 + 计数指标。

    返回本 (uid, credential_id) 日志窗口内是否为「首条」（调用方应输出 ERROR）。
    计数器按次累加，不受日志限频影响（限频只作用于日志行数，不动观测口径）。
    """
    key = (str(uid or "").strip().lower(), int(credential_id))
    now = time.time()
    with _LOCK:
        rec = _REPORTS.get(key)
        if rec is None:
            rec = {"uid": key[0], "credential_id": key[1], "count": 0,
                   "first_seen": now, "last_seen": now, "last_error_at": 0.0,
                   "sources": set(), "runtime_ids": set()}
            _REPORTS[key] = rec
        rec["count"] += 1
        rec["last_seen"] = now
        rec["sources"].add(str(source))
        for rid in (runtime_ids or ()):
            try:
                rid_i = int(rid)
            except (TypeError, ValueError):
                continue
            if rid_i > 0:
                rec["runtime_ids"].add(rid_i)
        _prune_locked(now)
        first_in_window = (now - rec["last_error_at"]) >= log_window_s()
        if first_in_window:
            rec["last_error_at"] = now
    if _stale_uid_total is not None:
        try:
            _stale_uid_total.labels(source=str(source)).inc()
        except Exception:  # noqa: BLE001
            pass
    return first_in_window


def record_and_log(uid: str, credential_id: int, source: str, message: str,
                   runtime_ids: Iterable[int] = ()) -> None:
    """记录 + 窗口内首条 ERROR（Issue #4349：每请求 WARNING → 一次性 ERROR + 限频）。"""
    if record_stale(uid, credential_id, source, runtime_ids):
        log.error(message)


def record_heartbeat_lost(credential_id: int, message: str) -> None:
    """心跳丢失分支（stale_uids 非空且无 runtime_ids 回退）：计数 + 按凭据限频 ERROR。

    Issue #4349 前该分支只在响应里回 engine_uids_stale 计数，引擎状态被静默
    丢弃、无任何指标/日志，SRE 无法发现「心跳丢失」。
    """
    global _LOST_TOTAL
    now = time.time()
    with _LOCK:
        _LOST_TOTAL += 1
        last = _LOST_LOG_AT.get(int(credential_id), 0.0)
        first_in_window = (now - last) >= log_window_s()
        if first_in_window:
            _LOST_LOG_AT[int(credential_id)] = now
        _prune_locked(now)
    if _heartbeat_lost_total is not None:
        try:
            _heartbeat_lost_total.inc()
        except Exception:  # noqa: BLE001
            pass
    if first_in_window:
        log.error(message)


def heartbeat_lost_count() -> int:
    """心跳丢失累计（测试/自检口径；prometheus 不可用时也能读）。"""
    with _LOCK:
        return _LOST_TOTAL


def reports_for_credential(credential_id: int, max_entries: int = 50) -> list[dict]:
    """该凭据下的 stale uid 报告快照（TTL 内，按 last_seen 倒序，JSON 友好类型）。"""
    now = time.time()
    out: list[dict] = []
    with _LOCK:
        _prune_locked(now)
        for (_uid, cid), rec in _REPORTS.items():
            if cid != int(credential_id):
                continue
            out.append({"uid": _uid, "credential_id": cid, "count": rec["count"],
                        "first_seen": rec["first_seen"], "last_seen": rec["last_seen"],
                        "sources": sorted(rec["sources"]),
                        "runtime_ids": sorted(rec["runtime_ids"])})
    out.sort(key=lambda r: r["last_seen"], reverse=True)
    return out[:max_entries]


def clear() -> None:
    """清空注册表与计数（单测隔离用；生产进程内状态随 TTL 自然剪枝）。"""
    global _LOST_TOTAL
    with _LOCK:
        _REPORTS.clear()
        _LOST_LOG_AT.clear()
        _LOST_TOTAL = 0
