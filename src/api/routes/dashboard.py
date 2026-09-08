"""Dashboard 路由：仓库概览、指标、Agent 状态、运维可观察性"""
from fastapi import APIRouter, Depends
from src.user import db as user_db
from src.api.deps import get_current_user
from src.api.time_utils import normalize_event_time
from src.observability.engine_probe import get_probe_results
from src.config import log

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ===== 运维可观察性端点（必须在 /{owner}/{repo} 之前注册，否则被通配路由拦截）=====


@router.get("/ops/overview")
def get_ops_overview(repo: str = "", user: dict = Depends(get_current_user)):
    """实时运维概览：任务队列深度、运行数、卡住检测、runtime 在线状态。

    参考 multica BusinessSampler（metrics/business_sampler.go）的实时采样设计，
    但通过 Dashboard API 暴露给前端看板，无需 Prometheus 基础设施。

    需登录：user scope worker 按当前用户过滤（与 Agent 运行时页面可见性一致）。
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
    repo_key = ""
    if repo:
        repo_key = f"gitcode:{repo}" if not repo.startswith("gitcode:") else repo
        _repo_filter = " AND repo_key=?"
        _repo_params = [repo_key]
    # stuck 阈值：running 超 22 分钟（worker 默认 RUNTIME_TASK_TIMEOUT=1800s=30min，22min 能在超时前预警）
    stuck_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=22)).isoformat()

    with db_lock:
        c = db.cursor()

        # --- 任务队列状态分布（task 级别）---
        task_status_counts = {"queued": 0, "dispatched": 0, "running": 0,
                              "completed": 0, "failed": 0, "cancelled": 0}
        try:
            c.execute(f"SELECT status, COUNT(*) FROM agent_tasks WHERE 1=1{_repo_filter} GROUP BY status", tuple(_repo_params))
            for status, count in c.fetchall():
                if status in task_status_counts:
                    task_status_counts[status] = count
        except Exception:
            pass

        # --- Issue 级别统计（一个 issue = 一个任务）---
        issue_stats = {"total": 0, "completed": 0, "in_progress": 0, "failed": 0}
        try:
            # 每个 issue 取最新 task 的状态作为该 issue 的状态
            c.execute(f"""
                SELECT t.status, COUNT(*) FROM (
                    SELECT issue_id, MAX(id) as max_id FROM agent_tasks
                    WHERE issue_id IS NOT NULL AND issue_id <> '' {(_repo_filter.replace('WHERE', 'AND') if _repo_filter else '')}
                    GROUP BY issue_id
                ) latest JOIN agent_tasks t ON t.id = latest.max_id
                GROUP BY t.status
            """, tuple(_repo_params))
            _issue_status_map = {}
            for status, count in c.fetchall():
                _issue_status_map[status] = count
            issue_stats["total"] = sum(_issue_status_map.values())
            issue_stats["completed"] = _issue_status_map.get("completed", 0)
            issue_stats["failed"] = _issue_status_map.get("failed", 0)
            issue_stats["in_progress"] = (_issue_status_map.get("running", 0) +
                                          _issue_status_map.get("dispatched", 0) +
                                          _issue_status_map.get("queued", 0))
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
        _local_cpu = _local_mem = _local_disk = _local_tmp = 0.0
        _local_load = ""
        try:
            import psutil as _ps
            _local_cpu = _ps.cpu_percent(interval=None)
            # MEM 用 /proc/meminfo 读取实际进程占用（total - free - buffers - cached，不含缓存）
            try:
                with open("/proc/meminfo") as _mf:
                    _mi = {}
                    for _line in _mf:
                        _p = _line.split(":")
                        if len(_p) == 2:
                            _mi[_p[0]] = int(_p[1].strip().split()[0]) * 1024
                    _total = _mi["MemTotal"]
                    _free = _mi.get("MemFree", 0)
                    _buffers = _mi.get("Buffers", 0)
                    _cached = _mi.get("Cached", 0)
                    _used = _total - _free - _buffers - _cached
                    if _total > 0:
                        _local_mem = round(_used / _total * 100, 1)
                    else:
                        _local_mem = _ps.virtual_memory().percent
            except Exception:
                _local_mem = _ps.virtual_memory().percent
            _local_disk = _ps.disk_usage("/").percent
            # TMP: /tmp 目录使用率（tmpfs 内存文件系统，独立于根分区）
            try:
                _local_tmp = _ps.disk_usage("/tmp").percent
            except Exception:
                _local_tmp = 0.0
            _local_load = ",".join(f"{x:.2f}" for x in _ps.getloadavg())
        except Exception:
            pass

        worker_list = []
        worker_online = 0
        worker_total = 0

        # 本机主控机：按 hostname 匹配 runtime_credentials 记录，采集指标后写入 DB
        _local_hostname = ""
        try:
            import socket as _sock
            _local_hostname = _sock.gethostname()
        except Exception:
            pass
        _local_cred_id = None
        if _local_hostname:
            try:
                c.execute("SELECT id FROM runtime_credentials WHERE device_name=?", (_local_hostname,))
                row = c.fetchone()
                if row:
                    _local_cred_id = row[0]
            except Exception:
                pass
        if _local_cred_id is not None:
            # 写入本机指标到 DB（admin 健康中心读同一张表即可看到）
            try:
                c.execute(
                    "UPDATE runtime_credentials SET last_seen_at=?, last_ip=?, "
                    "cpu_percent=?, memory_percent=?, disk_percent=?, tmp_percent=?, load_avg=? WHERE id=?",
                    (now_iso, "127.0.0.1", _local_cpu, _local_mem, _local_disk, _local_tmp, _local_load, _local_cred_id))
                db.commit()
            except Exception:
                pass

        # 查 worker（凭据），排除已注销（revoked）
        # worker 可见性：global 全部可见 + user scope 只看自己的 + repo scope 看仓库的
        _uname = user.get("username") or ""
        try:
            if repo:
                # 仓库级看板：global + 自己的 user + 该 repo 的 worker
                c.execute(
                    "SELECT id, name, device_name, status, last_seen_at, last_ip, "
                    "cpu_percent, memory_percent, disk_percent, tmp_percent, load_avg, owner_name "
                    "FROM runtime_credentials WHERE status != 'revoked' "
                    "AND (scope='global' OR (scope='user' AND owner_name=?) OR repo_key=? OR repo_key=?) ORDER BY id",
                    (_uname, repo_key, repo),
                )
            else:
                # 全局看板：global + 自己的 user scope worker
                c.execute(
                    "SELECT id, name, device_name, status, last_seen_at, last_ip, "
                    "cpu_percent, memory_percent, disk_percent, tmp_percent, load_avg, owner_name "
                    "FROM runtime_credentials WHERE status != 'revoked' "
                    "AND (scope='global' OR (scope='user' AND owner_name=?)) ORDER BY id",
                    (_uname,),
                )
            for w in c.fetchall():
                wid, wname, wdevice, wstatus, wlast_seen, wip, wcpu, wmem, wdisk, wtmp, wload, wowner = w
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
                    "disk_percent": wdisk or 0, "tmp_percent": wtmp or 0,
                    "load_avg": wload or "",
                    "owner_name": wowner or "",
                })
        except Exception:
            pass

        # 补充本机直连设备（credential_id=0 的引擎不在 runtime_credentials 表中）
        try:
            _local_hostname = ""
            try:
                import socket as _sock
                _local_hostname = _sock.gethostname()
            except Exception:
                pass
            c.execute(
                "SELECT ar.id, ar.name, ar.status, ar.last_seen_at, ar.host_name, "
                "ar.healthy, ar.busy_slots, ar.max_concurrent "
                "FROM agent_runtimes ar WHERE ar.credential_id = 0"
            )
            _local_rts = c.fetchall()
            if _local_rts:
                _has_local_worker = any(w.get("device_name") == (_local_hostname or "本机直连") for w in worker_list)
                if not _has_local_worker:
                    worker_list.append({
                        "id": 0, "name": _local_hostname or "本机直连",
                        "device_name": _local_hostname or "本机直连",
                        "status": "online", "health": "online",
                        "last_seen_at": "", "last_ip": "127.0.0.1",
                        "cpu_percent": _local_cpu, "memory_percent": _local_mem,
                        "disk_percent": _local_disk, "tmp_percent": _local_tmp,
                        "load_avg": _local_load, "owner_name": "",
                    })
                    worker_total += 1
                    worker_online += 1
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
        # 引擎探测结果（每 10 分钟 CLI 测试对话）
        _probe_results = get_probe_results()
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
                "rc.device_name, rc.status AS cred_status, ar.host_name, "
                "ar.probe_status, ar.probe_latency, ar.probe_last, ar.probe_error "
                "FROM agent_runtimes ar "
                "LEFT JOIN runtime_credentials rc ON ar.credential_id = rc.id "
                "ORDER BY ar.id"
            )
            for r in c.fetchall():
                rid, rname, cid, rstatus, rlast_seen, rprovider, rhealthy, rbusy, rmax, rver, rdev, rcred_status, rhost, rprobe_status, rprobe_latency, rprobe_last, rprobe_error = r
                # 跳过已注销（revoked）凭据绑定的远端引擎（与 Workers 视图一致）
                if (cid or 0) > 0 and rcred_status == 'revoked':
                    continue
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
                # 引擎探测结果：本机用内存（engine_probe），远端用 DB（心跳上报）
                if (cid or 0) == 0:
                    _probe = _probe_results.get(rname or "", {})
                else:
                    _probe = {
                        "status": rprobe_status or "pending",
                        "latency": rprobe_latency or 0,
                        "last_probe": rprobe_last or "",
                        "error": rprobe_error or "",
                    }
                # 本机直连引擎的 device_name 用 host_name（区分多台主控机）
                _dev_name = rdev or ""
                if (cid or 0) == 0:
                    _dev_name = rhost or "本机直连"
                runtime_list.append({
                    "id": rid, "name": rname or "", "status": rstatus or "offline",
                    "health": health, "mode": mode,
                    "provider": rprovider or "", "last_seen_at": normalize_event_time(rlast_seen),
                    "healthy": bool(rhealthy), "busy_slots": rbusy or 0, "max_concurrent": max_conc,
                    "running": running, "avg_duration": _avg_durations.get(rid, 0),
                    "version": rver or "", "device_name": _dev_name,
                    "probe_status": _probe.get("status", "pending"),
                    "probe_latency": _probe.get("latency", 0),
                    "probe_last": _probe.get("last_probe", ""),
                    "probe_error": _probe.get("error", ""),
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
            "issue_stats": issue_stats,
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


@router.get("/ops/probe-now")
def probe_engines_now():
    """手动触发引擎探测（60s 防抖：窗口内重复请求返回上次结果，不重新探测）。"""
    from src.observability.engine_probe import get_probe_results, _last_probe_time
    import time, threading
    now = time.time()
    last = _last_probe_time if _last_probe_time else 0
    if now - last < 300:
        # 防抖窗口内（5分钟），返回上次结果
        return {"status": "throttled", "wait": int(300 - (now - last)), "results": get_probe_results()}
    # 防抖窗口外：后台线程异步执行探测，立即返回（不阻塞当前请求）
    def _bg_probe():
        try:
            from src.observability.engine_probe import probe_all_now
            probe_all_now()
        except Exception as e:
            import logging
            logging.getLogger("devops-bot").warning(f"bg probe error: {e}")
    threading.Thread(target=_bg_probe, daemon=True).start()
    return {"status": "accepted", "results": get_probe_results()}


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
        """检查日志条目是否匹配指定的 repo。
        - 无 repo 过滤条件：全部返回
        - 日志有 repo_key：必须精确匹配
        - 日志无 repo_key 但 message 含 owner/repo 格式仓库名：
          匹配当前仓库则展示，含其他仓库则不展示
        - 日志不含任何仓库名：始终展示（系统级日志）"""
        if not _repo_filter:
            return True
        entry_repo = entry.get("repo_key", "")
        if entry_repo:
            return entry_repo == _repo_filter
        msg = entry.get("message", "")
        repo_short = _repo_filter.replace("gitcode:", "")
        if repo_short in msg:
            return True
        # 检查 message 中是否含其他仓库名（owner/repo 格式，排除路径段如 api/v5）
        import re as _re2
        _repo_pattern = _re2.compile(r'\b([a-zA-Z0-9_-]+)/([a-zA-Z0-9_.-]+)\b')
        for m in _repo_pattern.finditer(msg):
            candidate = f"{m.group(1)}/{m.group(2)}"
            if candidate != repo_short and not m.group(1).startswith("api"):
                return False  # 含其他仓库名，不展示
        # 不含任何仓库名，系统级日志，始终展示
        return True

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
                    if entry.get("level", "") in allowed_levels and _match_repo(entry) and _match_user(entry) and not _is_http_noise(entry):
                        entries.append(entry)
                        if len(entries) >= limit:
                            return
                except (_json.JSONDecodeError, ValueError):
                    continue
        except Exception:
            pass

    def _is_http_noise(entry):
        """过滤掉正常行为的 HTTP 请求日志（非真正错误）。
        保留 runtime/worker 凭据失败的 401（运维需感知 worker 掉线）。"""
        msg = entry.get("message", "")
        path = entry.get("path", "")
        status = entry.get("status")
        # 401：非 runtime 的是 token 过期刷新，过滤
        if status == 401 or "status=401" in msg:
            if "/api/runtime/" in path or "/api/runtime/" in msg:
                return False  # runtime 401 保留
            return True
        # 403/404：health 限制、favicon、SPA 路由等，过滤
        if status in (403, 404) or "status=403" in msg or "status=404" in msg:
            return True
        return False

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
                        if entry.get("level", "") in allowed_levels and _match_repo(entry) and _match_user(entry) and not _is_http_noise(entry):
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
                        if _match_repo(_e) and _match_user(_e) and not _is_http_noise(_e):
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
                        if _match_repo(_e) and _match_user(_e) and not _is_http_noise(_e):
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
                "SELECT seq, event_type, issue_id, title, actor, agent_type, skill, status, duration, created_at, payload, repo_key "
                "FROM events WHERE repo_key=? AND issue_id IS NOT NULL AND issue_id <> '' ORDER BY created_at",
                (repo_key,)
            )
        else:
            c.execute(
                "SELECT seq, event_type, issue_id, title, actor, agent_type, skill, status, duration, created_at, payload, repo_key "
                "FROM events WHERE issue_id IS NOT NULL AND issue_id <> '' ORDER BY created_at"
            )
        rows = c.fetchall()

        # 查 agent_tasks 建立 (issue_id, event_seq) → (task_id, runtime_id) 映射
        c.execute(
            "SELECT issue_id, event_seq, id, runtime_id FROM agent_tasks "
            "WHERE issue_id IS NOT NULL AND issue_id <> ''"
        )
        task_map = {}
        runtime_map = {}
        for row in c.fetchall():
            task_map[(row[0], row[1])] = row[2]
            runtime_map[(row[0], row[1])] = row[3] or 0

        # 查 runtime_id → 引擎实例名映射（type-device_name 格式）
        c.execute(
            "SELECT ar.id, ar.name, ar.credential_id, rc.device_name, ar.host_name "
            "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON ar.credential_id = rc.id"
        )
        from src.runtime.instances import instance_name as _inst_name
        import socket as _sock
        _local_hostname = ""
        try:
            _local_hostname = _sock.gethostname()
        except Exception:
            pass
        rt_name_map = {}
        for r in c.fetchall():
            _rid, _name, _cid, _dev, _host = r
            # 本机引擎（cid=0）：优先用 host_name，其次用本机 hostname
            if (_cid or 0) == 0:
                _dev = _host or _local_hostname or ""
            rt_name_map[_rid] = _inst_name(_name, _dev, _cid)

    # 按 issue_id 分组（同时记录每个 issue 的 repo_key）
    group = {}
    issue_repo = {}
    for row in rows:
        seq, event_type, issue_id, title, actor, agent_type, skill, status, duration, created_at, payload, ev_repo_key = row
        if issue_id not in group:
            group[issue_id] = []
            if ev_repo_key:
                issue_repo[issue_id] = ev_repo_key
        _rt_id = runtime_map.get((issue_id, seq), 0)
        group[issue_id].append({
            "seq": seq, "eventType": event_type, "agentType": agent_type or "",
            "skill": skill or "", "status": status or "",
            "duration": duration or 0, "createdAt": normalize_event_time(created_at),
            "actor": actor or "", "title": title or "",
            "taskId": task_map.get((issue_id, seq), ""),
            "engine": rt_name_map.get(_rt_id, "") if _rt_id else "",
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
            "repoKey": issue_repo.get(issue_id, repo_key),
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


def _check_host_status(owner: str, repo: str, repo_key: str) -> str:
    """方案 X（F-2 落地）：纯本地判定托管状态，零 GitCode API 调用。

    - webhook 检测：读本地 managed_repos.webhook_id，>0 即视为存在（不远程比对 URL，杜绝 URL 漂移误报）。
    - bot 检测：读本地已落库的 host_status；仅当原值为 bot_missing/partial 且 bot 实际应存在时
      维持原值（不额外远程查权限，避免限流）。
    - 不调用任何远程 API，避免 429；真实"webhook 被手动删除"的漏报由 repair-host 手动确认兜底。
    """
    from src.config import log as _log
    try:
        from src.user.db import get_host_status, set_host_status, _get_managed_webhook_id
        webhook_id = _get_managed_webhook_id(repo_key)
        cur = get_host_status(repo_key)
        if not webhook_id:
            # webhook 从未建立 → webhook_missing
            if cur != "webhook_missing":
                set_host_status(repo_key, "webhook_missing")
            return "webhook_missing"
        # webhook 存在：
        if cur in ("bot_missing", "partial"):
            # bot 缺失/部分是托管时落库的明确标记，保留（不远程复查）
            return cur
        # 正常（含曾误标 webhook_missing → 现在 webhook_id>0 存在 → 清掉误报）
        if cur != "":
            set_host_status(repo_key, "")
        return ""
    except Exception as e:
        _log.warning(f"Dashboard: host status check failed: {e}")
        return ""


@router.get("/{owner}/{repo}")
def get_dashboard(owner: str, repo: str, user: dict = Depends(get_current_user)):
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

    # 获取当前场景和场景列表（从 DB 读取，方案 B：无 default 场景）
    dev_scene = ""
    try:
        with db_lock:
            c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            if row and row[0]:
                dev_scene = row[0]
    except Exception:
        pass

    scenes = []
    scene_nodes = []
    try:
        from src.api.pipeline_config import get_scenes, get_pipeline_config
        scenes = get_scenes(repo_key=repo_key)
        # dev_scene 必须在可见场景列表中才加载，否则自动选中第一个场景
        scene_ids = {s["id"] for s in scenes}
        if scenes:
            if not dev_scene or dev_scene not in scene_ids:
                dev_scene = scenes[0]["id"]
            cfg = get_pipeline_config(dev_scene, repo_key=repo_key)
            if cfg:
                scene_nodes = cfg.get("nodes", [])
        else:
            dev_scene = ""
    except Exception as e:
        log.warning(f"Dashboard: scene load failed: {e}")

    # 为每个 agent 附带场景信息（skill / enabled_in_scene / engine_runtime_ids）
    # engine_runtime_ids 只从场景 node 读取（为空 = 全广播/兜底）
    scene_by_agent = {n.get("tag") or n.get("id", ""): n for n in scene_nodes}
    # 构建 skill_name → display_name 映射（官方 skill 优先）
    skill_display_map = {}
    try:
        with db_lock:
            c.execute("SELECT skill_name, display_name FROM skill WHERE is_active=1 AND scope='official'")
            skill_display_map = {r[0]: r[1] or r[0] for r in c.fetchall()}
    except Exception:
        pass
    for ag in agent_sessions:
        at = ag.get("agent_type", "")
        node = scene_by_agent.get(at)
        if node:
            _sk = node.get("skill", "")
            ag["skill"] = _sk
            ag["skill_display_name"] = skill_display_map.get(_sk, _sk)
            ag["enabled_in_scene"] = node.get("enabled", True)
            ag["engine_runtime_ids"] = node.get("engine_runtime_ids", []) or []
        else:
            ag["skill"] = ""
            ag["skill_display_name"] = ""
            ag["enabled_in_scene"] = False
            ag["engine_runtime_ids"] = []

    # 获取可用引擎列表（供前端 engine 下拉）
    # 按 agent_type 标签过滤：只返回 tags 包含该 agent_type 或 "all" 的引擎
    agent_engines = {}
    try:
        from src.runtime.engine_select import query_engines
        from src.runtime.instances import instance_name
        _uname = user.get("username") or ""
        for atype in all_types:
            engines = (query_engines(repo_key, "global", agent_type=atype)
                       + query_engines(repo_key, "user", agent_type=atype, username=_uname)
                       + query_engines(repo_key, "repo", agent_type=atype)
                       + query_engines(repo_key, "local", agent_type=atype, username=_uname))
            _scope_labels = {"global": "全局", "user": "个人", "repo": "仓库", "local": "本机"}
            from src.runtime.health import derive_runtime_health
            import time as _time
            _now = _time.time()
            agent_engines[atype] = [{"id": e["runtime_id"],
                                     "name": instance_name(e["name"], e.get("device_name", ""), e.get("credential_id", 0)),
                                     "device": e.get("device_name", ""), "cred_name": e.get("owner_name", ""),
                                     "status": derive_runtime_health(e["status"], e.get("last_seen_at"), _now),
                                     "tags": e.get("tags", []),
                                     "scope": e.get("scope", "global"),
                                     "scope_label": _scope_labels.get(e.get("scope", "global"), "")}
                                    for e in engines]
    except Exception as e:
        log.warning(f"dashboard agent_engines failed: {e}")

    # 真实指标统计（替代假数据）
    metrics = {"total_events": len(recent_events), "active_agents": 0,
               "pending_events": event_counts["pending"], "failed_events": event_counts["failed"],
               "auto_resolution": 0.0, "events_week": 0, "avg_response": 0}
    try:
        with db_lock:
            # total_issues: 统计不同 issue_id 的数量（去重）
            c.execute(
                "SELECT COUNT(DISTINCT issue_id) FROM events WHERE repo_key=? AND issue_id!='' AND issue_id IS NOT NULL",
                (repo_key,))
            metrics["total_issues"] = c.fetchone()[0] or 0
            # Auto-Resolution: completed / (completed + failed)
            c.execute(
                "SELECT COUNT(*) FROM events WHERE repo_key=? AND status='completed'",
                (repo_key,)
            )
            completed = c.fetchone()[0] or 0
            c.execute(
                "SELECT COUNT(*) FROM events WHERE repo_key=? AND status='failed'",
                (repo_key,)
            )
            failed = c.fetchone()[0] or 0
            total_done = completed + failed
            metrics["auto_resolution"] = round(completed / total_done * 100, 1) if total_done > 0 else 0.0

            # Events/Week: 最近 7 天事件数
            c.execute(
                "SELECT COUNT(*) FROM events WHERE repo_key=? AND created_at >= datetime('now', '-7 days')",
                (repo_key,)
            )
            metrics["events_week"] = c.fetchone()[0] or 0

            # Avg Response: 已完成事件的平均 duration（秒）
            c.execute(
                "SELECT AVG(duration) FROM events WHERE repo_key=? AND status='completed' AND duration IS NOT NULL AND duration > 0",
                (repo_key,)
            )
            avg_dur = c.fetchone()[0]
            metrics["avg_response"] = round(avg_dur, 1) if avg_dur else 0
    except Exception:
        pass

    metrics["active_agents"] = sum(1 for a in agent_sessions if a["status"] == "running")

    # 方案 F-2：实时复查托管状态（bot 是否维护者 + webhook 是否存在），更新 host_status
    host_status = _check_host_status(owner, repo, repo_key)

    # 读取当前场景的高级选项（工作分支 + 自定义 key:value）
    advanced_options = {}
    try:
        if dev_scene:
            with db_lock:
                c.execute(
                    "SELECT advanced_options FROM devops_pipeline_scene "
                    "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                    (dev_scene, repo_key))
                _adv_row = c.fetchone()
            if _adv_row and _adv_row[0]:
                import json as _json
                advanced_options = _json.loads(_adv_row[0])
    except Exception:
        pass

    return {
        "repo": repo_key,
        "role": role,
        "dev_scene": dev_scene,
        "advanced_options": advanced_options,
        "_scenes": scenes,
        "metrics": metrics,
        "agents": agent_sessions,
        "agent_engines": agent_engines,
        "scene_nodes": scene_nodes,
        "host_status": host_status,
    }
