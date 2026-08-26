"""Dashboard 路由：仓库概览、指标、Agent 状态、运维可观察性"""
from fastapi import APIRouter, Depends
from src.user import db as user_db
from src.api.deps import get_current_user
from src.api.time_utils import normalize_event_time

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ===== 运维可观察性端点（必须在 /{owner}/{repo} 之前注册，否则被通配路由拦截）=====


@router.get("/ops/overview")
async def get_ops_overview():
    """实时运维概览：任务队列深度、运行数、卡住检测、runtime 在线状态。

    参考 multica BusinessSampler（metrics/business_sampler.go）的实时采样设计，
    但通过 Dashboard API 暴露给前端看板，无需 Prometheus 基础设施。

    无认证：运维只读数据，不涉及敏感信息（与 /health/realtime 同级）。
    数据全部来自 agent_tasks / agent_runtimes 表的实时 SQL 查询。
    """
    from src.session_manager import db, db_lock
    from src.runtime.health import derive_runtime_health
    from datetime import datetime, timezone, timedelta
    import time as _time

    now_ts = _time.time()
    now_iso = datetime.now(timezone.utc).isoformat()
    # stuck 阈值：running 超 15 分钟（worker 默认 RUNTIME_TASK_TIMEOUT=1200s=20min，15min 能在超时前预警）
    stuck_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()

    with db_lock:
        c = db.cursor()

        # --- 任务队列状态分布 ---
        task_status_counts = {"queued": 0, "dispatched": 0, "running": 0,
                              "completed": 0, "failed": 0, "cancelled": 0}
        try:
            c.execute("SELECT status, COUNT(*) FROM agent_tasks GROUP BY status")
            for status, count in c.fetchall():
                if status in task_status_counts:
                    task_status_counts[status] = count
        except Exception:
            pass

        # --- in_progress（dispatched + running）---
        in_progress = task_status_counts["dispatched"] + task_status_counts["running"]

        # --- stuck 检测：running 且 started_at < 30 分钟前 ---
        stuck_tasks = []
        try:
            c.execute(
                "SELECT id, agent_type, runtime_id, started_at, repo_key, issue_id, event_seq "
                "FROM agent_tasks WHERE status='running' AND started_at IS NOT NULL AND started_at < ? "
                "ORDER BY started_at ASC LIMIT 100",
                (stuck_cutoff,),
            )
            for r in c.fetchall():
                stuck_tasks.append({
                    "id": r[0], "agent_type": r[1] or "", "runtime_id": r[2],
                    "started_at": normalize_event_time(r[3]), "repo_key": r[4] or "",
                    "issue_id": r[5] or "", "event_seq": r[6] or 0,
                })
        except Exception:
            pass

        # --- 最近 failed 任务（最近 20 条）---
        failed_tasks = []
        try:
            c.execute(
                "SELECT id, agent_type, runtime_id, started_at, completed_at, repo_key, issue_id, event_seq, result_text, exit_code, errors "
                "FROM agent_tasks WHERE status='failed' ORDER BY completed_at DESC"
            )
            for r in c.fetchall():
                # errors 字段是 JSON 数组字符串，解析后取前 3 条
                import json as _json
                _errors = []
                try:
                    _errors = _json.loads(r[10]) if r[10] else []
                except Exception:
                    _errors = [r[10]] if r[10] else []
                failed_tasks.append({
                    "id": r[0], "agent_type": r[1] or "", "runtime_id": r[2],
                    "started_at": normalize_event_time(r[3]), "completed_at": normalize_event_time(r[4]),
                    "repo_key": r[5] or "", "issue_id": r[6] or "", "event_seq": r[7] or 0,
                    "result_text": (r[8] or "")[:200],
                    "exit_code": r[9],
                    "errors": _errors[:3],
                })
        except Exception:
            pass

        # --- 按来源（agent_type）分组排队/运行数 ---
        task_by_source = {}
        try:
            c.execute(
                "SELECT agent_type, status, COUNT(*) FROM agent_tasks "
                "WHERE status IN ('queued','dispatched','running') GROUP BY agent_type, status"
            )
            for atype, status, count in c.fetchall():
                key = atype or "unknown"
                if key not in task_by_source:
                    task_by_source[key] = {"queued": 0, "running": 0}
                if status == "queued":
                    task_by_source[key]["queued"] += count
                elif status in ("dispatched", "running"):
                    task_by_source[key]["running"] += count
        except Exception:
            pass

        # --- runtime 在线状态 ---
        runtimes_online = 0
        runtimes_total = 0
        runtime_by_mode = {"local": 0, "remote": 0, "unknown": 0}
        runtime_list = []
        try:
            c.execute(
                "SELECT id, name, credential_id, status, last_seen_at, provider "
                "FROM agent_runtimes ORDER BY id"
            )
            for r in c.fetchall():
                rid, rname, cid, rstatus, rlast_seen, rprovider = r
                runtimes_total += 1
                health = derive_runtime_health(rstatus or "offline", rlast_seen, now_ts)
                mode = "remote" if (cid or 0) > 0 else "local"
                is_online = health == "online"
                if is_online:
                    runtimes_online += 1
                    runtime_by_mode[mode] = runtime_by_mode.get(mode, 0) + 1
                runtime_list.append({
                    "id": rid, "name": rname or "", "status": rstatus or "offline",
                    "health": health, "mode": mode,
                    "provider": rprovider or "", "last_seen_at": normalize_event_time(rlast_seen),
                })
        except Exception:
            pass

        # --- 今日任务统计（UTC 日期前缀匹配 created_at ISO 时间戳）---
        today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_stats = {"created": 0, "completed": 0, "failed": 0}
        try:
            c.execute(
                "SELECT status, COUNT(*) FROM agent_tasks "
                "WHERE created_at LIKE ? GROUP BY status",
                (today_prefix + "%",),
            )
            for status, count in c.fetchall():
                if status in today_stats:
                    today_stats[status] = count
        except Exception:
            pass

    return {
        "timestamp": now_iso,
        "tasks": {
            "status_counts": task_status_counts,
            "in_progress": in_progress,
            "stuck_count": len(stuck_tasks),
            "stuck_tasks": stuck_tasks,
            "failed_tasks": failed_tasks,
            "by_source": task_by_source,
            "today": today_stats,
        },
        "runtimes": {
            "total": runtimes_total,
            "online": runtimes_online,
            "offline": runtimes_total - runtimes_online,
            "by_mode": runtime_by_mode,
            "list": runtime_list,
        },
    }


