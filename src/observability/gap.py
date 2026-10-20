"""数据缺失（Data Gap）检测（P1-3）—— "**该报数却没报**"。

为什么单独做：静默失效最危险 —— 一切看起来正常（没有指标、没有错误），只是**数据不再来了**。
观测云把 Data Gap 单列为事件级别不是偶然。

两类探测（规则 `kind='gap'`，由 `metric` 指定探测目标）：

| `metric` | 判定 | 依据 |
|---|---|---|
| `gap_runtime` | 引擎/设备超过 `threshold` 秒没有心跳 | `runtime_credentials.last_seen_at` |
| `gap_repo` | 近 `window_sec` 有任务活动的仓库，最近 `threshold` 秒**没有任何任务活动** | `agent_tasks` 的 created/started/completed |

口径与取舍（避免变成噪音源）：

1. **从未上报过**（`last_seen_at` 为空）→ 不算缺失（那是没装成/没用过，不是"停止上报"）
2. 超过 `GAP_MAX_STALE_SEC`（7 天）没上报 → 视为**已退役凭证**，不再产生事件
   （否则一条死凭证据会永久刷告警）；但 L2 的**静默名单仍会列出它们**（`silent_targets`），供运维清理
3. `repo_silent` 种子规则**默认关闭**：低活跃仓库天然长期无任务，开启前先看该仓库的历史频率
4. 事件与指标事件**同表同生命周期**（`obs_alert_events`，`source='gap'`）：`dim_key` 唯一
   （`runtime:<id>` / `repo:<key>`），数据恢复即 `resolved`
5. `first_fired_at` = **静默起点**（最后一次数据时间 + threshold），所以"已持续"就是静默时长；
   `value` 记静默秒数（便于按严重度排序）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.config import log

#: 超过这个时长没上报 → 视为已退役凭证，不再报缺失（仍出现在 L2 静默名单里）
GAP_MAX_STALE_SEC = 7 * 24 * 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _dt(raw: Any) -> Optional[datetime]:
    try:
        s = str(raw or "").strip().replace("Z", "+00:00")
        if not s:
            return None
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _gap_rules() -> List[Dict[str, Any]]:
    from src.observability import alerts as al
    return [r for r in al.list_rules()
            if (r.get("kind") or "metric") == "gap" and int(r.get("enabled") or 0) == 1]


# ---------------------------------------------------------------- 探测

def probe_all(open_cache=None) -> Dict[str, int]:
    """跑一轮数据缺失探测（由 30s 告警循环调用）。返回 {gap_fired, gap_resolved, gap_evaluated}。"""
    out = {"gap_fired": 0, "gap_resolved": 0, "gap_evaluated": 0}
    try:
        rules = _gap_rules()
    except Exception as e:
        log.warning(f"obs gap: load rules failed: {e}")
        return out
    if not rules:
        return out
    for r in rules:
        target = str(r.get("metric") or "")
        try:
            if target == "gap_runtime":
                f, v, ev = _probe_runtimes(r, open_cache)
            elif target == "gap_repo":
                f, v, ev = _probe_repos(r, open_cache)
            else:
                continue
            out["gap_fired"] += f
            out["gap_resolved"] += v
            out["gap_evaluated"] += ev
        except Exception as e:
            log.warning(f"obs gap probe {target} failed: {e}")
    if out["gap_fired"] or out["gap_resolved"]:
        log.info(f"obs gap: {out}")
    return out


def _probe_runtimes(rule: Dict[str, Any], open_cache=None) -> tuple:
    """设备/引擎静默：last_seen_at 落后超过 threshold 秒。"""
    from src.observability import alerts as al
    from src.session_manager import db, db_lock

    thr = float(rule.get("threshold") or 900)
    now = _now()
    fired = resolved = evaluated = 0
    rows: List[tuple] = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, device_name, name, last_seen_at, repo_key, org_id, scope"
                      " FROM runtime_credentials WHERE status != 'revoked'")
            rows = list(c.fetchall())
    except Exception as e:
        log.warning(f"obs gap runtime list failed: {e}")
        return 0, 0, 0

    seen: List[str] = []
    with db_lock:
        c = db.cursor()
        for rid, device, name, last_seen, repo_key, _org_id, _scope in rows:
            evaluated += 1
            dim = f"runtime:{int(rid)}"
            seen.append(dim)
            ls = _dt(last_seen)
            open_ev = al._open_event(c, int(rule["id"]), dim, open_cache)
            if ls is None:
                # 从未上报过（没装成/没用过）→ 不算缺失
                if al._resolve_open(c, int(rule["id"]), dim, now, open_cache):
                    resolved += 1
                continue
            silent_sec = (now - ls).total_seconds()
            if silent_sec > thr and silent_sec <= GAP_MAX_STALE_SEC:
                silent_since = ls + timedelta(seconds=thr)
                if open_ev is None:
                    c.execute(
                        "INSERT INTO obs_alert_events (rule_id, rule_name, level, severity, status,"
                        " source, metric, value, threshold, dim_key, repo_key, first_fired_at,"
                        " last_seen_at) VALUES (?,?,?,?,'firing','gap',?,?,?,?,?,?,?)",
                        (int(rule["id"]), rule["name"], rule.get("level") or "user",
                         rule.get("severity") or "warning", "gap_runtime",
                         round(silent_sec, 1), thr, dim, repo_key or "",
                         _iso(silent_since), _iso(now)))
                    fired += 1
                    log.info(f"obs gap: 设备静默 {device or name or rid} "
                             f"最后上报 {last_seen}（已 {silent_sec / 60:.0f} 分钟）")
                else:
                    c.execute("UPDATE obs_alert_events SET value=?, last_seen_at=?, updated_at=?"
                              " WHERE id=?",
                              (round(silent_sec, 1), _iso(now), _iso(now), open_ev["id"]))
            else:
                if al._resolve_open(c, int(rule["id"]), dim, now, open_cache):
                    resolved += 1
        c.execute("SELECT dim_key FROM obs_alert_events WHERE rule_id=? AND status='firing'",
                  (int(rule["id"]),))
        for (dim_key,) in c.fetchall():
            if dim_key not in seen and al._resolve_open(c, int(rule["id"]), dim_key, now, open_cache):
                resolved += 1        # 设备被删除/退役 → 相关事件收口
        db.commit()
    return fired, resolved, evaluated


def _probe_repos(rule: Dict[str, Any], open_cache=None) -> tuple:
    """仓库静默（默认关闭）：近 window_sec 有活动，最近 threshold 秒完全没有任务活动。"""
    from src.observability import alerts as al
    from src.session_manager import db, db_lock

    thr = float(rule.get("threshold") or 3600)
    active_window = float(rule.get("window_sec") or 21600)
    now = _now()
    rows: List[tuple] = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT repo_key, created_at, started_at, completed_at FROM agent_tasks"
                      " WHERE repo_key IS NOT NULL AND repo_key != ''")
            rows = list(c.fetchall())
    except Exception as e:
        log.warning(f"obs gap repo list failed: {e}")
        return 0, 0, 0

    last_act: Dict[str, datetime] = {}
    for repo_key, created_at, started_at, completed_at in rows:
        for raw in (completed_at, started_at, created_at):
            dt = _dt(raw)
            if dt and (repo_key not in last_act or dt > last_act[repo_key]):
                last_act[repo_key] = dt

    fired = resolved = evaluated = 0
    seen: List[str] = []
    with db_lock:
        c = db.cursor()
        for repo_key, dt in last_act.items():
            evaluated += 1
            dim = f"repo:{repo_key}"
            seen.append(dim)
            idle = (now - dt).total_seconds()
            active_recently = idle <= active_window
            silent = idle > thr
            open_ev = al._open_event(c, int(rule["id"]), dim, open_cache)
            if active_recently and silent:
                if open_ev is None:
                    c.execute(
                        "INSERT INTO obs_alert_events (rule_id, rule_name, level, severity, status,"
                        " source, metric, value, threshold, dim_key, repo_key, first_fired_at,"
                        " last_seen_at) VALUES (?,?,?,?,'firing','gap',?,?,?,?,?,?,?)",
                        (int(rule["id"]), rule["name"], rule.get("level") or "user",
                         rule.get("severity") or "warning", "gap_repo", round(idle, 1), thr, dim,
                         repo_key, _iso(dt + timedelta(seconds=thr)), _iso(now)))
                    fired += 1
                    log.info(f"obs gap: 仓库静默 {repo_key} 最后活动 {_iso(dt)}"
                             f"（已 {idle / 60:.0f} 分钟无任务活动）")
                else:
                    c.execute("UPDATE obs_alert_events SET value=?, last_seen_at=?, updated_at=?"
                              " WHERE id=?", (round(idle, 1), _iso(now), _iso(now), open_ev["id"]))
            else:
                if al._resolve_open(c, int(rule["id"]), dim, now, open_cache):
                    resolved += 1
        c.execute("SELECT dim_key FROM obs_alert_events WHERE rule_id=? AND status='firing'",
                  (int(rule["id"]),))
        for (dim_key,) in c.fetchall():
            if dim_key not in seen and al._resolve_open(c, int(rule["id"]), dim_key, now, open_cache):
                resolved += 1
        db.commit()
    return fired, resolved, evaluated


# ---------------------------------------------------------------- L2 静默名单

def silent_targets(include_retired: bool = True) -> Dict[str, Any]:
    """全局静默名单（管理台 T-A5 用）：不套 7 天退役窗口，全部列出（供运维清理死凭证）。

    返回 {runtimes: [...], repos: [...], generated_at}；每项带 `beyond_retire_window`
    标记（= 已超过 7 天，不会产生事件，但仍需人工处理）。
    """
    from src.session_manager import db, db_lock
    now = _now()
    runtimes: List[Dict[str, Any]] = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, name, device_name, scope, owner_name, org_id, last_seen_at,"
                      " status, repo_key FROM runtime_credentials WHERE status != 'revoked'")
            for rid, name, device, scope, owner, org_id, last_seen, status, repo_key in c.fetchall():
                ls = _dt(last_seen)
                if ls is None:
                    runtimes.append({"id": int(rid), "name": name or "", "device_name": device or "",
                                     "scope": scope or "", "owner_name": owner or "",
                                     "org_id": int(org_id or 0), "last_seen_at": "",
                                     "silent_sec": None, "never_reported": True,
                                     "beyond_retire_window": False, "repo_key": repo_key or ""})
                    continue
                silent = (now - ls).total_seconds()
                runtimes.append({"id": int(rid), "name": name or "", "device_name": device or "",
                                 "scope": scope or "", "owner_name": owner or "",
                                 "org_id": int(org_id or 0), "last_seen_at": _iso(ls),
                                 "silent_sec": round(silent, 1), "never_reported": False,
                                 "beyond_retire_window": silent > GAP_MAX_STALE_SEC,
                                 "repo_key": repo_key or "", "status": status or ""})
    except Exception as e:
        log.warning(f"obs gap silent_targets runtime failed: {e}")

    repos: List[Dict[str, Any]] = []
    for r in _gap_rules():
        if str(r.get("metric") or "") != "gap_repo":
            continue
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT dim_key, value, repo_key, first_fired_at FROM obs_alert_events"
                          " WHERE rule_id=? AND status='firing' ORDER BY value DESC",
                          (int(r["id"]),))
                for dim_key, value, repo_key, first in c.fetchall():
                    repos.append({"dim_key": dim_key, "repo_key": repo_key or "",
                                  "silent_sec": value, "since": first})
        except Exception as e:
            log.warning(f"obs gap silent_targets repo failed: {e}")
        break
    if not include_retired:
        runtimes = [x for x in runtimes if not x.get("beyond_retire_window")]
    runtimes.sort(key=lambda x: (x.get("silent_sec") is None, -(x.get("silent_sec") or 0)))
    return {"runtimes": runtimes, "repos": repos, "generated_at": _iso(now),
            "retire_window_sec": GAP_MAX_STALE_SEC}