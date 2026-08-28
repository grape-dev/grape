"""Dashboard 路由：仓库概览、指标、Agent 状态、运维可观察性"""
from fastapi import APIRouter, Depends
from src.user import db as user_db
from src.api.deps import get_current_user
from src.api.time_utils import normalize_event_time

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ===== 运维可观察性端点（必须在 /{owner}/{repo} 之前注册，否则被通配路由拦截）=====


@router.get("/ops/overview")
async def get_ops_overview(repo: str = ""):
    """实时运维概览：任务队列深度、运行数、卡住检测、runtime 在线状态。

    参考 multica BusinessSampler（metrics/business_sampler.go）的实时采样设计，
    但通过 Dashboard API 暴露给前端看板，无需 Prometheus 基础设施。

    无认证：运维只读数据，不涉及敏感信息（与 /health/realtime 同级）。
    数据全部来自 agent_tasks / agent_runtimes 表的实时 SQL 查询。
    repo 参数：仓库名（requiredsa/hello-flask），过滤仅该仓库的任务。
    """
    from src.session_manager import db, db_lock
    from src.runtime.health import derive_runtime_health
    from datetime import datetime, timezone, timedelta
    import time as _time

    now_ts = _time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    # repo 过滤条件
    _repo_filter = ""
    _repo_params = []
    if repo:
        repo_key = f"gitcode:{repo}" if not repo.startswith("gitcode:") else repo
        _repo_filter = " AND repo_key=?"
        _repo_params = [repo_key]
    # stuck 阈值：running 超 15 分钟（worker 默认 RUNTIME_TASK_TIMEOUT=1200s=20min，15min 能在超时前预警）
    stuck_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()

    with db_lock:
        c = db.cursor()

        # --- 任务队列状态分布 ---
        task_status_counts = {"queued": 0, "dispatched": 0, "running": 0,
                              "completed": 0, "failed": 0, "cancelled": 0}
        try:
            c.execute(f"SELECT status, COUNT(*) FROM agent_tasks WHERE 1=1{_repo_filter} GROUP BY status", tuple(_repo_params))
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
                "FROM agent_tasks WHERE status='running' AND started_at IS NOT NULL AND started_at < ?"
                + _repo_filter +
                " ORDER BY started_at ASC LIMIT 100",
                (stuck_cutoff,) + tuple(_repo_params),
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
                "FROM agent_tasks WHERE status='failed'" + _repo_filter + " ORDER BY completed_at DESC",
                tuple(_repo_params),
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
                "WHERE status IN ('queued','dispatched','running')" + _repo_filter + " GROUP BY agent_type, status",
                tuple(_repo_params),
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

        # --- worker（凭据）列表 + 资源指标 ---
        # 本机直连引擎（credential_id=0）用 psutil 采集本机资源
        # interval=None 非阻塞：返回自上次调用以来的平均值（10s 刷新间隔内的 CPU 均值）
        # 首次调用返回 0.0（无基准），后续调用返回真实值
        _local_cpu = _local_mem = _local_disk = 0.0
        _local_load = ""
        try:
            import psutil as _ps
            _local_cpu = _ps.cpu_percent(interval=None)
            # MEM 用 /proc/meminfo 读取实际占用（total - MemAvailable，包含不可回收缓存）
            try:
                with open("/proc/meminfo") as _mf:
                    _mi = {}
                    for _line in _mf:
                        _p = _line.split(":")
                        if len(_p) == 2:
                            _mi[_p[0]] = int(_p[1].strip().split()[0]) * 1024
                    _total = _mi["MemTotal"]
                    _available = _mi.get("MemAvailable", 0)
                    if _available > 0:
                        _local_mem = round((_total - _available) / _total * 100, 1)
                    else:
                        _local_mem = _ps.virtual_memory().percent
            except Exception:
                _local_mem = _ps.virtual_memory().percent
            _local_disk = _ps.disk_usage("/").percent
            _local_load = ",".join(f"{x:.2f}" for x in _ps.getloadavg())
        except Exception:
            pass

        worker_list = []
        worker_online = 0
        worker_total = 0

        # 检查是否有本机直连引擎
        has_local = False
        try:
            c.execute("SELECT COUNT(*) FROM agent_runtimes WHERE credential_id=0")
            has_local = (c.fetchone()[0] or 0) > 0
        except Exception:
            pass
        if has_local:
            worker_total += 1
            worker_online += 1
            # 本机指标只在 ops/overview 返回中展示，不写入 DB（运营管理台设备监控只显示远端 worker）
            worker_list.append({
                "id": 0, "name": "Local", "device_name": "Local Direct",
                "status": "online", "health": "online",
                "last_seen_at": now_iso, "last_ip": "127.0.0.1",
                "cpu_percent": _local_cpu, "memory_percent": _local_mem,
                "disk_percent": _local_disk, "load_avg": _local_load,
                "owner_name": "",
            })

        # 查远端 worker（凭据）
        try:
            c.execute(
                "SELECT id, name, device_name, status, last_seen_at, last_ip, "
                "cpu_percent, memory_percent, disk_percent, load_avg, owner_name "
                "FROM runtime_credentials ORDER BY id"
            )
            for w in c.fetchall():
                wid, wname, wdevice, wstatus, wlast_seen, wip, wcpu, wmem, wdisk, wload, wowner = w
                worker_total += 1
                w_health = derive_runtime_health(wstatus or "offline", wlast_seen, now_ts)
                w_online = w_health == "online"
                if w_online:
                    worker_online += 1
                worker_list.append({
                    "id": wid, "name": wname or "", "device_name": wdevice or "",
                    "status": wstatus or "offline", "health": w_health,
                    "last_seen_at": normalize_event_time(wlast_seen), "last_ip": wip or "",
                    "cpu_percent": wcpu or 0, "memory_percent": wmem or 0,
                    "disk_percent": wdisk or 0, "load_avg": wload or "",
                    "owner_name": wowner or "",
                })
        except Exception:
            pass

        # 查所有 runtime（引擎），关联 worker 信息
        runtimes_online = 0
        runtimes_total = 0
        runtime_by_mode = {"local": 0, "remote": 0, "unknown": 0}
        runtime_list = []
        # 查每个 runtime 当前 running 任务数（实时负载）
        _running_counts = {}
        try:
            c.execute(
                "SELECT runtime_id, COUNT(*) FROM agent_tasks "
                "WHERE status='running' GROUP BY runtime_id"
            )
            for row in c.fetchall():
                _running_counts[row[0]] = row[1]
        except Exception:
            pass
        # 查每个引擎已完成任务的平均耗时（秒）
        _avg_durations = {}
        try:
            c.execute(
                "SELECT runtime_id, AVG(CASE "
                "WHEN completed_at IS NOT NULL AND started_at IS NOT NULL "
                "THEN (strftime('%s', completed_at) - strftime('%s', started_at)) "
                "ELSE NULL END) as avg_sec "
                "FROM agent_tasks WHERE status IN ('completed','failed') AND runtime_id > 0 "
                "GROUP BY runtime_id"
            )
            for row in c.fetchall():
                _avg_durations[row[0]] = round(row[1] or 0, 1) if row[1] else 0
        except Exception:
            try:
                # MySQL 兼容（TIMESTAMPDIFF）
                c.execute(
                    "SELECT runtime_id, AVG(TIMESTAMPDIFF(SECOND, started_at, completed_at)) as avg_sec "
                    "FROM agent_tasks WHERE status IN ('completed','failed') AND runtime_id > 0 "
                    "AND started_at IS NOT NULL AND completed_at IS NOT NULL "
                    "GROUP BY runtime_id"
                )
                for row in c.fetchall():
                    _avg_durations[row[0]] = round(row[1] or 0, 1) if row[1] else 0
            except Exception:
                pass
        try:
            c.execute(
                "SELECT ar.id, ar.name, ar.credential_id, ar.status, ar.last_seen_at, ar.provider, "
                "ar.healthy, ar.busy_slots, ar.max_concurrent, ar.version, "
                "rc.device_name "
                "FROM agent_runtimes ar "
                "LEFT JOIN runtime_credentials rc ON ar.credential_id = rc.id "
                "ORDER BY ar.id"
            )
            for r in c.fetchall():
                rid, rname, cid, rstatus, rlast_seen, rprovider, rhealthy, rbusy, rmax, rver, rdev = r
                runtimes_total += 1
                base_health = derive_runtime_health(rstatus or "offline", rlast_seen, now_ts)
                mode = "remote" if (cid or 0) > 0 else "local"
                # 本机直连引擎始终算在线
                is_online = (cid or 0) == 0 or base_health == "online"
                if is_online:
                    runtimes_online += 1
                    runtime_by_mode[mode] = runtime_by_mode.get(mode, 0) + 1
                # running 任务数（实时负载）
                running = _running_counts.get(rid, 0)
                max_conc = rmax or 0
                # 本机直连引擎（credential_id=0）始终可用，不看心跳
                if (cid or 0) == 0:
                    if running == 0:
                        health = "idle"
                    elif running < max_conc:
                        health = "normal"
                    elif running < max_conc * 2:
                        health = "busy"
                    else:
                        health = "overload"
                # 远端引擎：先看心跳，online 时再看负载
                elif base_health == "online":
                    if running == 0:
                        health = "idle"
                    elif running < max_conc:
                        health = "normal"
                    elif running < max_conc * 2:
                        health = "busy"
                    else:
                        health = "overload"
                else:
                    health = base_health
                runtime_list.append({
                    "id": rid, "name": rname or "", "status": rstatus or "offline",
                    "health": health, "mode": mode,
                    "provider": rprovider or "", "last_seen_at": normalize_event_time(rlast_seen),
                    "healthy": bool(rhealthy), "busy_slots": rbusy or 0, "max_concurrent": max_conc,
                    "running": running, "avg_duration": _avg_durations.get(rid, 0),
                    "version": rver or "", "device_name": rdev or ("Local Direct" if (cid or 0) == 0 else ""),
                })
        except Exception:
            pass

        # --- 今日任务统计（UTC 日期前缀匹配 created_at ISO 时间戳）---
        today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_stats = {"created": 0, "completed": 0, "failed": 0}
        try:
            c.execute(
                "SELECT status, COUNT(*) FROM agent_tasks "
                "WHERE created_at LIKE ?" + _repo_filter + " GROUP BY status",
                (today_prefix + "%",) + tuple(_repo_params),
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
        "workers": {
            "total": worker_total,
            "online": worker_online,
            "offline": worker_total - worker_online,
            "list": worker_list,
        },
    }