@router.get("/ops/error-logs")
async def get_error_logs(limit: int = 100):
    """最近错误/警告日志（从 data/logs/devops-bot.log 读取 JSON 行）。

    无认证：运维诊断数据，与 /health/realtime 同级。
    返回 [{ts, level, component, request_id, message, method, path, status, duration_ms}]。
    """
    import os, json as _json
    from src.config import LOG_DIR

    log_file = os.path.join(LOG_DIR, "devops-bot.log")
    entries = []
    if not os.path.exists(log_file):
        return {"entries": [], "file": log_file, "error": "log file not found"}

    # 从文件尾部读取，倒序收集 ERROR/WARNING 行
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = _json.loads(line)
                level = entry.get("level", "")
                if level in ("ERROR", "WARNING"):
                    entries.append(entry)
                    if len(entries) >= limit:
                        break
            except (_json.JSONDecodeError, ValueError):
                continue
    except Exception as e:
        return {"entries": [], "file": log_file, "error": str(e)}

    return {"entries": entries, "file": log_file}


@router.get("/ops/a2a-health")
async def get_a2a_health():
    """A2A 健康检查：server_base_url 可达性 + 最近 A2A 调用记录。

    无认证：运维诊断数据。
    """
    import requests as _requests
    from src.user.db import get_server_base_url
    from src.session_manager import db, db_lock

    base_url = get_server_base_url()
    result = {
        "server_base_url": base_url,
        "reachable": False,
        "health_status": None,
        "latency_ms": None,
        "error": None,
        "recent_a2a": [],
    }

    # 检查可达性
    try:
        import time as _time
        t0 = _time.time()
        r = _requests.get(base_url.rstrip("/") + "/health", timeout=5)
        result["latency_ms"] = round((_time.time() - t0) * 1000)
        result["reachable"] = r.status_code == 200
        result["health_status"] = r.status_code
    except Exception as e:
        result["error"] = str(e)[:200]

    # 最近 A2A 调用记录（从 events 表查 agent_type=A2A 的最近记录）
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, event_type, repo_key, issue_id, status, created_at, payload "
                "FROM events WHERE event_type LIKE '%a2a%' OR event_type LIKE '%A2A%' "
                "ORDER BY created_at DESC LIMIT 10"
            )
            for row in c.fetchall():
                result["recent_a2a"].append({
                    "seq": row[0], "event_type": row[1] or "", "repo_key": row[2] or "",
                    "issue_id": row[3] or "", "status": row[4] or "",
                    "created_at": normalize_event_time(row[5]),
                })
    except Exception:
        pass

    return result


