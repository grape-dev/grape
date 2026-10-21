"""可观测性（Observability）API —— 面向用户的观测接口。

设计：docs/weekly-07/observability-redesign-design.md（§5.3 可见性边界）
       docs/weekly-07/observability-overview-page-design.md

边界铁律（对所有 /api/obs/* 生效）：
1. **可见范围后端强制**：先解析调用者可见的仓库/设备范围，再拼查询条件；
   越权对象一律 404（不是 403 —— 避免探测存在性）。
2. **不提供平台基础设施接口**：DB 连接池、慢查询、主机拓扑、全租户数据一律不给，
   归 admin 健康中心（L2）。L1 只看用户自己的资产。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.api.deps import require_org_member
from src.api.routes.runtime import _require_credential
from src.config import log
from src.observability.overview import DEFAULT_RANGE, RANGE_SPECS, RepoNotVisible, build_overview

router = APIRouter(prefix="/api/obs", tags=["observability"])


@router.get(
    "/overview",
    operation_id="obs_overview",
    summary="Observability overview aggregates",
    description=(
        "Aggregated overview for the Observability section (KPIs, task outcome trend, "
        "token usage trend, model share). Results are always restricted to the caller's "
        "visible repositories; an out-of-scope ?repo returns 404. "
        "Platform infrastructure metrics (DB pools, host topology) are intentionally excluded."
    ),
)
def obs_overview(
    repo: str = "",
    range: str = DEFAULT_RANGE,
    user: dict = Depends(require_org_member),
):
    """总览聚合。单请求返回全部卡片数据，避免首屏 N 个并发请求（设计文档 §6）。"""
    _range = range if range in RANGE_SPECS else DEFAULT_RANGE
    try:
        return build_overview(user, repo=repo, range_str=_range)
    except RepoNotVisible:
        # 越权 → 404：不区分"不存在"与"无权限"
        raise HTTPException(status_code=404, detail={"code": 404, "message": "repo not found"})
    except Exception as e:
        log.warning(f"obs/overview failed user={user.get('id')} repo={repo!r} range={_range}: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "overview failed"})


# ============================================================ 告警（§9）

@router.get(
    "/alerts",
    operation_id="obs_alerts",
    summary="Observability alerts (banner + panel)",
    description=(
        "Alert banner and panel data for the Observability overview: firing summary plus "
        "recent events and the rule list. User-facing results only ever contain "
        "level='user' events on repositories visible to the caller; platform-level rules "
        "(DB pool, aborted connects, all-runtimes-offline, HTTP 5xx) are never returned here."
    ),
)
def obs_alerts(
    status: str = "",
    limit: int = 50,
    repo: str = "",
    user: dict = Depends(require_org_member),
):
    """告警横幅 + 面板数据（一次拿全，面板内切换子视图不再请求）。"""
    from src.observability import alerts as alerts_mod
    try:
        _limit = max(1, min(int(limit or 50), 200))
        return {
            "summary": alerts_mod.firing_summary(user),
            "events": alerts_mod.list_events(user, status=status, limit=_limit, repo=repo),
            "rules": alerts_mod.list_rules(level="user"),
        }
    except RepoNotVisible:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "repo not found"})
    except Exception as e:
        log.warning(f"obs/alerts failed user={user.get('id')}: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "alerts failed"})


@router.post(
    "/alerts/{event_id}/silence",
    operation_id="obs_alert_silence",
    summary="Silence an alert event",
    description=("Silence an alert event for N minutes (notifications suppressed, the event "
                 "is still recorded). Only events on the caller's visible repositories can be "
                 "silenced; anything else returns 404."),
)
def obs_alert_silence(
    event_id: int,
    minutes: int = 60,
    user: dict = Depends(require_org_member),
):
    from src.observability import alerts as alerts_mod
    ok = alerts_mod.silence_event(event_id, minutes, user)
    if not ok:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "event not found"})
    return {"ok": True, "event_id": event_id, "minutes": max(1, int(minutes))}


@router.post(
    "/alerts/rules/{rule_id}/toggle",
    operation_id="obs_alert_rule_toggle",
    summary="Enable/disable an alert rule",
    description="Enable or disable a user-facing alert rule.",
)
def obs_alert_rule_toggle(
    rule_id: int,
    enabled: bool = True,
    user: dict = Depends(require_org_member),
):
    from src.observability import alerts as alerts_mod
    # 只允许操作 level='user' 的规则（平台级规则属 L2）
    ids = {int(r["id"]) for r in alerts_mod.list_rules(level="user")}
    if int(rule_id) not in ids:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "rule not found"})
    if not alerts_mod.set_rule_enabled(rule_id, bool(enabled)):
        raise HTTPException(status_code=500, detail={"code": 500, "message": "toggle failed"})
    return {"ok": True, "rule_id": rule_id, "enabled": bool(enabled)}


@router.post(
    "/alerts/rules/{rule_id}/threshold",
    operation_id="obs_alert_rule_threshold",
    summary="Update an alert rule threshold",
    description="Update the threshold of a user-facing alert rule.",
)
def obs_alert_rule_threshold(
    rule_id: int,
    threshold: float,
    user: dict = Depends(require_org_member),
):
    from src.observability import alerts as alerts_mod
    ids = {int(r["id"]) for r in alerts_mod.list_rules(level="user")}
    if int(rule_id) not in ids:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "rule not found"})
    if not alerts_mod.set_rule_threshold(rule_id, float(threshold)):
        raise HTTPException(status_code=500, detail={"code": 500, "message": "update failed"})
    return {"ok": True, "rule_id": rule_id, "threshold": float(threshold)}


@router.post(
    "/alerts/rules/{rule_id}/notify",
    operation_id="obs_alert_rule_notify",
    summary="Set alert rule notification channels",
    description=("Set the notification channels of a user-facing alert rule. "
                 "Comma-separated: 'feishu' (private message via the recipient's own chat bot), "
                 "'webhook' (OBS_ALERT_WEBHOOK_URL). Empty = record only, never push."),
)
def obs_alert_rule_notify(
    rule_id: int,
    channels: str = "",
    user: dict = Depends(require_org_member),
):
    from src.observability import alerts as alerts_mod
    ids = {int(r["id"]) for r in alerts_mod.list_rules(level="user")}
    if int(rule_id) not in ids:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "rule not found"})
    _allowed = {"feishu", "webhook"}
    _chans = [c.strip() for c in (channels or "").split(",") if c.strip()]
    bad = [c for c in _chans if c not in _allowed]
    if bad:
        raise HTTPException(status_code=400, detail={"code": 400,
                                                    "message": f"unsupported channel: {','.join(bad)}"})
    if not alerts_mod.set_rule_notify(rule_id, ",".join(_chans)):
        raise HTTPException(status_code=500, detail={"code": 500, "message": "update failed"})
    return {"ok": True, "rule_id": rule_id, "channels": ",".join(_chans)}


@router.post(
    "/alerts/test-notify",
    operation_id="obs_alert_test_notify",
    summary="Send a test alert to myself",
    description=("Send a test alert message to the caller's own IM identity (via their own chat bot) "
                 "so the notification channel can be verified end to end. Returns reason=no_open_id / "
                 "no_bot / send_failed when it cannot be delivered."),
)
async def obs_alert_test_notify(user: dict = Depends(require_org_member)):
    from src.observability import alerts as alerts_mod
    try:
        return await alerts_mod.send_test_notification(user)
    except Exception as e:
        log.warning(f"obs/alerts/test-notify failed user={user.get('id')}: {e}")
        return {"ok": False, "reason": "exception", "message": str(e)}


# ============================================================ 日志（§6.3）

class LogRecordBody(BaseModel):
    ts: str = ""
    level: str = ""
    msg: str = ""
    component: str = ""
    task_id: str = ""
    repo_key: str = ""
    trace_id: str = ""
    runtime_id: int = 0


class LogIngestBody(BaseModel):
    records: list[LogRecordBody] = []
    runtime_id: int = 0


@router.post(
    "/logs/ingest",
    operation_id="obs_logs_ingest",
    summary="Ingest worker logs (WARNING/ERROR)",
    description=(
        "Batch ingest of WARNING/ERROR log lines from a worker (authenticated with the worker's "
        "rta_ credential). INFO is intentionally dropped — it stays in the local file logs. "
        "Secrets are redacted before storage; task/repo context is extracted from the message "
        "so the lines become visible under the owning repository."
    ),
)
def obs_logs_ingest(body: LogIngestBody, credential_id: int = Depends(_require_credential)):
    from src.observability import logs as logs_mod

    src = f"worker-{credential_id}"
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT name, device_name FROM runtime_credentials WHERE id=?", (credential_id,))
            row = c.fetchone()
            if row:
                src = row[1] or row[0] or src
    except Exception:
        pass

    recs = []
    for r in body.records:
        d = r.model_dump() if hasattr(r, "model_dump") else r.dict()
        if not d.get("runtime_id"):
            d["runtime_id"] = int(body.runtime_id or 0)
        d["source"] = src
        recs.append(d)
    n = logs_mod.ingest(recs, default_source=src)
    return {"ok": True, "ingested": n, "received": len(recs), "source": src}


@router.get(
    "/logs",
    operation_id="obs_logs",
    summary="Search observability logs",
    description=(
        "Search WARNING/ERROR logs stored on the master (cross-machine). Results are restricted "
        "to repositories visible to the caller; rows without repository context (process/platform "
        "logs) are never returned here. Filters: level, source, repo, task_id, trace_id, keyword, "
        "time range; newest first."
    ),
)
def obs_logs(
    level: str = "",
    source: str = "",
    repo: str = "",
    task_id: str = "",
    trace_id: str = "",
    q: str = "",
    since: str = "",
    until: str = "",
    limit: int = 100,
    offset: int = 0,
    user: dict = Depends(require_org_member),
):
    from src.observability import logs as logs_mod
    try:
        return logs_mod.query(user, level=level, source=source, repo=repo, task_id=task_id,
                              trace_id=trace_id, q=q, since=since, until=until,
                              limit=limit, offset=offset)
    except RepoNotVisible:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "repo not found"})
    except Exception as e:
        log.warning(f"obs/logs failed user={user.get('id')}: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "logs failed"})


@router.get(
    "/logs/sources",
    operation_id="obs_logs_sources",
    summary="Log sources available in the caller's scope",
    description="Distinct log sources (master / worker names) present in the caller's visible scope.",
)
def obs_logs_sources(user: dict = Depends(require_org_member)):
    from src.observability import logs as logs_mod
    try:
        res = logs_mod.query(user, limit=1)
        return {"sources": (res.get("facets") or {}).get("sources", []),
                "levels": (res.get("facets") or {}).get("levels", {})}
    except Exception as e:
        log.warning(f"obs/logs/sources failed: {e}")
        return {"sources": [], "levels": {}}


# ============================================================ 追踪 / 时间线（§6.4）

@router.get(
    "/trace/{trace}",
    operation_id="obs_trace",
    summary="Task trace timeline",
    description=(
        "Rebuild a task's execution timeline (no OTel): enqueue → dispatch → engine steps "
        "(task_messages) → terminal, plus token usage, events and matching log lines. "
        "Accepts either a task-level trace id or a pipeline-level one (all tasks of the same "
        "issue share a pipeline trace). Restricted to the caller's visible repositories; "
        "an unknown or out-of-scope trace returns 404."
    ),
)
def obs_trace(trace: str, with_steps: bool = True, user: dict = Depends(require_org_member)):
    from src.observability import trace as trace_mod
    try:
        data = trace_mod.timeline(user, trace, with_steps=with_steps)
    except Exception as e:
        log.warning(f"obs/trace failed user={user.get('id')} trace={trace!r}: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "trace failed"})
    if not data.get("tasks"):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "trace not found"})
    return data


@router.get(
    "/trace",
    operation_id="obs_trace_by_issue",
    summary="Pipeline timeline by issue",
    description=(
        "Timeline of the whole development flow of one issue (all tasks sharing the pipeline "
        "trace id). Same visibility rules as /trace/{trace}."
    ),
)
def obs_trace_by_issue(
    repo: str = Query(...),
    issue_id: str = Query(...),
    user: dict = Depends(require_org_member),
):
    from src.observability import trace as trace_mod
    try:
        data = trace_mod.pipeline_by_issue(user, repo, issue_id)
    except Exception as e:
        log.warning(f"obs/trace by issue failed: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "trace failed"})
    if not data.get("tasks"):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "trace not found"})
    return data


@router.post(
    "/trace/stamp",
    operation_id="obs_trace_stamp",
    summary="Backfill trace ids for legacy tasks",
    description="Backfill trace_id / pipeline_trace_id for tasks created before tracing existed.",
)
def obs_trace_stamp(user: dict = Depends(require_org_member)):
    from src.observability import trace as trace_mod
    n = trace_mod.stamp_missing()
    return {"ok": True, "stamped": n}


# ============================================================ 设备资源曲线（设备分区）

@router.get(
    "/runtimes/history",
    operation_id="obs_runtime_history",
    summary="Engine resource history (devices section)",
    description=(
        "Per-engine resource history (CPU / memory / disk / health score) for the Devices "
        "section resource curves. Only engines visible to the caller are returned "
        "(same visibility rule as the device list); an unknown or out-of-scope ?runtime_id "
        "returns 404."
    ),
)
def obs_runtime_history(
    range: str = "6h",
    runtime_id: int = 0,
    repo: str = "",
    user: dict = Depends(require_org_member),
):
    """设备资源历史（采样表 obs_runtime_samples）。"""
    from src.observability import rollup
    from src.observability.overview import RepoNotVisible, _cred_visibility, _scope_keys

    try:
        repo_keys = _scope_keys(user, repo)
    except RepoNotVisible:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "repo not found"})

    frag, params = _cred_visibility(user, repo_keys)
    runtimes = []
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, device_name, name FROM runtime_credentials"
                      " WHERE status != 'revoked'" + frag + " ORDER BY id", tuple(params))
            runtimes = [{"id": int(r[0]), "device_name": r[1] or "", "name": r[2] or ""}
                        for r in c.fetchall()]
    except Exception as e:
        log.warning(f"obs/runtimes/history list failed: {e}")

    visible_ids = [r["id"] for r in runtimes]
    if runtime_id:
        if int(runtime_id) not in visible_ids:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
        visible_ids = [int(runtime_id)]

    try:
        data = rollup.runtime_history(visible_ids, range_str=range)
    except Exception as e:
        log.warning(f"obs/runtimes/history failed user={user.get('id')}: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "history failed"})

    data["runtimes"] = [r for r in runtimes if r["id"] in set(visible_ids)]
    return data

@router.get(
    "/events",
    operation_id="obs_events",
    summary="Unified event view (multi-source)",
    description=(
        "Unified event stream (design §6.9): alert rules (monitor), data gaps, mutations, "
        "task incidents (stuck/failed) and change events (merged PRs) normalized into one "
        "shape with first-seen time, duration and a suggested next step. Results are always "
        "restricted to the caller's visible repositories and devices; platform-level alerts "
        "and platform audit actions are never returned here (the latter live in the admin API). "
        "The `q` parameter is a server-side keyword filter (alert rule name/dim key, change "
        "title/actor/issue, task fields) — it searches the whole source, not just the loaded page. "
        "Pagination uses an opaque cursor: pass the `next_cursor` of the previous page back as "
        "`before` (null `next_cursor` means the end). `truncated_sources` lists sources whose "
        "fetch window was saturated, i.e. older items may exist beyond the returned cursor."
    ),
)
def obs_events(
    sources: str = "",
    state: str = "",
    repo: str = "",
    limit: int = 100,
    q: str = "",
    before: str = "",
    user: dict = Depends(require_org_member),
):
    """统一事件视图（用户面）：来源/状态/仓库/关键字过滤，含首次发生与已持续时长。

    关键字 `q` 由**服务端检索**（不是前端过滤已加载页）：告警来源下沉到 SQL 的
    `rule_name/dim_key`，变更到 `title/actor/issue_id`，任务来源在服务端对这一来源的
    当前卡住/失败列表过滤。空则不过滤。
    """
    from src.observability import events_view
    from src.observability.overview import RepoNotVisible

    _limit = max(1, min(int(limit or 100), 500))
    want = [s.strip() for s in str(sources or "").split(",") if s.strip()]
    try:
        return events_view.unified_events(user, sources=want, state=state, repo=repo,
                                          limit=_limit, allow_audit=False, q=q, before=before)
    except RepoNotVisible:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "repo not found"})
    except Exception as e:
        log.warning(f"obs/events failed user={user.get('id')}: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "events failed"})


@router.get(
    "/metrics/series",
    operation_id="obs_metrics_series",
    summary="Business metric time series (from the rollup table)",
    description=(
        "Minute/hour time series for business metrics (task outcome, success rate, queue wait, "
        "duration, token usage) read from obs_metric_rollup (design §6.2). Series are aggregated "
        "over the caller's visible repositories; there is no platform-wide ('global') series on "
        "the user-facing side — use the admin API for that."
    ),
)
def obs_metrics_series(
    metric: str = "task_success_rate,task_done,task_failed",
    repo: str = "",
    hours: int = 6,
    resolution: str = "minute",
    user: dict = Depends(require_org_member),
):
    """业务指标序列（用户面）：只覆盖调用者可见仓库；越权仓库 → 404。"""
    from datetime import datetime, timedelta, timezone
    from src.observability import metric_rollup
    from src.observability.overview import RepoNotVisible, _scope_keys

    res = (resolution or "minute").strip().lower()
    if res not in (metric_rollup.RES_MINUTE, metric_rollup.RES_HOUR):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "resolution must be minute|hour"})
    mets = [m.strip() for m in str(metric or "").split(",") if m.strip()]
    bad = [m for m in mets if m not in metric_rollup.ALL_METRICS]
    if not mets or bad:
        raise HTTPException(status_code=400, detail={"code": 400,
                                                     "message": f"unknown metric: {bad or 'empty'}"})
    try:
        repo_keys = _scope_keys(user, repo)
    except RepoNotVisible:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "repo not found"})
    span = max(1, min(int(hours or 6), 24 * 40))
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=span)
    dims = [f"repo:{k}" for k in repo_keys]
    data = metric_rollup.series(mets, dims, start, now, res)
    return {"resolution": res, "hours": span, "dim_keys": dims, "series": data,
            "scanned_repos": len(repo_keys), "global": False}


# ===== SLO / 错误预算（P2-1，§6.7）=====

@router.get(
    "/slo",
    operation_id="obs_slo_list",
    summary="SLO / error budget (caller's visible repositories)",
    description=(
        "Health conclusions for the caller: per-SLO state (ok/warning/breached/no_data), current "
        "value, target and remaining error budget. Buckets are hourly; the bad-bucket ratio method "
        "gives the budget. Engine-availability SLOs are platform targets and are only exposed on "
        "the admin API. Scope is always the caller's visible repositories (never platform-wide)."
    ),
)
def obs_slo_list(repo: str = "", user: dict = Depends(require_org_member)):
    """SLO 列表（用户面）：范围是可见仓库；`repo` 给了就只看这个仓（越权 → 404）。

    与总览页其它 KPI/趋势图同一口径：页面选了仓库，这里也只看那个仓，避免"别的数字跟着仓变、
    只有 SLO 卡片不变"的错觉。
    """
    from src.observability import slo as slo_mod
    from src.observability.overview import RepoNotVisible
    try:
        return {"slos": slo_mod.list_summaries(user=user, global_scope=False, repo=repo),
                "global": False, "repo": str(repo or "")}
    except RepoNotVisible:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "repo not found"})
    except Exception as e:
        log.warning(f"obs/slo failed user={user.get('id')}: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "slo failed"})


@router.get(
    "/slo/{slo_id}",
    operation_id="obs_slo_detail",
    summary="One SLO with its burn-down series",
    description="Same scope rules as the list; includes the burn series (ideal vs used budget %).",
)
def obs_slo_detail(slo_id: int, repo: str = "", user: dict = Depends(require_org_member)):
    """单条 SLO（含燃尽序列）；不存在/不可见/越权仓库 → 404。"""
    from src.observability import slo as slo_mod
    from src.observability.overview import RepoNotVisible
    try:
        data = slo_mod.detail(int(slo_id), user=user, global_scope=False, repo=repo)
    except RepoNotVisible:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "repo not found"})
    except Exception as e:
        log.warning(f"obs/slo/{slo_id} failed user={user.get('id')}: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "slo failed"})
    if not data:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "slo not found"})
    return data


# ===== Token 三级归因（引擎 / 任务 / 阶段事件）=====

def _obs_token_engine_ids(user: dict, repo: str = "") -> list:
    """调用者可见的引擎 id 列表（设备维度可见性），拿不到就返回空（不降级）。"""
    try:
        from src.observability.overview import visible_repo_keys, visible_runtime_dim_keys
        keys = sorted(visible_repo_keys(user or {}))
        dims = visible_runtime_dim_keys(user or {}, keys)
        return [int(str(d).split(":", 1)[1]) for d in dims if str(d).startswith("runtime:")]
    except Exception as e:
        log.warning(f"obs tokens visible engines failed: {e}")
        return []


def _obs_token_scope(repo: str, user: dict) -> None:
    """越权仓库 → 404（口径同事件视图/指标序列）。"""
    if not repo:
        return
    from src.observability.overview import RepoNotVisible, visible_repo_keys
    from src.repo_key import to_repo_key
    want = {to_repo_key(repo), to_repo_key(repo, "github")}
    if not (want & set(visible_repo_keys(user or {}))):
        raise RepoNotVisible(repo)


@router.get("/tokens/engines", operation_id="obs_tokens_engines",
            summary="Token usage per engine (caller's visible engines/repos)")
def obs_tokens_engines(range: str = "24h", repo: str = "", user: dict = Depends(require_org_member)):
    """每个引擎的 token 消耗（用户面：可见引擎 ∪ 可见仓库）。"""
    from src.observability import tokens as tok
    from src.observability.overview import RepoNotVisible
    try:
        _obs_token_scope(repo, user)
        return tok.engine_totals(user=user, repo=repo, range_str=range,
                                 engine_ids=_obs_token_engine_ids(user, repo))
    except RepoNotVisible:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "repo not found"})
    except Exception as e:
        log.warning(f"obs/tokens/engines failed user={user.get('id')}: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "tokens failed"})


@router.get("/tokens/tasks", operation_id="obs_tokens_tasks",
            summary="Token usage per task/run")
def obs_tokens_tasks(range: str = "24h", repo: str = "", limit: int = 100, run_kind: str = "",
                     user: dict = Depends(require_org_member)):
    """每个任务/运行的 token 消耗（按 total 降序）。"""
    from src.observability import tokens as tok
    from src.observability.overview import RepoNotVisible
    try:
        _obs_token_scope(repo, user)
        return tok.task_totals(user=user, repo=repo, range_str=range, run_kind=run_kind,
                               limit=max(1, min(int(limit or 100), 500)))
    except RepoNotVisible:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "repo not found"})
    except Exception as e:
        log.warning(f"obs/tokens/tasks failed user={user.get('id')}: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "tokens failed"})


@router.get("/tokens/issues/{issue_id}/stages", operation_id="obs_tokens_issue_stages",
            summary="Token usage per stage event of one issue (e.g. vod -> issue)")
def obs_tokens_issue_stages(issue_id: str, range: str = "30d", repo: str = "",
                            user: dict = Depends(require_org_member)):
    """单个 issue 的阶段事件 token（一个 issue 走 vod→issue 就是两行）。"""
    from src.observability import tokens as tok
    from src.observability.overview import RepoNotVisible
    try:
        _obs_token_scope(repo, user)
        return tok.issue_stages(issue_id, user=user, repo=repo, range_str=range)
    except RepoNotVisible:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "repo not found"})
    except Exception as e:
        log.warning(f"obs/tokens/issues failed user={user.get('id')}: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "tokens failed"})


@router.get("/tokens/stages", operation_id="obs_tokens_stages",
            summary="Token usage grouped by issue x stage (visible scope)")
def obs_tokens_stages(range: str = "24h", repo: str = "", user: dict = Depends(require_org_member)):
    from src.observability import tokens as tok
    from src.observability.overview import RepoNotVisible
    try:
        _obs_token_scope(repo, user)
        return tok.stage_totals(user=user, repo=repo, range_str=range)
    except RepoNotVisible:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "repo not found"})
    except Exception as e:
        log.warning(f"obs/tokens/stages failed user={user.get('id')}: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "tokens failed"})
