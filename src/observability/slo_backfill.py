# -*- coding: utf-8 -*-
"""Issue #544 L1：SLO 可用率缺失小时的**后台**限流回填队列。

背景（断链根因）：`slo._availability_buckets` 在 API 请求路径上对 rollup 缺失的闭合小时
做同步 raw-scan 回填（扫 `obs_runtime_samples`），持全局 `db_lock` 可达数十秒 →
`/api/a2a` 等所有走库请求被卡死（客户端 20s 读超时放弃 → 目标事件未创建 → 断链）。

两类慢性缺失源：
1. 停机/升级窗口：`obs_rollup` 60s 循环不在 → 小时桶整段缺失（该循环重启后本会补，
   但在补齐前每次 SLO 评估都重复全窗扫描）；
2. **零样本小时**：`metric_rollup.compute_bucket` 只在有采样时写 `engine_avail_*` 行，
   而覆盖判定按"t>0 的行"——无采样小时永远算"缺失"，每次评估都白扫一遍空段。

本模块把回填从请求进程剥离：
- `schedule(start, end)`：合并去重入队（请求路径只做这一步，O(1) 不扫库）；
- 单 daemon worker + 最小间隔限流（`GRAPE_SLO_BACKFILL_MIN_INTERVAL`，默认 5s）；
- worker 对窗口内缺 `engine_avail_total` 行的闭合小时：连续段合并、按段扫原始表聚合，
  经 `metric_rollup.write_bucket` 写 `engine_avail_online/total`；
  **零样本小时写 total=0 行**（行存在 = 已覆盖，消除第 2 类慢性缺失）；
- 健康登记进 `observability.loops`（`obs_slo_backfill`），管理台可见。

设计取舍：进程内队列（最小改动消除卡死，保留演进为独立 worker 的空间）；任务不持久化，
进程重启丢队列时退化为"下次评估再次 schedule"，不会更差。
"""
from __future__ import annotations

import os
import queue
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from src.config import log

#: 两次回填作业之间的最小间隔（秒）：限流，避免回填自身成为 db_lock 大户
_MIN_INTERVAL_DEFAULT = 5.0
#: 单次作业最多处理的小时数（与 obs_rollup catch_up 的 max_hours 同量级，防长事务）
_MAX_HOURS_PER_JOB = 48

_QUEUE: "queue.Queue[Optional[Tuple[datetime, datetime, int]]]" = queue.Queue()
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
_LAST_RUN = {"at": 0.0, "count": 0}
#: 代际计数：`reset()`（测试）后旧代任务直接丢弃，防止跨用例窗口串扰
_GEN = {"n": 0}

LOOP_NAME = "obs_slo_backfill"


def _min_interval() -> float:
    try:
        return float(os.environ.get("GRAPE_SLO_BACKFILL_MIN_INTERVAL",
                                    str(_MIN_INTERVAL_DEFAULT)) or _MIN_INTERVAL_DEFAULT)
    except Exception:
        return _MIN_INTERVAL_DEFAULT


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _dt(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def reset() -> None:
    """测试辅助：清空队列与限流状态（不动已写入的 rollup 行）；旧代任务作废。

    同时置 `_WORKER_STARTED=True` 阻止 worker 线程启动——测试用 `drain_for_test()`
    同步消化队列，避免 worker 并发写库造成用例间竞态。
    """
    global _WORKER_STARTED
    _WORKER_STARTED = True
    _GEN["n"] += 1
    while True:
        try:
            _QUEUE.get_nowait()
        except Exception:
            break
    _LAST_RUN["at"] = 0.0
    _LAST_RUN["count"] = 0


def scheduled() -> int:
    return _QUEUE.qsize()


def schedule(start: datetime, end: datetime) -> bool:
    """请求路径调用：把缺失窗口交给后台回填（不扫库、不等待）。

    窗口去重靠 worker 侧覆盖查询（缺行才算缺），这里不做窗口合并的精确去重，
    只丢弃明显空的窗口。返回 True 表示已入队。
    """
    if not start or not end or start >= end:
        return False
    _ensure_worker()
    try:
        _QUEUE.put((start, end, _GEN["n"]))
        return True
    except Exception as e:
        log.warning(f"slo backfill: enqueue failed: {e}")
        return False


def _ensure_worker() -> None:
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        t = threading.Thread(target=_worker_main, name="obs-slo-backfill", daemon=True)
        t.start()
        _WORKER_STARTED = True
        log.info("slo backfill: background worker started (Issue #544 L1)")


def _worker_main() -> None:
    import time
    while True:
        try:
            item = _QUEUE.get(timeout=3600)
        except Exception:
            continue
        if item is None:
            continue
        start, end, gen = item
        if gen != _GEN["n"]:
            continue  # reset() 后的旧代任务（测试窗口串扰防护）
        # 限流：与前一次作业保持最小间隔（首跑不限流）
        if _LAST_RUN["at"] > 0:
            wait = _min_interval() - (time.monotonic() - _LAST_RUN["at"])
            if wait > 0:
                time.sleep(wait)
        try:
            _run_job(start, end)
            _LAST_RUN["at"] = time.monotonic()
            _LAST_RUN["count"] += 1
            from src.observability import loops
            loops.beat(LOOP_NAME)
        except Exception as e:
            log.warning(f"slo backfill job failed: {e}")
            try:
                from src.observability import loops
                loops.fail(LOOP_NAME, e)
            except Exception:
                pass


def _covered_hour_keys(start: datetime, end: datetime) -> set:
    """窗口内已有 `engine_avail_total`（global, hour）行的桶（行存在=已覆盖，total=0 也算）。"""
    from src.session_manager import db, db_lock
    out: set = set()
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT DISTINCT bucket_start FROM obs_metric_rollup"
                " WHERE resolution='hour' AND metric='engine_avail_total' AND dim_key='global'"
                " AND bucket_start >= ? AND bucket_start < ?",
                (_iso(start), _iso(end)))
            out = {r[0] for r in c.fetchall()}
    except Exception as e:
        log.warning(f"slo backfill: covered query failed: {e}")
    return out


