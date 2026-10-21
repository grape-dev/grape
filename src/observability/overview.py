"""可观测性 · 总览聚合（`/api/obs/overview` 的服务层）。

设计：docs/weekly-07/observability-overview-page-design.md
边界：docs/weekly-07/observability-redesign-design.md §5.3 —— 只返回**调用者可见范围**内的数据，
越权仓库直接 404；不下发任何平台基础设施（DB/连接池/主机拓扑）信息。

两个实现要点（踩过的坑，改动前先读）：

1. **分桶在 Python 做，不用 SQL 日期函数。** 库里时间有裸 CST 串、也有 UTC ISO 两种形态
   （见 src/api/time_utils.normalize_event_time），SQLite 与 MySQL 的日期函数又不通用，
   统一 normalize 后在 Python 分桶，才能跨方言得到一致结果。
2. **可见范围复用既有口径**，不另立一套：
   - 仓库：`user_repos` 缓存 + 组织过滤（与 `/repos/member`、`routes/repos._filter_repos` 同源）
   - 设备：`runtime_credentials` 的 scope/owner/repo 规则（与 `dashboard.ops_overview` 同源）
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.api.time_utils import normalize_event_time

# range → (窗口秒数, 分桶秒数)。桶数上限约 60，避免柱条细如发丝（见总览设计 §5「点数上限」）
RANGE_SPECS: Dict[str, Tuple[int, int]] = {
    "1h":  (3600,        60),
    "6h":  (6 * 3600,    300),
    "24h": (24 * 3600,   1800),
    "7d":  (7 * 86400,   10800),
}
DEFAULT_RANGE = "24h"


class RepoNotVisible(Exception):
    """请求的仓库不在调用者可见范围内 —— 路由层转 404（不是 403，避免探测存在性）。"""


# ---------------------------------------------------------------- 时间工具

def _repo_dim(repo_key: str) -> str:
    return f"repo:{repo_key}"


def _dt(raw: Any) -> Optional[datetime]:
    """任意存储形态 → 带时区的 UTC datetime；解析不了返回 None（不抛）。"""
    s = normalize_event_time(raw)
    if not s or not isinstance(s, str):
        return None
    try:
        d = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _bucket_start(ts: datetime, start: datetime, bucket: int) -> datetime:
    delta = int((ts - start).total_seconds())
    return start + timedelta(seconds=(delta // bucket) * bucket)


def _p95(values: List[float]) -> Optional[float]:
    if not values:
        return None
    vs = sorted(values)
    idx = max(0, min(len(vs) - 1, int(round(0.95 * (len(vs) - 1)))))
    return round(vs[idx], 1)


# ---------------------------------------------------------------- 可见范围

def visible_repo_keys(user: Dict[str, Any]) -> List[str]:
    """调用者可见的仓库 key 列表（gitcode:/github: 前缀）。只读缓存，不触发 VCS 同步。

    与 `/repos/member` 同源：user_repos 缓存 + 组织隔离（非个人组织只显示本组织已托管的）。
    缓存为空 → 返回空列表（不降级为"全部可见"，否则等于没有边界）。
    """
    uid = int(user.get("id") or 0)
    if not uid:
        return []
    try:
        from src.user.db import get_user_repos
        from src.api.routes.repos import _filter_repos     # 口径同源，勿另写一套
        from src.user.org_db import is_org_personal
    except Exception:
        return []
    org_id = int(user.get("org_id") or 0)
    try:
        _is_personal = is_org_personal(org_id) if org_id else False
    except Exception:
        _is_personal = False
    _filt_org = 0 if _is_personal else org_id
    try:
        cached = get_user_repos(uid)
        if not cached:
            return []
        rows = _filter_repos(uid, cached, org_id=_filt_org)
    except Exception:
        return []
    return sorted({(r or {}).get("repo_key") or "" for r in rows if (r or {}).get("repo_key")})


def _scope_keys(user: Dict[str, Any], repo: str) -> List[str]:
    """解析本次查询的仓库范围；越权抛 RepoNotVisible。"""
    visible = visible_repo_keys(user)
    if not repo:
        return visible
    from src.repo_key import to_repo_key
    want = {to_repo_key(repo), to_repo_key(repo, "github")}
    hit = sorted(want & set(visible))
    if not hit:
        raise RepoNotVisible(repo)
    return hit


def _cred_visibility(user: Dict[str, Any], repo_keys: List[str]) -> Tuple[str, List[Any]]:
    """设备/引擎可见性 WHERE 片段 —— 与 dashboard.ops_overview 的 runtime_credentials 口径一致：
    非个人组织按 org_id 隔离；指定仓库时 global + 自己的 user + 该仓的 worker；否则 global + 自己的。
    """
    _uname = user.get("username") or ""
    org_id = int(user.get("org_id") or 0)
    _is_personal = False
    if org_id:
        try:
            from src.user.org_db import is_org_personal
            _is_personal = is_org_personal(org_id)
        except Exception:
            _is_personal = False
    if org_id and not _is_personal:
        return " AND org_id=?", [org_id]
    if repo_keys:
        ph = ",".join("?" * len(repo_keys))
        return f" AND (scope='global' OR (scope='user' AND owner_name=?) OR repo_key IN ({ph}))", \
               [_uname, *repo_keys]
    return " AND (scope='global' OR (scope='user' AND owner_name=?))", [_uname]


# ---------------------------------------------------------------- 取数

def _fetch_tasks(repo_keys: List[str]) -> List[Dict[str, Any]]:
    """范围内任务（不分页，窗口在 Python 侧过滤：时间格式跨方言，交给 normalize 处理）。"""
    if not repo_keys:
        return []
    from src.session_manager import db, db_lock
    ph = ",".join("?" * len(repo_keys))
    out: List[Dict[str, Any]] = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, status, repo_key, agent_type, created_at, started_at, completed_at"
                f" FROM agent_tasks WHERE repo_key IN ({ph})", tuple(repo_keys))
            cols = [d[0] for d in c.description]
            out = [dict(zip(cols, r)) for r in c.fetchall()]
    except Exception:
        return []
    return out


def _fetch_usage(repo_keys: List[str]) -> List[Dict[str, Any]]:
    """范围内 LLM 用量台账行（table: obs_llm_usage）。"""
    if not repo_keys:
        return []
    from src.session_manager import db, db_lock
    ph = ",".join("?" * len(repo_keys))
    out: List[Dict[str, Any]] = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT model_key, source, created_at,"
                " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens"
                f" FROM obs_llm_usage WHERE repo_key IN ({ph})", tuple(repo_keys))
            cols = [d[0] for d in c.description]
            out = [dict(zip(cols, r)) for r in c.fetchall()]
    except Exception:
        return []
    return out


def _fetch_engines(user: Dict[str, Any], repo_keys: List[str]) -> Tuple[int, int]:
    """(在线设备数, 总数) —— 按可见性口径过滤 runtime_credentials。"""
    frag, params = _cred_visibility(user, repo_keys)
    from src.session_manager import db, db_lock
    total = online = 0
    try:
        from src.runtime.health import derive_runtime_health
    except Exception:
        derive_runtime_health = None
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT status, last_seen_at FROM runtime_credentials WHERE status != 'revoked'" + frag,
                tuple(params))
            now = datetime.now(timezone.utc)
            _now_ts = now.timestamp()
            for status, last_seen in c.fetchall():
                total += 1
                if derive_runtime_health is not None:
                    try:
                        if derive_runtime_health(status or "offline", last_seen, _now_ts) == "online":
                            online += 1
                    except Exception:
                        pass
                elif (status or "") == "online":
                    online += 1
    except Exception:
        return 0, 0
    return online, total


# ---------------------------------------------------------------- 聚合

def build_overview(user: Dict[str, Any], repo: str = "", range_str: str = DEFAULT_RANGE) -> Dict[str, Any]:
    """总览聚合。越权仓库抛 RepoNotVisible（路由层转 404）。"""
    if range_str not in RANGE_SPECS:
        range_str = DEFAULT_RANGE
    span, bucket = RANGE_SPECS[range_str]
    now = datetime.now(timezone.utc)
    start = now - timedelta(seconds=span)

    repo_keys = _scope_keys(user, repo)
    tasks = _fetch_tasks(repo_keys)
    usage = _fetch_usage(repo_keys)
    engines_online, engines_total = _fetch_engines(user, repo_keys)

    # ---- 任务：窗口内 + 分桶 ----
    nbuckets = max(1, span // bucket)
    buckets = [start + timedelta(seconds=i * bucket) for i in range(nbuckets)]
    bx = {b.isoformat(): i for i, b in enumerate(buckets)}
    completed = [0] * nbuckets
    failed = [0] * nbuckets
    cancelled = [0] * nbuckets

    queue_depth = 0
    finished_in_window: List[Dict[str, Any]] = []
    durations: List[float] = []
    for t in tasks:
        st = (t.get("status") or "").lower()
        if st in ("queued", "dispatched"):
            queue_depth += 1
        cdt = _dt(t.get("created_at"))
        if cdt and start <= cdt <= now:
            i = bx.get(_bucket_start(cdt, start, bucket).isoformat())
            if i is not None:
                if st == "completed":
                    completed[i] += 1
                elif st == "failed":
                    failed[i] += 1
                elif st == "cancelled":
                    cancelled[i] += 1
        # 完成情况看 completed_at（决定成功率与耗时）
        fdt = _dt(t.get("completed_at"))
        if fdt and start <= fdt <= now:
            finished_in_window.append(t)
            sdt = _dt(t.get("started_at"))
            if sdt:
                durations.append((fdt - sdt).total_seconds())

    done = sum(1 for t in finished_in_window if (t.get("status") or "").lower() == "completed")
    bad = sum(1 for t in finished_in_window
              if (t.get("status") or "").lower() in ("failed", "cancelled"))
    success_rate = round(done * 100.0 / (done + bad), 1) if (done + bad) else None

    rate = []
    for i in range(nbuckets):
        tot = completed[i] + failed[i] + cancelled[i]
        rate.append(round(completed[i] * 100.0 / tot, 1) if tot else None)

    # ---- Token：窗口内 + 分桶 ----
    t_in = [0] * nbuckets
    t_out = [0] * nbuckets
    t_cache = [0] * nbuckets
    model_agg: Dict[str, int] = {}
    tokens_total = 0
    for u in usage:
        cdt = _dt(u.get("created_at"))
        _i = None
        if cdt and start <= cdt <= now:
            _i = bx.get(_bucket_start(cdt, start, bucket).isoformat())
        i_in = int(u.get("input_tokens") or 0)
        i_out = int(u.get("output_tokens") or 0)
        i_c = int(u.get("cache_read_tokens") or 0) + int(u.get("cache_write_tokens") or 0)
        i_r = int(u.get("reasoning_tokens") or 0)
        tot = i_in + i_out + i_c + i_r
        tokens_total += tot
        mk = u.get("model_key") or "unknown"
        model_agg[mk] = model_agg.get(mk, 0) + tot
        if _i is not None:
            t_in[_i] += i_in
            t_out[_i] += i_out + i_r          # reasoning 并入 output（多数厂商按 output 计价）
            t_cache[_i] += i_c

    model_share = []
    for mk, tot in sorted(model_agg.items(), key=lambda kv: -kv[1])[:8]:
        model_share.append({
            "model": mk, "tokens": tot,
            "share": round(tot * 100.0 / tokens_total, 1) if tokens_total else 0,
        })

    charts = {
        "task_outcome": {
            "buckets": [b.isoformat() for b in buckets],
            "completed": completed, "failed": failed, "cancelled": cancelled, "rate": rate,
        },
        "token_trend": {
            "buckets": [b.isoformat() for b in buckets],
            "input": t_in, "output": t_out, "cache": t_cache,
        },
        "model_share": model_share,
        "source": "live",           # live=直查原始表；rollup=读聚合表（P1-2）
    }
    # P1-2：趋势优先读 rollup（跨机可见、重启不丢、表大了也不慢）；无数据则回退直查
    try:
        from src.observability import metric_rollup
        dims = [_repo_dim(k) for k in repo_keys] if len(repo_keys) <= 200 else []
        rolled = metric_rollup.overview_trends(dims, start, bucket, nbuckets) if dims else None
        if rolled:
            charts["task_outcome"] = rolled["task_outcome"]
            charts["token_trend"] = rolled["token_trend"]
            charts["source"] = "rollup"
    except Exception as e:
        log.warning(f"obs overview: rollup trends failed, fallback to live: {e}")

    return {
        "range": range_str,
        "bucket_seconds": bucket,
        "generated_at": now.isoformat(),
        "scope": {
            "repo": repo or "",
            "repos": len(repo_keys),
            "restricted": True,
        },
        "kpis": {
            "queue_depth": queue_depth,
            "success_rate": success_rate,
            "p95_duration_s": _p95(durations),
            "engines_online": engines_online,
            "engines_total": engines_total,
            "tokens": tokens_total,
        },
        "charts": charts,
    }