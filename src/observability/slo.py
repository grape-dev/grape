"""SLO / 错误预算（P2-1，§6.7）—— 把"一堆当前值"变成一句可承诺的**结论**。

目标形态：`本月任务成功率 ≥ 99% —— 当前 99.4%，错误预算剩余 63%`。

三类 SLI（只做三类，不做通用 DSL）：

| sli_type | 定义 | 底座 | 粒度 |
|---|---|---|---|
| `task_success_rate` | 成功任务数 / 任务总数 | `obs_metric_rollup`（done/failed/cancelled） | 小时桶 |
| `task_queue_wait_p95` | 排队时延 P95 ≤ `max_p95_sec` 的时间占比 | `obs_metric_rollup`（`task_queue_wait_p95`） | 小时桶 |
| `engine_availability` | 在线采样数 / 总采样数 | `obs_runtime_samples.online` | 小时桶 |

**为什么桶粒度是小时而不是设计稿里的"分钟桶"**：分钟明细只保留 7 天（`obs_metric_rollup` 分钟
分辨率留 7 天），而窗口默认 30 天 —— 30 天窗口只有小时底座可用。方法本身（**桶比例法**）不变：
`坏桶数 / 有数据的桶数`，同一桶内无论触发多少次只算一桶，重叠天然合并，错误预算
= `(1 - target) × 有数据的桶数`，燃尽曲线 = 逐桶累计消耗。这是与观测云"事件时长合并"等价但更简单的口径。

**引擎可用率的数据缺口（已核实，2026-10-18）**：`obs_runtime_samples.health_score` 来自
`runtime_health_scores`，是"近 N 天任务健康度"**不是在线**，且 dev 库 7454 行实测全为 0；
也没有心跳历史表（只有 `last_seen_at` 当前值）。因此本仓给采样表加了 `online` 列，由采样循环按
**心跳新鲜度**（`last_seen_at` 距采样时刻 ≤ `rollup.ONLINE_WINDOW_SEC`）逐点记录 ——
可用率**从该列上线时刻起**才可算；此前窗口返回 `state='no_data'`（**不假装 0%/100%**）。

**违约如何通知**：不新建通知链路 —— 违约写一条 `source='slo'` 的事件进统一事件视图（§6.9），
复用告警的静默/通知渠道（观测云"事件中心只聚合、告警策略统一投递"的分层）。

**边界**：仓库范围 SLO = L1 用户面（在调用者可见仓库上算）；`engine_availability` 这类平台目标 = L2。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.config import log

# ---------------------------------------------------------------- 常量

SLI_SUCCESS = "task_success_rate"
SLI_LATENCY = "task_queue_wait_p95"
SLI_AVAIL = "engine_availability"
SLI_TYPES = (SLI_SUCCESS, SLI_LATENCY, SLI_AVAIL)

#: 默认 SLO（幂等 seed；目标值可由管理台改）
DEFAULTS: List[Dict[str, Any]] = [
    {"name": SLI_SUCCESS, "sli_type": SLI_SUCCESS, "scope_type": "global",
     "target": 0.99, "window_days": 30, "threshold_json": "{}"},
    {"name": SLI_LATENCY, "sli_type": SLI_LATENCY, "scope_type": "global",
     "target": 0.95, "window_days": 30, "threshold_json": '{"max_p95_sec": 300}'},
    {"name": SLI_AVAIL, "sli_type": SLI_AVAIL, "scope_type": "engine",
     "target": 0.99, "window_days": 30, "threshold_json": "{}"},
]

STATE_OK = "ok"
STATE_WARNING = "warning"
STATE_BREACHED = "breached"
STATE_NO_DATA = "no_data"

#: 预算消耗比例 ≥ 该值 → warning；≥ 1.0 → breached。
#: 比较时带 EPS：预算总量是 `(1-target)×桶数` 的浮点结果（如 0.01*400=4.000000000000004），
#: 3 个坏桶本意是"刚好 75%"，不留容差会被判成 ok（单测实测踩到）。
WARN_CONSUMED = 0.75
_EPS = 1e-9
#: 窗口最长回看（与小时底座留存一致）
MAX_WINDOW_DAYS = 30
#: 单次计算最多返回的燃尽点（超长窗口抽稀，避免前端画 720 个点）
MAX_BURN_POINTS = 240

_RES_HOUR = "hour"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _dt(raw: Any) -> Optional[datetime]:
    s = str(raw or "").strip().replace("Z", "+00:00")
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _json(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(str(raw or "{}"))
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------- 配置读写

def seed_defaults() -> int:
    """幂等写入 3 条默认 SLO（按 name 唯一）。返回新增条数。"""
    from src.session_manager import db, db_lock
    added = 0
    try:
        with db_lock:
            c = db.cursor()
            for d in DEFAULTS:
                c.execute("SELECT id FROM obs_slo WHERE name=?", (d["name"],))
                if c.fetchone():
                    continue
                c.execute("INSERT INTO obs_slo (name, sli_type, scope_type, scope_key, target,"
                          " window_days, threshold_json, enabled, created_by)"
                          " VALUES (?,?,?,?,?,?,?,1,'seed')",
                          (d["name"], d["sli_type"], d["scope_type"], "", float(d["target"]),
                           int(d["window_days"]), d["threshold_json"]))
                added += 1
            db.commit()
    except Exception as e:
        log.warning(f"slo seed failed: {e}")
    return added


def _row_to_slo(row: tuple, cols: List[str]) -> Dict[str, Any]:
    d = dict(zip(cols, row))
    d["target"] = float(d.get("target") or 0)
    d["window_days"] = int(d.get("window_days") or 30)
    d["enabled"] = bool(d.get("enabled"))
    d["threshold"] = _json(d.get("threshold_json"))
    return d


_SLO_COLS = ("id", "name", "sli_type", "scope_type", "scope_key", "target", "window_days",
             "threshold_json", "enabled", "created_by", "created_at", "updated_at")


def list_slos(enabled_only: bool = True) -> List[Dict[str, Any]]:
    from src.session_manager import db, db_lock
    sql = f"SELECT {','.join(_SLO_COLS)} FROM obs_slo"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    out: List[Dict[str, Any]] = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute(sql)
            rows = c.fetchall()
        out = [_row_to_slo(r, list(_SLO_COLS)) for r in rows]
    except Exception as e:
        log.warning(f"slo list failed: {e}")
    return out


def get_slo(slo_id: int) -> Optional[Dict[str, Any]]:
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute(f"SELECT {','.join(_SLO_COLS)} FROM obs_slo WHERE id=?", (int(slo_id),))
            row = c.fetchone()
        return _row_to_slo(tuple(row), list(_SLO_COLS)) if row else None
    except Exception as e:
        log.warning(f"slo get failed: {e}")
        return None


def create_slo(name: str, sli_type: str, scope_type: str = "global", scope_key: str = "",
               target: float = 0.99, window_days: int = 30,
               threshold_json: str = "{}", created_by: str = "") -> int:
    """新建 SLO（管理台）；同名已存在 → 返回现有 id（幂等）。"""
    from src.session_manager import db, db_lock
    if sli_type not in SLI_TYPES:
        raise ValueError(f"unknown sli_type: {sli_type}")
    if scope_type not in ("repo", "global", "engine"):
        raise ValueError(f"unknown scope_type: {scope_type}")
    name = str(name or "").strip()[:64]
    if not name:
        raise ValueError("name required")
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM obs_slo WHERE name=?", (name,))
        row = c.fetchone()
        if row:
            return int(row[0])
        c.execute("INSERT INTO obs_slo (name, sli_type, scope_type, scope_key, target,"
                  " window_days, threshold_json, enabled, created_by)"
                  " VALUES (?,?,?,?,?,?,?,1,?)",
                  (name, sli_type, scope_type, str(scope_key or ""),
                   float(target), int(window_days), str(threshold_json or "{}"),
                   str(created_by or "")))
        db.commit()
        return int(c.lastrowid or 0)


_UPDATABLE = ("target", "window_days", "threshold_json", "enabled", "scope_type", "scope_key")


def update_slo(slo_id: int, **fields: Any) -> bool:
    """改配置（管理台统一设定目标值）。只允许白名单字段。"""
    from src.session_manager import db, db_lock
    sets, params = [], []
    for k, v in fields.items():
        if k not in _UPDATABLE or v is None:
            continue
        if k == "enabled":
            v = 1 if v else 0
        elif k in ("target",):
            v = float(v)
            if not (0 < v < 1):
                raise ValueError("target 必须在 (0,1) 之间")
        elif k == "window_days":
            v = max(1, min(int(v), MAX_WINDOW_DAYS))
        sets.append(f"{k}=?")
        params.append(v)
    if not sets:
        return False
    params.append(int(slo_id))
    try:
        with db_lock:
            c = db.cursor()
            c.execute(f"UPDATE obs_slo SET {','.join(sets)}, updated_at=datetime('now') WHERE id=?",
                      tuple(params))
            db.commit()
        return True
    except Exception as e:
        log.warning(f"slo update failed: {e}")
        return False


# ---------------------------------------------------------------- 计算（三类 SLI → 桶序列）

def _hour_buckets(start: datetime, end: datetime) -> List[datetime]:
    out: List[datetime] = []
    cur = start.replace(minute=0, second=0, microsecond=0)
    if cur < start:
        cur = start.replace(minute=0, second=0, microsecond=0)
    while cur < end:
        out.append(cur)
        cur = cur + timedelta(hours=1)
    return out


def _rollup_hour_series(metrics: List[str], dim_keys: List[str],
                        start: datetime, end: datetime) -> Dict[str, List[Dict[str, Any]]]:
    from src.observability import metric_rollup
    return metric_rollup.series(metrics, dim_keys, start, end, _RES_HOUR)


def _success_buckets(dim_keys: List[str], start: datetime, end: datetime
                     ) -> List[Tuple[str, float, float]]:
    """任务成功率：(桶, 成功数, 总数)。"""
    data = _rollup_hour_series(["task_done", "task_failed", "task_cancelled"],
                               dim_keys, start, end)
    idx: Dict[str, Dict[str, float]] = {}
    for metric in ("task_done", "task_failed", "task_cancelled"):
        for pt in (data.get(metric) or []):
            b = str(pt.get("bucket") or "")
            if not b:
                continue
            slot = idx.setdefault(b, {"good": 0.0, "bad": 0.0})
            slot["good" if metric == "task_done" else "bad"] += float(pt.get("value") or 0)
    return [(b, v["good"], v["good"] + v["bad"]) for b, v in sorted(idx.items())]


def _latency_buckets(dim_keys: List[str], start: datetime, end: datetime, max_p95_sec: float
                     ) -> Tuple[List[Tuple[str, float, float]], Optional[float]]:
    """排队时延：(桶, 达标?, 1) + 窗口 P95（小时桶 p95 的 95 分位，作展示值）。

    桶达标判定 = 该小时 p95 ≤ 阈值。窗口 P95 不等于"把小时 p95 再平均"，
    这里取各小时 p95 的 95 分位（即"95% 的小时都不高于该值"），并在文档中写明口径。
    """
    data = _rollup_hour_series(["task_queue_wait_p95"], dim_keys, start, end)
    pts = [p for p in (data.get("task_queue_wait_p95") or []) if p.get("cnt")]
    if not pts:
        return [], None
    rows: List[Tuple[str, float, float]] = []
    vals: List[float] = []
    for p in pts:
        v = float(p.get("value") or 0)
        vals.append(v)
        rows.append((str(p.get("bucket") or ""), 1.0 if v <= max_p95_sec else 0.0, 1.0))
    vals.sort()
    k = max(0, min(len(vals) - 1, int(round(0.95 * (len(vals) - 1)))))
    return rows, round(vals[k], 3)


def _availability_buckets(start: datetime, end: datetime,
                          not_before: Optional[datetime] = None
                          ) -> List[Tuple[str, float, float]]:
    """引擎可用率：(桶, 在线采样数, 总采样数)。数据源 `obs_runtime_samples.online`。

    `not_before`：`online` 列上线前的历史行是"未知"（建列默认 0），把它们当"离线"会把可用率
    长期按 0 算（真机实测：加列当天 L2 可用率 = 0% → 误报违约）。因此可用率**只从该 SLO 的
    `created_at` 起算**（SLO 建起来 = 这列已在生产里数数了）；更早的桶直接不进窗口。
    """
    from src.session_manager import db, db_lock
    if not_before and not_before > start:
        start = not_before
    if start >= end:
        return []
    s_iso, e_iso = _iso(start), _iso(end)
    rows: List[tuple] = []
    try:
        with db_lock:
            c = db.cursor()
            # sampled_at 是 ISO 串；按前 13 位对齐到小时（两个后端都支持 substr）
            c.execute("SELECT substr(sampled_at, 1, 13) AS h, SUM(online), COUNT(*)"
                      " FROM obs_runtime_samples WHERE sampled_at >= ? AND sampled_at < ?"
                      " GROUP BY h ORDER BY h", (s_iso, e_iso))
            rows = c.fetchall()
    except Exception as e:
        log.warning(f"slo availability query failed: {e}")
        return []
    out: List[Tuple[str, float, float]] = []
    for h, good, total in rows:
        b = f"{h}:00:00+00:00"
        out.append((b, float(good or 0), float(total or 0)))
    return out


def window_of(slo: Dict[str, Any], now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    days = max(1, min(int(slo.get("window_days") or 30), MAX_WINDOW_DAYS))
    end = now or _now()
    return end - timedelta(days=days), end


def compute(slo: Dict[str, Any], dim_keys: List[str], now: Optional[datetime] = None,
            allow_engine: bool = False, with_burn: bool = True) -> Dict[str, Any]:
    """算一条 SLO 的当前结论。

    `dim_keys`：L1 传调用者可见仓库（`repo:<key>`）；L2 传 `['global']`（平台汇总）。
    `allow_engine`：`engine_availability` 属平台目标，只有 L2 允许算（L1 → `no_data` + reason）。
    """
    start, end = window_of(slo, now)
    sli = slo.get("sli_type") or ""
    target = float(slo.get("target") or 0)
    thr = slo.get("threshold") or {}
    buckets: List[Tuple[str, float, float]] = []
    value: Optional[float] = None
    extra: Dict[str, Any] = {}
    reason = ""
    if sli == SLI_SUCCESS:
        buckets = _success_buckets(dim_keys, start, end)
    elif sli == SLI_LATENCY:
        max_p95 = float(thr.get("max_p95_sec") or 300)
        buckets, wp95 = _latency_buckets(dim_keys, start, end, max_p95)
        extra["max_p95_sec"] = max_p95
        extra["window_p95_sec"] = wp95
    elif sli == SLI_AVAIL:
        if not allow_engine:
            reason = "l2_only"
        else:
            nb = _dt(slo.get("created_at"))
            buckets = _availability_buckets(start, end, not_before=nb)
    else:
        reason = "unknown_sli"

    data_buckets = [(b, g, t) for (b, g, t) in buckets if t > 0]
    total = sum(_t for _b, _g, _t in data_buckets)
    good = sum(_g for _b, _g, _t in data_buckets)
    budget_total = (1.0 - target) * len(data_buckets)
    bad_buckets = [b for (b, g, t) in data_buckets if (g / t) < target]
    consumed = float(len(bad_buckets))
    consumed_ratio = (consumed / budget_total) if budget_total > 0 else 0.0
    if reason:
        state = STATE_NO_DATA
    elif not data_buckets:
        state = STATE_NO_DATA
        reason = "no_buckets"
    elif consumed_ratio >= 1.0 - _EPS:
        state = STATE_BREACHED
    elif consumed_ratio >= WARN_CONSUMED - _EPS:
        state = STATE_WARNING
    else:
        state = STATE_OK
    value = (good / total) if total > 0 else None
    out: Dict[str, Any] = {
        "slo_id": slo.get("id"), "name": slo.get("name"), "sli_type": sli,
        "scope_type": slo.get("scope_type") or "global",
        "scope_key": slo.get("scope_key") or "",
        "target": target, "window_days": int(slo.get("window_days") or 30),
        "window_start": _iso(start), "window_end": _iso(end),
        "value": (round(value, 6) if value is not None else None),
        "good": round(good, 3), "total": round(total, 3),
        "buckets": len(data_buckets),
        "budget_total": round(budget_total, 3),
        "budget_consumed": round(consumed, 3),
        "budget_remaining_pct": (round(max(0.0, 1.0 - consumed_ratio) * 100, 2)
                                 if budget_total > 0 else None),
        "bad_buckets": len(bad_buckets),
        "state": state, "reason": reason,
        "dim_keys": list(dim_keys),
    }
    out.update(extra)
    if with_burn:
        out["burn"] = burn_series(data_buckets, target, start, end)
    return out


def burn_series(data_buckets: List[Tuple[str, float, float]], target: float,
                start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """燃尽曲线：逐桶累计消耗（%）。点太多时等距抽稀（保留首末）。"""
    pts: List[Dict[str, Any]] = []
    cum = 0.0
    budget_total = (1.0 - target) * len(data_buckets)
    for i, (b, g, t) in enumerate(data_buckets):
        if (g / t) < target:
            cum += 1.0
        used_pct = (cum / budget_total * 100.0) if budget_total > 0 else 0.0
        pts.append({"bucket": b, "ideal_pct": round((i + 1) / max(1, len(data_buckets)) * 100.0, 2),
                    "used_pct": round(used_pct, 2), "bad": 1 if (g / t) < target else 0})
    if len(pts) > MAX_BURN_POINTS:
        step = len(pts) // MAX_BURN_POINTS + 1
        thinned = pts[::step]
        if thinned[-1] is not pts[-1]:
            thinned.append(pts[-1])
        pts = thinned
    return pts


def summary_for(slo: Dict[str, Any], dim_keys: List[str], now: Optional[datetime] = None,
                allow_engine: bool = False) -> Dict[str, Any]:
    """前端用的精简结论（不含燃尽序列）：状态 + 值 + 预算剩余。"""
    full = compute(slo, dim_keys, now=now, allow_engine=allow_engine, with_burn=False)
    return {k: v for k, v in full.items() if k != "burn"}


# ---------------------------------------------------------------- 快照（燃尽与历史回溯）

def write_snapshot(summary: Dict[str, Any], bucket_start: Optional[datetime] = None) -> bool:
    """把一次计算写进小时快照（唯一 (slo_id, bucket_start)，重复算覆盖）。"""
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO obs_slo_snapshots (slo_id, bucket_start, window_start, window_end,"
                      " good, total, value, budget_total, budget_consumed, state)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?)"
                      " ON CONFLICT(slo_id, bucket_start) DO UPDATE SET"
                      " window_start=excluded.window_start, window_end=excluded.window_end,"
                      " good=excluded.good, total=excluded.total, value=excluded.value,"
                      " budget_total=excluded.budget_total, budget_consumed=excluded.budget_consumed,"
                      " state=excluded.state, computed_at=datetime('now')",
                      (int(summary.get("slo_id") or 0), _iso(bucket_start or _now()),
                       str(summary.get("window_start") or ""), str(summary.get("window_end") or ""),
                       float(summary.get("good") or 0), float(summary.get("total") or 0),
                       float(summary.get("value") or 0), float(summary.get("budget_total") or 0),
                       float(summary.get("budget_consumed") or 0), str(summary.get("state") or "ok")))
            db.commit()
        return True
    except Exception as e:
        log.warning(f"slo snapshot write failed: {e}")
        return False


def snapshots(slo_id: int, limit: int = 168) -> List[Dict[str, Any]]:
    """取快照（默认最近 168 小时 ≈ 7 天）。"""
    from src.session_manager import db, db_lock
    out: List[Dict[str, Any]] = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT bucket_start, window_start, window_end, good, total, value,"
                      " budget_total, budget_consumed, state, computed_at"
                      " FROM obs_slo_snapshots WHERE slo_id=? ORDER BY bucket_start DESC LIMIT ?",
                      (int(slo_id), max(1, min(int(limit), 1000))))
            rows = c.fetchall()
        cols = ["bucket_start", "window_start", "window_end", "good", "total", "value",
                "budget_total", "budget_consumed", "state", "computed_at"]
        out = [dict(zip(cols, r)) for r in reversed(rows)]
    except Exception as e:
        log.warning(f"slo snapshots failed: {e}")
    return out

# ---------------------------------------------------------------- 作用域 / 违约事件 / 周期评估

#: SLO 违约不是"规则"驱动的事件：rule_id 固定 0（表里是 NOT NULL），rule_name 稳定为下面这个，
#: 既避免与 obs_alert_rules 里的真实规则冲突，也便于统一事件视图按 kind 识别。
SLO_RULE_ID = 0
SLO_RULE_NAME = "slo_breached"


def repo_dims(repo: str, user: Optional[Dict[str, Any]] = None,
              check_visible: bool = True) -> List[str]:
    """把页面上的仓库选择器（短名/全名）折算成维度；**越权抛 `RepoNotVisible`**。

    与事件视图/rollup 序列同一套口径：`repo` 命中可见集合（含 github: 前缀变体）才算数；
    校验失败**不降级**成"看不见就按全量算"（那等于没有边界）。
    """
    from src.repo_key import to_repo_key
    sk = str(repo or "").strip()
    if not sk:
        return []
    want = {to_repo_key(sk), to_repo_key(sk, "github")}
    if check_visible:
        from src.observability.overview import RepoNotVisible, visible_repo_keys
        hit = sorted(want & set(visible_repo_keys(user or {})))
        if not hit:
            raise RepoNotVisible(sk)
        return [f"repo:{hit[0]}"]
    key = sorted(want)[0]
    return [f"repo:{key}"]


def scope_dims(slo: Dict[str, Any], user: Optional[Dict[str, Any]] = None,
               global_scope: bool = False, repo: str = "") -> List[str]:
    """算一条 SLO 时用哪些维度：

    - `repo` 给了（页面上的仓库选择器）→ **只看这个仓**（L1 校验可见性，越权 → RepoNotVisible）
    - `global_scope=True`（L2，且没给 repo）→ `['global']`（平台汇总，rollup 已聚合好）
    - `scope_type='repo'` → 该仓库（L1 需先校验可见性，见 `detail()`）
    - 其余 → 调用者**可见仓库**（`repo:<key>`）；一个都没有 → 空列表（绝不降级成全量）
    """
    if repo:
        return repo_dims(repo, user=user, check_visible=not global_scope)
    if global_scope:
        return ["global"]
    if (slo.get("scope_type") or "global") == "repo" and slo.get("scope_key"):
        return [f"repo:{slo['scope_key']}"]
    try:
        from src.observability.overview import visible_repo_keys
        return [f"repo:{k}" for k in sorted(visible_repo_keys(user or {}))]
    except Exception as e:
        log.warning(f"slo scope dims failed: {e}")
        return []


def _visible(slo: Dict[str, Any], user: Optional[Dict[str, Any]]) -> bool:
    """L1 可见性：仓库范围的 SLO 只对可见该仓库的人出。"""
    if (slo.get("scope_type") or "global") != "repo" or not slo.get("scope_key"):
        return True
    try:
        from src.observability.overview import visible_repo_keys
        keys = set(visible_repo_keys(user or {}))
        sk = str(slo.get("scope_key") or "")
        return sk in keys or f"gitcode:{sk}" in keys
    except Exception:
        return False


def list_summaries(user: Optional[Dict[str, Any]] = None, global_scope: bool = False,
                   now: Optional[datetime] = None, repo: str = "") -> List[Dict[str, Any]]:
    """列表（不含燃尽序列）。L1 不出 `engine_availability`（平台目标，见模块注释边界）。

    `repo`：页面上的仓库选择器 —— 给了就只看这个仓（与总览页其它 KPI/趋势图一致）；
    L1 越权 → `RepoNotVisible`（调用方转 404）。
    """
    out: List[Dict[str, Any]] = []
    for slo in list_slos(enabled_only=True):
        if not global_scope:
            if slo.get("sli_type") == SLI_AVAIL:
                continue
            if not _visible(slo, user):
                continue
        dims = scope_dims(slo, user=user, global_scope=global_scope, repo=repo)
        out.append(compute(slo, dims, now=now, allow_engine=bool(global_scope), with_burn=False))
    return out


def detail(slo_id: int, user: Optional[Dict[str, Any]] = None, global_scope: bool = False,
           now: Optional[datetime] = None, repo: str = "") -> Optional[Dict[str, Any]]:
    """单条详情（含燃尽序列）。不可见/不存在 → None（调用方给 404）。"""
    slo = get_slo(slo_id)
    if not slo:
        return None
    if not slo.get("enabled") and not global_scope:
        return None
    if not global_scope and (not _visible(slo, user) or slo.get("sli_type") == SLI_AVAIL):
        return None
    dims = scope_dims(slo, user=user, global_scope=global_scope, repo=repo)
    return compute(slo, dims, now=now, allow_engine=bool(global_scope), with_burn=True)


def _sync_violation(slo: Dict[str, Any], summary: Dict[str, Any], now: datetime) -> str:
    """违约 → 写/续一条 `source='slo'` 的事件（进统一事件视图，复用静默/通知）；恢复 → resolved。

    只对 `breached` 触发（`warning` 是"预算快用完"的预警，卡片上显示即可，不打扰）。
    所有时间戳**显式写入 UTC**，不依赖 DDL 默认值（MySQL 下默认值是本地时间 CST，本仓已踩过）。
    """
    from src.session_manager import db, db_lock
    state = str(summary.get("state") or "")
    firing = (state == STATE_BREACHED)
    dim = f"slo:{slo.get('id')}"
    repo_key = str(slo.get("scope_key") or "") if (slo.get("scope_type") or "") == "repo" else ""
    level = "user" if (slo.get("scope_type") or "") == "repo" else "platform"
    ts = _iso(now)
    result = ""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, status FROM obs_alert_events WHERE rule_name=? AND dim_key=?"
                      " ORDER BY id DESC LIMIT 1", (SLO_RULE_NAME, dim))
            row = c.fetchone()
            open_id = int(row[0]) if row and str(row[1]) == "firing" else 0
            if firing:
                if open_id:
                    c.execute("UPDATE obs_alert_events SET last_seen_at=?, value=?, threshold=?,"
                              " updated_at=? WHERE id=?",
                              (ts, float(summary.get("value") or 0),
                               float(summary.get("target") or 0), ts, open_id))
                    result = "firing"
                else:
                    c.execute("INSERT INTO obs_alert_events (rule_id, rule_name, level, severity,"
                              " status, source, metric, value, threshold, dim_key, repo_key,"
                              " first_fired_at, last_seen_at, created_at, updated_at)"
                              " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (SLO_RULE_ID, SLO_RULE_NAME, level,
                               "critical" if repo_key else "warning", "firing", "slo",
                               str(slo.get("sli_type") or ""), float(summary.get("value") or 0),
                               float(summary.get("target") or 0), dim, repo_key, ts, ts, ts, ts))
                    result = "firing"
            elif open_id:
                c.execute("UPDATE obs_alert_events SET status='resolved', resolved_at=?,"
                          " last_seen_at=?, updated_at=? WHERE id=?", (ts, ts, ts, open_id))
                result = "resolved"
            db.commit()
    except Exception as e:
        log.warning(f"slo violation sync failed (slo={slo.get('id')}): {e}")
    return result


def evaluate_all(now: Optional[datetime] = None) -> Dict[str, int]:
    """周期评估（循环调用）：算每条 SLO → 落小时快照 → 同步违约事件 → 顺手清老快照。

    平台视角（`['global']`；repo 范围的 SLO 用自身仓库维度）——快照与违约事件都是"平台事实"，
    用户面看的是同一份事实在**自己可见范围**上的投影（`list_summaries`）。
    """
    now = now or _now()
    bucket = now.replace(minute=0, second=0, microsecond=0)
    stats = {"evaluated": 0, "snapshots": 0, "firing": 0, "resolved": 0, "cleaned": 0}
    for slo in list_slos(enabled_only=True):
        dims = (["repo:" + str(slo.get("scope_key"))]
                if (slo.get("scope_type") or "") == "repo" and slo.get("scope_key")
                else ["global"])
        summary = compute(slo, dims, now=now, allow_engine=True, with_burn=False)
        stats["evaluated"] += 1
        if write_snapshot(summary, bucket):
            stats["snapshots"] += 1
        r = _sync_violation(slo, summary, now)
        if r in ("firing", "resolved"):
            stats[r] += 1
    stats["cleaned"] = cleanup_snapshots(now=now)
    return stats


def cleanup_snapshots(now: Optional[datetime] = None, keep_days: int = 90) -> int:
    """清理老快照（小时快照留 90 天）。返回删除行数。"""
    from src.session_manager import db, db_lock
    cutoff = _iso((now or _now()) - timedelta(days=max(1, int(keep_days))))
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM obs_slo_snapshots WHERE bucket_start < ?", (cutoff,))
            n = int(c.rowcount or 0)
            db.commit()
        return n
    except Exception as e:
        log.warning(f"slo snapshot cleanup failed: {e}")
        return 0