@router.get("/ops/error-logs/sources")
async def get_error_log_sources():
    """返回可用的日志来源列表（主控 + 各 worker）。"""
    import os as _os, glob as _glob2
    from src.config import LOG_DIR
    from src.session_manager import db, db_lock

    sources = [{"source": "主控", "id": 0}]
    _worker_dir = _os.path.join(LOG_DIR, "workers")
    for _wf in sorted(_glob2.glob(_os.path.join(_worker_dir, "*.log"))):
        _cred_id = _os.path.splitext(_os.path.basename(_wf))[0]
        _wname = f"worker-{_cred_id}"
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT name, device_name FROM runtime_credentials WHERE id=?", (_cred_id,))
                row = c.fetchone()
                if row:
                    _wname = row[1] or row[0] or _wname
        except Exception:
            pass
        sources.append({"source": _wname, "id": int(_cred_id) if _cred_id.isdigit() else 0})
    return {"sources": sources}


@router.get("/ops/error-logs")
async def get_error_logs(limit: int = 100, level: str = "", repo: str = "", user_id: str = "", source: str = ""):
    """最近错误/警告日志。

    同时读取新旧两个日志文件：
    - data/logs/devops-bot.log（新 JSON 结构化日志）
    - data/devops-bot.log（旧纯文本日志 + 轮转文件）

    参数：
    - limit: 最多返回条数（默认 100）
    - level: 过滤级别 — ""（ERROR+WARNING）/"ERROR"/"WARNING"
    - repo: 仓库名（requiredsa/hello-flask），过滤仅该仓库的日志
    - user_id: 用户 ID，过滤仅该用户的日志

    无认证：运维诊断数据，与 /health/realtime 同级。
    """
    import os, json as _json, glob as _glob, re as _re
    from src.config import LOG_DIR

    allowed_levels = {"ERROR", "WARNING"}
    if level and level.upper() in allowed_levels:
        allowed_levels = {level.upper()}

    # repo/user_id 过滤条件
    _repo_filter = ""
    if repo:
        repo_key = f"gitcode:{repo}" if not repo.startswith("gitcode:") else repo
        _repo_filter = repo_key

    entries = []
    files_read = []

    def _match_repo(entry):
        """检查日志条目是否匹配指定的 repo"""
        if not _repo_filter:
            return True
        entry_repo = entry.get("repo_key", "")
        if entry_repo and entry_repo == _repo_filter:
            return True
        # 旧格式日志无 repo_key 字段，检查 message 中是否包含仓库名
        msg = entry.get("message", "")
        repo_short = _repo_filter.replace("gitcode:", "")
        if repo_short in msg:
            return True
        return False

    def _match_user(entry):
        """检查日志条目是否匹配指定的 user_id"""
        if not user_id:
            return True
        entry_uid = str(entry.get("user_id", ""))
        if entry_uid and entry_uid == str(user_id):
            return True
        return False

    def _parse_json_log(filepath):
        """解析 JSON 行格式日志"""
        nonlocal entries
        if not os.path.exists(filepath):
            return
        files_read.append(filepath)
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = _json.loads(line)
                    if entry.get("level", "") in allowed_levels and _match_repo(entry) and _match_user(entry):
                        entries.append(entry)
                        if len(entries) >= limit:
                            return
                except (_json.JSONDecodeError, ValueError):
                    continue
        except Exception:
            pass

    def _parse_text_log(filepath):
        """解析旧纯文本格式日志（正则提取 level + message）"""
        nonlocal entries
        if not os.path.exists(filepath):
            return
        files_read.append(filepath)
        # 旧格式: "2026-08-26T10:30:00.000Z - ERROR - devops-bot - message..."
        # 或: "ERROR:devops-bot:message"
        _re_json = _re.compile(r'^\s*\{.*\}\s*$')
        _re_text = _re.compile(
            r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.\dZ]*)\s*[-_]\s*'
            r'(ERROR|WARNING|WARN)\s*[-_]\s*'
            r'(\S+)\s*[-_]\s*(.*)',
            _re.IGNORECASE
        )
        _re_simple = _re.compile(r'^(ERROR|WARNING|WARN)[:\s]+(.+)', _re.IGNORECASE)
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                # 跳过 JSON 行（已由 _parse_json_log 处理）
                if _re_json.match(line):
                    try:
                        entry = _json.loads(line)
                        if entry.get("level", "") in allowed_levels and _match_repo(entry) and _match_user(entry):
                            entries.append(entry)
                            if len(entries) >= limit:
                                return
                    except Exception:
                        pass
                    continue
                # 纯文本格式
                m = _re_text.match(line)
                if m:
                    lvl = m.group(2).upper()
                    if lvl == "WARN":
                        lvl = "WARNING"
                    if lvl in allowed_levels:
                        _e = {"ts": m.group(1), "level": lvl,
                            "component": m.group(3), "request_id": "-",
                            "message": m.group(4)[:500]}
                        if _match_repo(_e) and _match_user(_e):
                            entries.append(_e)
                            if len(entries) >= limit:
                                return
                    continue
                m2 = _re_simple.match(line)
                if m2:
                    lvl = m2.group(1).upper()
                    if lvl == "WARN":
                        lvl = "WARNING"
                    if lvl in allowed_levels:
                        _e = {"ts": "", "level": lvl, "component": "",
                            "request_id": "-", "message": m2.group(2)[:500]}
                        if _match_repo(_e) and _match_user(_e):
                            entries.append(_e)
                        if len(entries) >= limit:
                            return
        except Exception:
            pass

    # 1. 新日志（JSON 格式）
    _parse_json_log(os.path.join(LOG_DIR, "devops-bot.log"))

    # 2. 旧日志（纯文本格式）+ 轮转文件
    old_log_dir = os.path.dirname(LOG_DIR)  # data/
    _parse_text_log(os.path.join(old_log_dir, "devops-bot.log"))
    for rotated in sorted(_glob.glob(os.path.join(old_log_dir, "devops-bot.log.*")), reverse=True):
        if len(entries) >= limit:
            break
        _parse_text_log(rotated)

    # 3. Worker 日志（data/logs/workers/*.log，JSON 格式，含 worker_id/worker_name）
    _worker_dir = os.path.join(LOG_DIR, "workers")
    for _wf in sorted(_glob.glob(os.path.join(_worker_dir, "*.log")), reverse=True):
        if len(entries) >= limit:
            break
        _parse_json_log(_wf)

    # 给主控日志加 source="主控"，worker 日志已有 worker_name
    for e in entries:
        if "source" not in e:
            e["source"] = e.get("worker_name") or "主控"

    # source 过滤
    if source:
        entries = [e for e in entries if e.get("source") == source or (source == "主控" and (e.get("source") == "主控"))]

    return {"entries": entries, "files": files_read, "count": len(entries)}


