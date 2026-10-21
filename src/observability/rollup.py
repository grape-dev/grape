"""可观测性 · 指标留存（设备资源历史，`obs_metric_rollup` 的第一步）。

设计：docs/weekly-07/observability-redesign-design.md §6（数据层：分钟聚合表）
       实施路线 P0「指标聚合与留存」+「设备分区迁移：现有内容迁入 + 资源曲线」

为什么要有这张表：`runtime_credentials` 只存**当前值**（worker 心跳覆盖写），
所以设备分区只能显示瞬时 CPU/内存，看不到趋势 —— 本模块按分钟留一份采样，
供「资源曲线」使用。留存窗口 `RETENTION_DAYS`，由采样线程顺带清理。

只存**业务可见维度**（设备资源 + 健康分），不存平台基础设施指标（DB 池等）——
后者属 L2（admin 健康中心），见 §5.3。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.config import log

RETENTION_DAYS = 7
SAMPLE_INTERVAL_SEC = 60

RANGE_SPECS = {     # range → (窗口秒数, 分桶秒数)
    "1h":  (3600, 60),
    "6h":  (6 * 3600, 300),
    "24h": (24 * 3600, 900),
    "7d":  (7 * 86400, 3600),
}
DEFAULT_RANGE = "6h"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(raw: Any) -> Optional[datetime]:
    from src.api.time_utils import normalize_event_time
    s = normalize_event_time(raw)
    if not s or not isinstance(s, str):
        return None
    try:
        d = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


#: 判定"在线"的心跳新鲜度窗口（秒）：采样时刻距 last_seen_at 在窗口内才算在线（P2-1 SLO）
ONLINE_WINDOW_SEC = 900


def snapshot_runtimes() -> Dict[str, int]:
    """采样一轮：把每个未吊销引擎的当前资源值写一行历史 + 清理过期数据。"""
    from src.session_manager import db, db_lock
    now = _now()
    now_iso = now.isoformat()
    inserted = 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT rc.id, rc.device_name, rc.cpu_percent, rc.memory_percent, rc.disk_percent,"
                      " rc.bandwidth_percent, COALESCE(hs.health_score, 0), rc.status, rc.last_seen_at"
                      " FROM runtime_credentials rc"
                      " LEFT JOIN runtime_health_scores hs ON hs.runtime_id = rc.id"
                      " WHERE rc.status != 'revoked'")
            rows = c.fetchall()
            for rid, device, cpu, mem, disk, bw, health, status, last_seen in rows:
                try:
                    # 在线信号（SLO 可用率）：采样时刻心跳是否新鲜。时间口径按 UTC 解析，
                    # 解析不出/过期 → 0（"没证据在线"就不算在线，不自造 1）。
                    online = 0
                    try:
                        from src.api.time_utils import normalize_event_time
                        _ls = normalize_event_time(last_seen)
                        _d = datetime.fromisoformat(str(_ls).replace("Z", "+00:00")) if _ls else None
                        if _d is not None:
                            if _d.tzinfo is None:
                                _d = _d.replace(tzinfo=timezone.utc)
                            online = 1 if (now - _d).total_seconds() <= ONLINE_WINDOW_SEC else 0
                    except Exception:
                        online = 0
                    c.execute(
                        "INSERT INTO obs_runtime_samples (runtime_id, device_name, sampled_at,"
                        " cpu_percent, memory_percent, disk_percent, bandwidth_percent,"
                        " health_score, status, online) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (int(rid), device or "", now_iso,
                         float(cpu or 0), float(mem or 0), float(disk or 0),
                         float(bw or 0), int(health or 0), status or "", int(online)))
                    inserted += 1
                except Exception:
                    pass
            # 清理过期采样（顺手做，避免表无限增长）
            try:
                cutoff = (now - timedelta(days=RETENTION_DAYS)).isoformat()
                c.execute("DELETE FROM obs_runtime_samples WHERE sampled_at < ?", (cutoff,))
            except Exception:
                pass
            db.commit()
    except Exception as e:
        log.warning(f"obs snapshot_runtimes failed: {e}")
    return {"inserted": inserted}


def runtime_history(runtime_ids: List[int], range_str: str = DEFAULT_RANGE) -> Dict[str, Any]:
    """按分钟桶返回资源曲线（CPU / 内存 / 健康分）。

    分桶在 Python 做（同 overview：库内时间有裸 CST 与 UTC ISO 两态，SQL 日期函数跨方言不通用）。
    """
    rng = range_str if range_str in RANGE_SPECS else DEFAULT_RANGE
    span, bucket = RANGE_SPECS[rng]
    now = _now()
    start = now - timedelta(seconds=span)
    nbuckets = max(1, span // bucket)
    buckets = [start + timedelta(seconds=i * bucket) for i in range(nbuckets)]
    bx = {b.isoformat(): i for i, b in enumerate(buckets)}

    series: Dict[str, Dict[str, List[Optional[float]]]] = {}
    for rid in runtime_ids:
        series[str(rid)] = {"cpu": [None] * nbuckets, "mem": [None] * nbuckets,
                            "disk": [None] * nbuckets, "health": [None] * nbuckets}
    if not runtime_ids:
        return {"range": rng, "bucket_seconds": bucket, "buckets": [b.isoformat() for b in buckets],
                "series": series}
    # 桶内累加求平均（health 取桶内均值，避免尖刺）
    acc: Dict[str, Dict[int, Dict[str, float]]] = {}
    ph = ",".join("?" * len(runtime_ids))
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT runtime_id, sampled_at, cpu_percent, memory_percent, disk_percent,"
                      f" health_score FROM obs_runtime_samples WHERE runtime_id IN ({ph})",
                      tuple(int(x) for x in runtime_ids))
            for rid, sampled_at, cpu, mem, disk, health in c.fetchall():
                sdt = _dt(sampled_at)
                if not sdt or sdt < start or sdt > now:
                    continue
                i = bx.get((start + timedelta(seconds=(int((sdt - start).total_seconds()) // bucket) * bucket)).isoformat())
                if i is None:
                    continue
                key = str(rid)
                slot = acc.setdefault(key, {}).setdefault(i, {"cpu": 0.0, "mem": 0.0, "disk": 0.0,
                                                              "health": 0.0, "n": 0})
                slot["cpu"] += float(cpu or 0)
                slot["mem"] += float(mem or 0)
                slot["disk"] += float(disk or 0)
                slot["health"] += float(health or 0)
                slot["n"] += 1
    except Exception as e:
        log.warning(f"obs runtime_history failed: {e}")

    for key, slots in acc.items():
        for i, slot in slots.items():
            n = slot["n"] or 1
            s = series.get(key)
            if not s:
                continue
            s["cpu"][i] = round(slot["cpu"] / n, 1)
            s["mem"][i] = round(slot["mem"] / n, 1)
            s["disk"][i] = round(slot["disk"] / n, 1)
            s["health"][i] = round(slot["health"] / n, 1)
    return {"range": rng, "bucket_seconds": bucket, "buckets": [b.isoformat() for b in buckets],
            "series": series}