@router.get("/{owner}/{repo}")
async def get_dashboard(owner: str, repo: str, user: dict = Depends(get_current_user)):
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")

    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()

        recent_events = []
        try:
            c.execute(
                "SELECT seq, event_type, agent_type, title, summary, status, created_at FROM events WHERE repo_key=? ORDER BY seq DESC LIMIT 5",
                (repo_key,)
            )
            recent_events = [
                {"seq": r[0], "event_type": r[1], "agent_type": r[2] or "", "title": r[3] or r[1],
                 "summary": r[4] or r[3] or r[1], "status": r[5], "created_at": normalize_event_time(r[6])}
                for r in c.fetchall()
            ]
        except Exception:
            pass

        agent_sessions = []
        try:
            c.execute(
                "SELECT s.agent_type, r.name, s.status, s.last_active_at, s.enable, COALESCE(rc.status,'') "
                "FROM devops_agent_session s LEFT JOIN agent_runtimes r ON s.runtime_id=r.id "
                "LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                "WHERE s.repo_key=? ORDER BY s.agent_type",
                (repo_key,)
            )
            agent_sessions = []
            for r in c.fetchall():
                _backend = r[1] or ""
                # 凭据已吊销的引擎视为不可用（revoke 后旧行保留但实际无法执行）：
                # 本机直连（rc.status=''，无凭据行）与 active 正常显示，revoked 置空走前端红字
                if _backend and r[5] not in ("", "active"):
                    _backend = ""
                agent_sessions.append({
                    "agent_type": r[0], "backend": _backend, "status": r[2], "last_active_at": normalize_event_time(r[3]),
                    "enable": bool(r[4]) if r[4] is not None else True})
        except Exception:
            pass

        # 每个 Agent 的最近 2 个事件（供 Dashboard Agent 卡片展示，替代假数据）
        agent_recent_events = {}
        try:
            for atype in ["vod", "issue", "coding", "qa", "sre"]:
                c.execute(
                    "SELECT seq, event_type, agent_type, title, summary, status, created_at FROM events WHERE repo_key=? AND agent_type=? ORDER BY seq DESC LIMIT 2",
                    (repo_key, atype)
                )
                agent_recent_events[atype] = [
                    {"seq": r[0], "event_type": r[1], "agent_type": r[2] or "", "title": r[3] or r[1],
                     "summary": r[4] or r[3] or r[1], "status": r[5], "created_at": normalize_event_time(r[6])}
                    for r in c.fetchall()
                ]
        except Exception:
            pass

    agent_types_present = {a["agent_type"] for a in agent_sessions}
    all_types = ["vod", "issue", "coding", "qa", "sre"]
    for atype in all_types:
        if atype not in agent_types_present:
            from src.api.session_store import get_agent_type_enable
            agent_sessions.append({
                "agent_type": atype,
                "backend": "",  # 无绑定/未建会话：不写硬编码默认（engine-selection-design.md v2）
                "status": "",
                "last_active_at": "",
                "enable": bool(get_agent_type_enable(atype)),
            })

    # 计算每个 Agent 的事件统计
    agent_event_counts = {}
    for ev in recent_events:
        at = ev["agent_type"]
        if at:
            agent_event_counts[at] = agent_event_counts.get(at, 0) + 1

    for atype in all_types:
        if atype not in agent_event_counts:
            agent_event_counts[atype] = 0

    # 获取用户角色
    role = "Member"
    try:
        repos = user_db.get_managed_repos(user["id"])
        for r in repos:
            if r["repo_owner"] == owner and r["repo_name"] == repo:
                role = "Maintainer" if r.get("is_admin") else "Member"
                break
    except Exception:
        pass

    event_counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
    for ev in recent_events:
        s = ev["status"]
        if s in event_counts:
            event_counts[s] += 1

    # 获取当前场景和场景列表
    dev_scene = "default"
    try:
        with db_lock:
            c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            if row and row[0]:
                dev_scene = row[0]
    except Exception:
        pass

    scenes = []
    try:
        from src.api.routes.scenes import _load_scenes
        for sid, s in _load_scenes().items():
            scenes.append({"id": sid, "name": s.get("name", sid)})
    except Exception:
        pass

    return {
        "repo": repo_key,
        "role": role,
        "dev_scene": dev_scene,
        "_scenes": scenes,
        "metrics": {
            "total_events": len(recent_events),
            "active_agents": sum(1 for a in agent_sessions if a["status"] == "running"),
            "pending_events": event_counts["pending"],
            "failed_events": event_counts["failed"],
        },
        "agents": agent_sessions,
        "agent_event_counts": agent_event_counts,
        "agent_recent_events": agent_recent_events,
        "activity": recent_events,
    }