@router.get("/ops/timeline")
async def get_ops_timeline(repo: str = ""):
    """任务时序图数据：按 issue 分组的事件链（参考 admin RepoService.timeline）。

    无认证：运维只读数据。
    返回 {repoKey, issues: [{issueId, title, eventCount, firstAt, lastAt, events: [...]}]}
    """
    from src.session_manager import db, db_lock
    import json as _json

    repo_key = ""
    if repo:
        repo_key = f"gitcode:{repo}" if not repo.startswith("gitcode:") else repo

    with db_lock:
        c = db.cursor()
        if repo_key:
            c.execute(
                "SELECT seq, event_type, issue_id, title, actor, agent_type, skill, status, duration, created_at, payload "
                "FROM events WHERE repo_key=? AND issue_id IS NOT NULL AND issue_id <> '' ORDER BY created_at",
                (repo_key,)
            )
        else:
            c.execute(
                "SELECT seq, event_type, issue_id, title, actor, agent_type, skill, status, duration, created_at, payload "
                "FROM events WHERE issue_id IS NOT NULL AND issue_id <> '' ORDER BY created_at"
            )
        rows = c.fetchall()

        # 查 agent_tasks 建立 (issue_id, event_seq) → task_id 映射
        c.execute(
            "SELECT issue_id, event_seq, id FROM agent_tasks "
            "WHERE issue_id IS NOT NULL AND issue_id <> ''"
        )
        task_map = {}
        for row in c.fetchall():
            task_map[(row[0], row[1])] = row[2]

    # 按 issue_id 分组
    group = {}
    for row in rows:
        seq, event_type, issue_id, title, actor, agent_type, skill, status, duration, created_at, payload = row
        if issue_id not in group:
            group[issue_id] = []
        group[issue_id].append({
            "seq": seq, "eventType": event_type, "agentType": agent_type or "",
            "skill": skill or "", "status": status or "",
            "duration": duration or 0, "createdAt": normalize_event_time(created_at),
            "actor": actor or "", "title": title or "",
            "taskId": task_map.get((issue_id, seq), ""),
            "_payload": payload or "",
        })

    issues = []
    for issue_id, evs in group.items():
        # 从 payload 提取 issue 标题
        issue_title = ""
        for ev in evs:
            if ev["_payload"]:
                try:
                    attrs = _json.loads(ev["_payload"])
                    oa = attrs.get("object_attributes", {})
                    issue_title = oa.get("title", "")
                    if issue_title:
                        break
                except Exception:
                    pass
        # 清理 _payload 字段
        for ev in evs:
            del ev["_payload"]
        issues.append({
            "issueId": issue_id, "title": issue_title,
            "eventCount": len(evs),
            "firstAt": evs[0]["createdAt"] if evs else "",
            "lastAt": evs[-1]["createdAt"] if evs else "",
            "events": evs,
        })

    return {"repoKey": repo_key, "issues": issues}


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