def _missing_hours(start: datetime, end: datetime) -> list:
    """窗口内缺失的闭合小时（从新到旧，与 catch_up 口径一致：优先补最近的）。"""
    covered = _covered_hour_keys(start, end)
    out: list = []
    cur = end.replace(minute=0, second=0, microsecond=0)
    floor_start = start.replace(minute=0, second=0, microsecond=0)
    if floor_start < start:
        floor_start += timedelta(hours=1)
    while cur > floor_start and len(out) < _MAX_HOURS_PER_JOB:
        cur -= timedelta(hours=1)
        if _iso(cur) not in covered:
            out.append(cur)
    return out


def _raw_hour_agg(bucket: datetime) -> Optional[Tuple[float, int]]:
    """该小时原始采样聚合（SUM(online), COUNT(*)）；无采样返回 None。"""
    from src.session_manager import db, db_lock
    s_iso, e_iso = _iso(bucket), _iso(bucket + timedelta(hours=1))
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT SUM(online), COUNT(*) FROM obs_runtime_samples"
                      " WHERE sampled_at >= ? AND sampled_at < ?", (s_iso, e_iso))
            row = c.fetchone()
        if row and int(row[1] or 0) > 0:
            return (float(row[0] or 0), int(row[1]))
    except Exception as e:
        log.warning(f"slo backfill: raw agg {bucket} failed: {e}")
    return None


def _run_job(start: datetime, end: datetime) -> int:
    """一次回填作业：缺失闭合小时逐个聚合写入（零样本写 total=0 覆盖行）。

    单次最多 `_MAX_HOURS_PER_JOB` 个（从新到旧）；打满时把更旧的剩余段重新入队，
    由后续限流间隔的作业继续补（最终全窗覆盖，且不产生长事务）。
    """
    from src.session_manager import db, db_lock
    hours = _missing_hours(start, end)
    written = 0
    for b in hours:
        agg = _raw_hour_agg(b)
        online, total = agg if agg else (0.0, 0)
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "INSERT INTO obs_metric_rollup (resolution, bucket_start, metric, dim_key,"
                    " repo_key, value, cnt, updated_at) VALUES ('hour', ?, 'engine_avail_online',"
                    " 'global', '', ?, ?, datetime('now'))"
                    " ON CONFLICT(resolution, bucket_start, metric, dim_key) DO UPDATE SET"
                    " value=excluded.value, cnt=excluded.cnt, updated_at=excluded.updated_at",
                    (_iso(b), round(online, 3), total))
                c.execute(
                    "INSERT INTO obs_metric_rollup (resolution, bucket_start, metric, dim_key,"
                    " repo_key, value, cnt, updated_at) VALUES ('hour', ?, 'engine_avail_total',"
                    " 'global', '', ?, ?, datetime('now'))"
                    " ON CONFLICT(resolution, bucket_start, metric, dim_key) DO UPDATE SET"
                    " value=excluded.value, cnt=excluded.cnt, updated_at=excluded.updated_at",
                    (_iso(b), float(total), total))
                db.commit()
            written += 1
        except Exception as e:
            log.warning(f"slo backfill: write bucket {b} failed: {e}")
    if hours:
        log.info(f"slo backfill: covered {written}/{len(hours)} missing hour(s)"
                 f" in [{_iso(start)}, {_iso(end)}) (Issue #544 L1)")
        if len(hours) >= _MAX_HOURS_PER_JOB:
            # 还有更旧的缺失段：重新入队交给下一限流间隔的作业（worker 侧；测试 drain 会同步消化）
            _QUEUE.put((start, hours[-1], _GEN["n"]))
    return written


def drain_for_test(timeout: float = 30.0) -> int:
    """测试辅助：同步执行完队列中全部作业（含续排段，绕过 worker 线程的限流等待）。"""
    n = 0
    deadline = _now() + timedelta(seconds=timeout)
    while True:
        try:
            item = _QUEUE.get_nowait()
        except Exception:
            return n
        if item is None:
            continue
        start, end, gen = item
        if gen != _GEN["n"]:
            continue
        n += _run_job(start, end)
        if _now() > deadline:
            return n
