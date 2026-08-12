"""健康态派生（移植 multica derive-health，见 agent-runtime-design.md §7.5 / concurrency 无需）。

规则：status=online → online；无 last_seen → offline；
失联 <5min → recently_lost；距 GC（7d）<1d → about_to_gc；其余 offline。
"""
from datetime import datetime, timezone


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
    if status == "online":
        return "online"
    ts = parse_dt(last_seen_at)
    if ts is None:
        return "offline"
    _now = now if now is not None else datetime.now(timezone.utc).timestamp()
    offline_for = _now - ts
    if offline_for < 5 * 60:
        return "recently_lost"
    if offline_for > 6 * 24 * 3600:
        return "about_to_gc"
    return "offline"
