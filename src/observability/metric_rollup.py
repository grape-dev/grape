"""业务指标分钟/小时聚合（P1-2）：`obs_metric_rollup` 的写入、查询与留存。

为什么需要它：总览趋势与突变检测都在**直查 `agent_tasks` / `obs_llm_usage`**，表一大就慢；
而 SLO（P2-1）的"分钟桶比例法"扣减必须有**可靠的分钟级底座**。

口径（与设计 §6.2 一致，两处偏离已注明）：

| 项 | 值 |
|---|---|
| 表 | `obs_metric_rollup(resolution, bucket_start, metric, dim_key, repo_key, value, cnt)` |
| 分辨率 | `minute`（保留 7 天）+ `hour`（保留 30 天）；**小时桶直接从原始表重算**（精确，p95 不靠分钟值拼） |
| 维度 | `repo:<repo_key>`（各仓库）+ `global`（平台汇总；**只在 L2 暴露**） |
| 指标 | 任务量/结果/成功率、排队等待与耗时的 avg+p95、四种 token、`runtimes_online`（仅 global）、`engine_avail_online/total`（仅 global，引擎可用率小时桶预聚合，Issue #437） |
| `value`/`cnt` | `value` = 该桶的聚合值（计数/比率/均值/p95/求和）；`cnt` = 参与样本数（比率类=分母，便于加权平均） |
| 写入 | 60s 循环做**追赶式补齐**（重启/停机后自动补），每 tick 限量，避免长事务 |
| 补数跨度 | 分钟默认回看 24h、小时回看 30 天（首次安装即有可用趋势） |

**偏离设计的两点**（记录原因）：
1. 设计把主机资源也塞进本表；实际上设备资源已有 `obs_runtime_samples`（60s/7 天，服务资源曲线）——
   两边都写会重复，故**本表只做业务维度**。
2. 时间戳格式不统一：`agent_tasks.*` 是 **ISO(UTC)**，而 `obs_llm_usage.created_at` 走 DDL 默认
   `datetime('now')` → **MySQL 本地时间(CST)**。所以 token 桶用**放宽窗口 + Python 精确分桶**
   （`normalize_event_time` 会把无时区串按 CST 解释），不依赖字符串比较。这是个真实存在的坑。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.api.time_utils import normalize_event_time
from src.config import log
from src.observability.overview import _p95   # 复用同一分位数实现，避免两套口径

RES_MINUTE = "minute"
RES_HOUR = "hour"

MINUTE_RETENTION_DAYS = 7
HOUR_RETENTION_DAYS = 30

#: 需要按"求和"聚合的指标；比率/均值类按 cnt 加权；p95 取最大值（保守近似）
#: engine_avail_online/total（Issue #437）：引擎可用率的小时桶预聚合（仅 global 维），
#: SLO 据此读小时桶，不再每轮全窗扫 obs_runtime_samples
SUM_METRICS = ("task_enqueued", "task_done", "task_failed", "task_cancelled",
               "token_input", "token_output", "token_cache", "token_total",
               "engine_avail_online", "engine_avail_total")
RATE_METRICS = ("task_success_rate",)
AVG_METRICS = ("task_queue_wait_avg", "task_duration_avg")
P95_METRICS = ("task_queue_wait_p95", "task_duration_p95")
GAUGE_METRICS = ("runtimes_online",)
ALL_METRICS = SUM_METRICS + RATE_METRICS + AVG_METRICS + P95_METRICS + GAUGE_METRICS

DIM_GLOBAL = "global"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _dt(raw: Any) -> Optional[datetime]:
    """解析存储层时间（ISO 或 MySQL 本地串都行）。"""
    s = normalize_event_time(raw)
    try:
        v = str(s or "").strip().replace("Z", "+00:00")
        if not v:
            return None
        d = datetime.fromisoformat(v)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def floor_bucket(ts: datetime, resolution: str) -> datetime:
    ts = ts.astimezone(timezone.utc)
    if resolution == RES_HOUR:
        return ts.replace(minute=0, second=0, microsecond=0)
    return ts.replace(second=0, microsecond=0)


def _repo_dim(repo_key: str) -> str:
    return f"repo:{repo_key}"


# ---------------------------------------------------------------- 计算

def _agg(values: Sequence[float]) -> Optional[Tuple[float, int]]:
    if not values:
        return None
    return (round(sum(values) / len(values), 3), len(values))


def _local_str(dt: datetime) -> str:
    """把 UTC 时刻转成"DB 本地"字符串（MySQL `datetime('now')` 写出来的形态，CST=UTC+8）。

    `obs_llm_usage.created_at` 走 DDL 默认值，落库就是这种形态——比较时不能用 ISO 串。
    """
    return (dt.astimezone(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def fetch_usage_rows(start: datetime, end: datetime) -> List[tuple]:
    """取窗口内的 token 用量行（粗筛用本地时间字符串，±1 天放宽；精确分桶在 Python 侧做）。

    `catch_up` 一次取整段窗口的行，避免每个桶都查一次库（回填 24h 时是 1440 次 → 1 次）。
    """
    from src.session_manager import db, db_lock
    lo = _local_str(start - timedelta(days=1))
    hi = _local_str(end + timedelta(days=1))
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT repo_key, created_at, input_tokens, output_tokens,"
                      " cache_read_tokens, cache_write_tokens, reasoning_tokens"
                      " FROM obs_llm_usage WHERE created_at >= ? AND created_at <= ?", (lo, hi))
            return list(c.fetchall())
    except Exception as e:
        log.warning(f"obs rollup: usage fetch failed: {e}")
        return []


def compute_bucket(start: datetime, end: datetime, usage_rows: Optional[List[tuple]] = None
                   ) -> Dict[str, Dict[str, Tuple[float, int]]]:
    """算一个桶（[start, end)）的全部指标，按维度分组。

    `usage_rows` 可传入预算好的 token 行（`catch_up` 会对整段窗口只查一次库）。
    返回 `{dim_key: {metric: (value, cnt)}}`；维度含各仓库 + `global`。
    """
    from src.session_manager import db, db_lock

    per_repo: Dict[str, Dict[str, List[float]]] = {}

    def bucket(repo_key: str) -> Dict[str, List[float]]:
        return per_repo.setdefault(repo_key or "", {})

    s_iso, e_iso = _iso(start), _iso(end)

    with db_lock:
        c = db.cursor()
        # ① 新建任务（按 created_at）
        c.execute("SELECT repo_key, status, COUNT(*) FROM agent_tasks"
                  " WHERE created_at >= ? AND created_at < ?"
                  " AND repo_key IS NOT NULL AND repo_key != ''"
                  " GROUP BY repo_key, status", (s_iso, e_iso))
        for repo_key, _status, n in c.fetchall():
            bucket(repo_key).setdefault("task_enqueued", []).append(float(n))

        # ② 终态任务（按 completed_at）：done/failed/cancelled + 成功率
        c.execute("SELECT repo_key, status, COUNT(*) FROM agent_tasks"
                  " WHERE completed_at >= ? AND completed_at < ?"
                  " AND repo_key IS NOT NULL AND repo_key != ''"
                  " GROUP BY repo_key, status", (s_iso, e_iso))
        for repo_key, status, n in c.fetchall():
            st = (status or "").lower()
            m = bucket(repo_key)
            if st == "completed":
                m.setdefault("task_done", []).append(float(n))
            elif st == "failed":
                m.setdefault("task_failed", []).append(float(n))
            elif st == "cancelled":
                m.setdefault("task_cancelled", []).append(float(n))

        # ③ 排队等待与执行耗时（started_at 落在桶内的任务）
        c.execute("SELECT repo_key, created_at, started_at, completed_at FROM agent_tasks"
                  " WHERE started_at >= ? AND started_at < ?"
                  " AND repo_key IS NOT NULL AND repo_key != ''", (s_iso, e_iso))
        for repo_key, created_at, started_at, completed_at in c.fetchall():
            m = bucket(repo_key)
            cdt, sdt, edt = _dt(created_at), _dt(started_at), _dt(completed_at)
            if cdt and sdt and sdt >= cdt:
                m.setdefault("_queue_wait", []).append((sdt - cdt).total_seconds())
            if sdt and edt and edt >= sdt:
                m.setdefault("_duration", []).append((edt - sdt).total_seconds())

    # ④ Token（格式混用：粗筛在 fetch_usage_rows 里，精确分桶在 Python）
    for repo_key, created_at, ti, to, tcr, tcw, tr in (usage_rows if usage_rows is not None
                                                       else fetch_usage_rows(start, end)):
        cdt = _dt(created_at)
        if not cdt or not (start <= cdt < end):
            continue
        m = bucket(repo_key)
        i_in = int(ti or 0)
        i_out = int(to or 0) + int(tr or 0)      # reasoning 并入 output（与总览口径一致）
        i_c = int(tcr or 0) + int(tcw or 0)
        m.setdefault("token_input", []).append(float(i_in))
        m.setdefault("token_output", []).append(float(i_out))
        m.setdefault("token_cache", []).append(float(i_c))
        m.setdefault("token_total", []).append(float(i_in + i_out + i_c))

    # ⑤ 引擎在线数（仅 global，桶末时刻快照）
    online = 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM runtime_credentials"
                      " WHERE status != 'revoked' AND last_seen_at IS NOT NULL"
                      " AND last_seen_at >= ?", (_iso(end - timedelta(minutes=5)),))
            online = int((c.fetchone() or [0])[0] or 0)
    except Exception as e:
        log.warning(f"obs rollup: online count failed: {e}")

    # ⑥ 引擎可用率采样（Issue #437）：SLO 的 engine_availability 从这里读小时桶，
    #    不再每轮评估全窗扫描 obs_runtime_samples（仅 global 维，桶内 SUM(online)/COUNT(*)）
    avail: Optional[Tuple[float, int]] = None
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT SUM(online), COUNT(*) FROM obs_runtime_samples"
                      " WHERE sampled_at >= ? AND sampled_at < ?", (s_iso, e_iso))
            _row = c.fetchone()
            if _row and int(_row[1] or 0) > 0:
                avail = (float(_row[0] or 0), int(_row[1]))
    except Exception as e:
        log.warning(f"obs rollup: availability agg failed: {e}")

    out: Dict[str, Dict[str, Tuple[float, int]]] = {}
    for repo_key, m in per_repo.items():
        row: Dict[str, Tuple[float, int]] = {}
        if m.get("task_enqueued"):
            n = int(sum(m["task_enqueued"]))
            row["task_enqueued"] = (float(n), n)
        for k, metric in (("task_done", "task_done"), ("task_failed", "task_failed"),
                          ("task_cancelled", "task_cancelled")):
            if m.get(k):
                n = int(sum(m[k]))
                row[metric] = (float(n), n)
        n_done = int(sum(m.get("task_done") or []))
        n_bad = int(sum(m.get("task_failed") or [])) + int(sum(m.get("task_cancelled") or []))
        if n_done + n_bad > 0:
            row["task_success_rate"] = (round(n_done * 100.0 / (n_done + n_bad), 2), n_done + n_bad)
        for metric, key in (("task_queue_wait_avg", "_queue_wait"), ("task_duration_avg", "_duration")):
            got = _agg(m.get(key) or [])
            if got:
                row[metric] = got
        for metric, key in (("task_queue_wait_p95", "_queue_wait"), ("task_duration_p95", "_duration")):
            vals = m.get(key) or []
            p = _p95(vals)
            if p is not None:
                row[metric] = (round(float(p), 3), len(vals))
        for metric in ("token_input", "token_output", "token_cache", "token_total"):
            if m.get(metric):
                vals = m[metric]
                row[metric] = (round(sum(vals), 3), len(vals))
        if row:
            out[_repo_dim(repo_key or "")] = row

    # 未归属的用量（chat/im/wiki 等主控机内运行的引擎，没有 repo_key）：
    # **不进任何仓库维度**（否则会冒出一个 `repo:` 空维度），但**要计入 global**——
    # 否则全局 token 趋势永远看不到这部分真实开销。
    _unattr = out.pop(_repo_dim(""), None)
    _dims_for_global = list(out.values()) + ([_unattr] if _unattr else [])

    # ---- global：各指标按口径汇总 ----
    g: Dict[str, Tuple[float, int]] = {}
    for metric in SUM_METRICS:
        tot = sum(v for r in _dims_for_global if metric in r for v, _c in [r[metric]])
        cnt = sum(c for r in _dims_for_global if metric in r for _v, c in [r[metric]])
        if cnt:
            g[metric] = (round(tot, 3), cnt)
    tot_done = g.get("task_done", (0, 0))[0]
    tot_bad = g.get("task_failed", (0, 0))[0] + g.get("task_cancelled", (0, 0))[0]
    if tot_done + tot_bad > 0:
        g["task_success_rate"] = (round(tot_done * 100.0 / (tot_done + tot_bad), 2),
                                  int(tot_done + tot_bad))
    for metric in AVG_METRICS:      # 加权平均（按 cnt 权重）
        num = sum(r[metric][0] * r[metric][1] for r in _dims_for_global if metric in r)
        den = sum(r[metric][1] for r in _dims_for_global if metric in r)
        if den:
            g[metric] = (round(num / den, 3), den)
    for metric in P95_METRICS:      # 各仓库 p95 取最大（保守，宁高不低）
        vals = [r[metric][0] for r in _dims_for_global if metric in r]
        cnts = [r[metric][1] for r in _dims_for_global if metric in r]
        if vals:
            g[metric] = (max(vals), sum(cnts))
    g["runtimes_online"] = (float(online), 1)
    if avail:      # 引擎可用率预聚合（仅 global；见 ⑥）
        g["engine_avail_online"] = (round(avail[0], 3), avail[1])
        g["engine_avail_total"] = (float(avail[1]), avail[1])
    out[DIM_GLOBAL] = g
    return out


def write_bucket(resolution: str, bucket_start: datetime, rows: Dict[str, Dict[str, Tuple[float, int]]]
                 ) -> int:
    """写入/更新一个桶（幂等：同一 (resolution, bucket, metric, dim) 覆盖）。返回写入行数。"""
    from src.session_manager import db, db_lock
    b_iso = _iso(bucket_start)
    now_iso = _iso(_now())
    n = 0
    with db_lock:
        c = db.cursor()
        for dim_key, metrics in rows.items():
            repo_key = dim_key.split(":", 1)[1] if dim_key.startswith("repo:") else ""
            for metric, (value, cnt) in metrics.items():
                c.execute(
                    "INSERT INTO obs_metric_rollup (resolution, bucket_start, metric, dim_key,"
                    " repo_key, value, cnt, updated_at) VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(resolution, bucket_start, metric, dim_key) DO UPDATE SET"
                    " value=excluded.value, cnt=excluded.cnt, repo_key=excluded.repo_key,"
                    " updated_at=excluded.updated_at",
                    (resolution, b_iso, metric, dim_key, repo_key, float(value), int(cnt), now_iso))
                n += 1
        db.commit()
    return n


# ---------------------------------------------------------------- 追赶补齐

def _existing_buckets(resolution: str, oldest: datetime, newest: datetime) -> set:
    from src.session_manager import db, db_lock
    out = set()
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT DISTINCT bucket_start FROM obs_metric_rollup"
                      " WHERE resolution=? AND bucket_start >= ? AND bucket_start <= ?",
                      (resolution, _iso(oldest), _iso(newest)))
            out = {r[0] for r in c.fetchall()}
    except Exception as e:
        log.warning(f"obs rollup: existing buckets failed: {e}")
    return out


def missing_buckets(resolution: str, oldest: datetime, newest: datetime,
                    limit: int) -> List[datetime]:
    """取尚未聚合的桶（最多 limit 个）。

    **从新到旧**：重启/停机后的回填优先补最近的桶 —— 用户/图表看的是当下的趋势，
    历史缺口在后续 tick 里慢慢补齐；反过来（从旧到新）会让"当前趋势"缺数据好几分钟。
    """
    have = _existing_buckets(resolution, oldest, newest)
    step = timedelta(hours=1) if resolution == RES_HOUR else timedelta(minutes=1)
    out: List[datetime] = []
    cur = floor_bucket(newest, resolution)
    floor_oldest = floor_bucket(oldest, resolution)
    while cur >= floor_oldest and len(out) < limit:
        if _iso(cur) not in have:
            out.append(cur)
        cur = cur - step
    return out


def catch_up(now: Optional[datetime] = None, minute_back_min: int = 24 * 60,
             hour_back_hours: int = 30 * 24, max_minutes: int = 180,
             max_hours: int = 48) -> Dict[str, Any]:
    """追赶式补齐：把缺的分钟/小时桶算出来。

    - 分钟：回看 `minute_back_min`（默认 24h），每 tick 最多 `max_minutes` 个（限流，避免长事务）
    - 小时：回看 `hour_back_hours`（默认 30 天），每 tick 最多 `max_hours` 个
    - 只算**已结束**的桶（当前这一分钟/小时还在变，不写）
    """
    now = now or _now()
    stats = {"minute_done": 0, "hour_done": 0, "rows": 0}
    # 分钟桶
    cur_minute = floor_bucket(now, RES_MINUTE)
    oldest_m = cur_minute - timedelta(minutes=minute_back_min)
    newest_m = cur_minute - timedelta(minutes=1)
    if newest_m >= oldest_m:
        _usage = fetch_usage_rows(oldest_m, newest_m + timedelta(minutes=1))
        for b in missing_buckets(RES_MINUTE, oldest_m, newest_m, max_minutes):
            try:
                rows = compute_bucket(b, b + timedelta(minutes=1), _usage)
                stats["rows"] += write_bucket(RES_MINUTE, b, rows)
                stats["minute_done"] += 1
            except Exception as e:
                log.warning(f"obs rollup minute {b} failed: {e}")
                break
    # 小时桶
    cur_hour = floor_bucket(now, RES_HOUR)
    oldest_h = cur_hour - timedelta(hours=hour_back_hours)
    newest_h = cur_hour - timedelta(hours=1)
    if newest_h >= oldest_h:
        _usage_h = fetch_usage_rows(oldest_h, newest_h + timedelta(hours=1))
        for b in missing_buckets(RES_HOUR, oldest_h, newest_h, max_hours):
            try:
                rows = compute_bucket(b, b + timedelta(hours=1), _usage_h)
                stats["rows"] += write_bucket(RES_HOUR, b, rows)
                stats["hour_done"] += 1
            except Exception as e:
                log.warning(f"obs rollup hour {b} failed: {e}")
                break
    if stats["minute_done"] or stats["hour_done"]:
        log.info(f"obs rollup: {stats}")
    return stats


def cleanup(now: Optional[datetime] = None) -> Dict[str, int]:
    """留存清理：分钟 7 天、小时 30 天。"""
    from src.session_manager import db, db_lock
    now = now or _now()
    cut_m = _iso(now - timedelta(days=MINUTE_RETENTION_DAYS))
    cut_h = _iso(now - timedelta(days=HOUR_RETENTION_DAYS))
    out = {"minute": 0, "hour": 0}
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM obs_metric_rollup WHERE resolution=? AND bucket_start < ?",
                      (RES_MINUTE, cut_m))
            out["minute"] = int(c.rowcount or 0)
            c.execute("DELETE FROM obs_metric_rollup WHERE resolution=? AND bucket_start < ?",
                      (RES_HOUR, cut_h))
            out["hour"] = int(c.rowcount or 0)
            db.commit()
        if out["minute"] or out["hour"]:
            log.info(f"obs rollup cleanup: {out}")
    except Exception as e:
        log.warning(f"obs rollup cleanup failed: {e}")
    return out


# ---------------------------------------------------------------- 查询

def series(metrics: Iterable[str], dim_keys: Iterable[str], start: datetime, end: datetime,
           resolution: str = RES_MINUTE) -> Dict[str, List[Dict[str, Any]]]:
    """按桶取序列：`{metric: [{bucket, value, cnt}]}`。

    - 求和类：同桶多维度相加；比率/均值类：按 `cnt` 加权合并；p95 类：取最大（保守）
    - 维度为空 → 返回空序列（**绝不降级成全量**）
    """
    dims = [d for d in dim_keys if d]
    mets = [m for m in metrics if m]
    out: Dict[str, List[Dict[str, Any]]] = {m: [] for m in mets}
    if not dims or not mets:
        return out
    from src.session_manager import db, db_lock
    ph_d = ",".join("?" * len(dims))
    ph_m = ",".join("?" * len(mets))
    rows: List[tuple] = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute(f"SELECT metric, bucket_start, value, cnt FROM obs_metric_rollup"
                      f" WHERE resolution=? AND metric IN ({ph_m}) AND dim_key IN ({ph_d})"
                      f" AND bucket_start >= ? AND bucket_start <= ? ORDER BY bucket_start",
                      (resolution, *mets, *dims, _iso(start), _iso(end)))
            rows = list(c.fetchall())
    except Exception as e:
        log.warning(f"obs rollup series failed: {e}")
        return out
    agg: Dict[Tuple[str, str], List[Tuple[float, int]]] = {}
    for metric, b_start, value, cnt in rows:
        agg.setdefault((metric, b_start), []).append((float(value or 0), int(cnt or 0)))
    for (metric, b_start), items in sorted(agg.items(), key=lambda kv: kv[0][1]):
        if metric in P95_METRICS:
            v = max(x[0] for x in items)
            n = sum(x[1] for x in items)
        elif metric in RATE_METRICS or metric in AVG_METRICS or metric in GAUGE_METRICS:
            den = sum(x[1] for x in items)
            v = round(sum(x[0] * x[1] for x in items) / den, 3) if den else 0.0
            n = den
        else:
            v = round(sum(x[0] for x in items), 3)
            n = sum(x[1] for x in items)
        out[metric].append({"bucket": b_start, "value": v, "cnt": n})
    return out


def has_data(resolution: str, start: datetime, end: datetime) -> bool:
    return bool(_existing_buckets(resolution, start, end))


def overview_trends(dim_keys: List[str], start: datetime, bucket_seconds: int, nbuckets: int
                    ) -> Optional[Dict[str, Any]]:
    """总览趋势（任务量与结果 / Token）：从 rollup 读并聚到目标桶宽；无数据返回 None（调用方回退直查）。

    返回与 `overview.build_overview` 的 charts 同形状（buckets/completed/failed/cancelled/rate）。
    """
    end = start + timedelta(seconds=bucket_seconds * nbuckets)
    res = RES_MINUTE if bucket_seconds < 3600 else RES_HOUR
    data = series(["task_done", "task_failed", "task_cancelled",
                   "token_input", "token_output", "token_cache"], dim_keys, start, end, res)
    if not any(data.get(k) for k in data):
        return None
    idx = {start + timedelta(seconds=i * bucket_seconds): i for i in range(nbuckets)}
    completed = [0] * nbuckets
    failed = [0] * nbuckets
    cancelled = [0] * nbuckets
    t_in = [0.0] * nbuckets
    t_out = [0.0] * nbuckets
    t_cache = [0.0] * nbuckets

    def slot(b_start: str) -> Optional[int]:
        dt = _dt(b_start)
        if dt is None:
            return None
        flo = start + timedelta(seconds=int((dt - start).total_seconds() // bucket_seconds)
                               * bucket_seconds)
        return idx.get(flo)

    for item in data["task_done"]:
        i = slot(item["bucket"])
        if i is not None:
            completed[i] += int(item["value"])
    for item in data["task_failed"]:
        i = slot(item["bucket"])
        if i is not None:
            failed[i] += int(item["value"])
    for item in data["task_cancelled"]:
        i = slot(item["bucket"])
        if i is not None:
            cancelled[i] += int(item["value"])
    for key, arr in (("token_input", t_in), ("token_output", t_out), ("token_cache", t_cache)):
        for item in data[key]:
            i = slot(item["bucket"])
            if i is not None:
                arr[i] += float(item["value"])
    rate = []
    for i in range(nbuckets):
        tot = completed[i] + failed[i] + cancelled[i]
        rate.append(round(completed[i] * 100.0 / tot, 1) if tot else None)
    return {
        "task_outcome": {
            "buckets": [(start + timedelta(seconds=i * bucket_seconds)).isoformat()
                        for i in range(nbuckets)],
            "completed": completed, "failed": failed, "cancelled": cancelled, "rate": rate,
        },
        "token_trend": {
            "buckets": [(start + timedelta(seconds=i * bucket_seconds)).isoformat()
                        for i in range(nbuckets)],
            "input": [int(x) for x in t_in], "output": [int(x) for x in t_out],
            "cache": [int(x) for x in t_cache],
        },
    }