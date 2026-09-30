"""健康态派生（移植 multica derive-health，见 agent-runtime-design.md §7.5 / concurrency 无需）。

规则：status=online 且心跳新鲜(< STALE_SECONDS，默认 100s) → online；status=online 但心跳超时 → recently_lost（失联）；
无 last_seen → offline；距 GC（7d）<1d → about_to_gc；其余 offline。

修复记录：原实现 status='online' 直接返回 online，不校验 last_seen 新鲜度 ——
worker 宕机后引擎永久显示"在线"，界面/调度展示误导（2026-08-12 修复）。
Issue #259：新鲜度阈值与 engine_select._RUNTIME_STALE_SECONDS 对齐（env RUNTIME_STALE_SECONDS，默认 100s），
原 5min 写死导致展示层与调度层判定不一致。
"""
import os
from datetime import datetime, timezone

_STALE_SECONDS = int(os.environ.get("RUNTIME_STALE_SECONDS", "100"))


def parse_dt(s: str) -> float | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def derive_runtime_health(status: str, last_seen_at: str | None, now: float | None = None) -> str:
    _now = now if now is not None else datetime.now(timezone.utc).timestamp()
    ts = parse_dt(last_seen_at)
    # "active" 是 runtime_credentials 表的状态（等价于 online）
    if status in ("online", "active"):
        if ts is None:
            return "recently_lost"      # 标 online 但无心跳记录 → 视为失联
        if _now - ts < _STALE_SECONDS:
            return "online"             # 心跳新鲜 → 真在线
        return "recently_lost"          # 心跳超时 → 失联（原实现直接返回 online 的缺陷）
    if ts is None:
        return "offline"
    offline_for = _now - ts
    if offline_for < _STALE_SECONDS:
        return "recently_lost"
    if offline_for > 6 * 24 * 3600:
        return "about_to_gc"
    return "offline"
