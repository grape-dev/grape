"""Runtime API 路由。

M2：任务状态轮询 / 取消（用户 token）。
M3：/api/runtime/* 协议端点（register/heartbeat/claim/report），仅 rta_ 凭据可过（D2 凭据隔离）。
见 agent-runtime-event-full-design.md §6 / tech-impl.md §3。
"""
import hashlib, json, os, threading
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from src.api.deps import require_no_session_token
from src.config import log
from src.api.time_utils import normalize_event_time
from src.session_manager import db, db_lock
from src.runtime import service

router = APIRouter(prefix="/api/runtime", tags=["runtime"])

# Worker 包下载并发限制（防止多 worker 同时下载占满带宽）
_PKG_DOWNLOAD_LIMIT = int(os.environ.get("PKG_DOWNLOAD_MAX_CONCURRENT", "5"))
_pkg_download_count = 0
_pkg_download_lock = threading.Lock()


# ===== rta_ 凭据认证中间件（M3，D2 凭据隔离）=====
def _auth_credential(request: Request) -> int:
    """解析 Authorization: Bearer rta_... → sha256 → 查 runtime_credentials(status=active)。返回 credential_id。"""
    token = (request.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    if not token.startswith("rta_"):
        raise HTTPException(status_code=401, detail={"code": 401, "message": "invalid runtime credential"})
    h = hashlib.sha256(token.encode()).hexdigest()
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM runtime_credentials WHERE token_hash=? AND status='active'", (h,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "runtime credential revoked or not found"})
    return row[0]


def _client_ip(request: Request) -> str:
    """解析来源 IP（经 nginx 反代优先 X-Forwarded-For；仅展示用，不入唯一键，见 multi-runtime-instance-design §3.2）。"""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


class RuntimeRegBody(BaseModel):
    device_name: str = ""
    cli_version: str = ""
    runtimes: list[dict] = []
    local_skills: list[dict] = []


class RuntimeInfo(BaseModel):
    name: str
    version: str = ""
    healthy: bool = True


class LogLineBody(BaseModel):
    ts: str = ""
    level: str = ""
    message: str = ""


class HeartbeatBody(BaseModel):
    runtime_ids: list[int] = []
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    disk_percent: float = 0.0
    tmp_percent: float = 0.0
    bandwidth_percent: float = 0.0
    bandwidth_mbps: float = 0.0
    load_avg: str = ""
    spec: str = ""
    log_lines: list[LogLineBody] = []
    probe_results: dict = {}  # {engine_name: {status, latency, last_probe, error}}


class TaskStartBody(BaseModel):
    pass


class ProgressBody(BaseModel):
    summary: str = ""
    step: int = 0
    total: int = 0


class MessagesBody(BaseModel):
    messages: list = []


class UsageBody(BaseModel):
    provider: str = ""
    model: str = ""
    input: int = 0
    output: int = 0
    cache: int = 0


class CompleteBody(BaseModel):
    result_text: str = ""
    exit_code: int = 0
    session_id: str = ""
    work_dir: str = ""
    engine: str = ""


class FailBody(BaseModel):
    error: str = ""
    failure_reason: str = ""
    exit_code: int = -1


# ===== 认证中间件 =====

def _require_credential(request: Request) -> int:
    return _auth_credential(request)


# ===== 协议端点（M3）=====

@router.post("/register")
def runtime_register(body: RuntimeRegBody, request: Request, credential_id: int = Depends(_require_credential)):
    """凭据校验 + 按实例键 upsert agent_runtimes（幂等），返回 server 权威 runtime IDs。

    多实例键（multi-runtime-instance-design §3.1）：每台 worker（凭据）对每个引擎独占一行，
    多 worker 注册同名引擎并存，互不覆盖 cli_path/version/credential_id。
    """
    registered = []
    client_ip = _client_ip(request)
    with db_lock:
        c = db.cursor()
        # 回填 device_name + 来源 IP
        c.execute("UPDATE runtime_credentials SET device_name=?, last_seen_at=?, last_ip=? WHERE id=?",
                  (body.device_name, datetime.now(timezone.utc).isoformat(), client_ip, credential_id))
        # 凭据作用域决定 runtime 归属：user scope 的个人 worker 注册为 user scope + owner_id；
        # repo scope 用凭据行 owner_id（仓库全名），否则 set_chat_config / Engines 视图的
        # scope 判定失效（个人/仓库级引擎永远选不中，见 engine-selection §7.1）
        c.execute("SELECT scope, owner_name, owner_id, repo_key, org_id FROM runtime_credentials WHERE id=?", (credential_id,))
        cred_row = c.fetchone()
        cred_scope = (cred_row[0] if cred_row and cred_row[0] else "global").strip().lower()
        owner_id = (cred_row[2] or "") if cred_row else ""
        cred_repo_key = (cred_row[3] or "") if cred_row else ""
        cred_org_id = (cred_row[4] or 0) if cred_row else 0
        if cred_scope == "user" and cred_row and cred_row[1]:
            # owner_id 与 user_accounts.id 对齐（按用户名），找不到保持空
            try:
                c.execute("SELECT id FROM user_accounts WHERE username=? LIMIT 1", (cred_row[1],))
                urow = c.fetchone()
                if urow:
                    owner_id = str(urow[0])
            except Exception:
                pass
        # local_skills：per-engine skill 列表，按 engine name 匹配 runtime 行
        skills_by_name = {}
        for ls in body.local_skills or []:
            eng = (ls.get("engine") or "").strip().lower()
            if eng:
                skills_by_name[eng] = json.dumps(ls.get("skills") or [], ensure_ascii=False)
        for rt in body.runtimes or []:
            name = (rt.get("name") or "").strip().lower()
            if not name:
                continue
            _ls_json = skills_by_name.get(name, "[]")
            c.execute("SELECT id FROM agent_runtimes WHERE credential_id=? AND lower(name)=? AND scope=? AND owner_id=? LIMIT 1",
                      (credential_id, name, cred_scope, owner_id))
            row = c.fetchone()
            if row:
                rid = row[0]
                c.execute("UPDATE agent_runtimes SET version=?, healthy=?, installed=1, status='online', last_seen_at=?, cli_path=?, local_skills=?, worker_version=? WHERE id=?",
                          (rt.get("version", ""), 1 if rt.get("healthy", True) else 0,
                           datetime.now(timezone.utc).isoformat(), rt.get("cli_path", ""), _ls_json, body.cli_version, rid))
            else:
                c.execute(
                    "INSERT INTO agent_runtimes (credential_id, name, display_name, `binary`, version, healthy, installed, status, last_seen_at, cli_path, scope, owner_id, local_skills, repo_key, worker_version, org_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (credential_id, name, rt.get("display_name", name), name, rt.get("version", ""),
                     1 if rt.get("healthy", True) else 0, 1, "online",
                     datetime.now(timezone.utc).isoformat(), rt.get("cli_path", ""), cred_scope, owner_id, _ls_json, cred_repo_key, body.cli_version, cred_org_id))
                rid = c.lastrowid
            # 自动写入默认 "all" 标签——新引擎默认可认领任意角色任务。
            # 若已有 capabilities 行则跳过（用户手动配置过标签的不覆盖）。
            # tags_user_set=1 时也跳过（用户已手动清空/设过标签，心跳不再自动补 'all'）
            c.execute("SELECT tags_user_set FROM agent_runtimes WHERE id=?", (rid,))
            _ts_row = c.fetchone()
            _user_set = (_ts_row[0] if _ts_row else 0) or 0
            c.execute("SELECT 1 FROM engine_capabilities WHERE runtime_id=? LIMIT 1", (rid,))
            if not c.fetchone() and not _user_set:
                from datetime import datetime as _dt, timezone as _tz
                _now_ec = _dt.now(_tz.utc).isoformat()
                c.execute(
                    "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled, created_at, updated_at) "
                    "VALUES (?, 'all', 1, 50, 1, ?, ?)",
                    (rid, _now_ec, _now_ec))
            registered.append({"id": rid, "name": name})
        db.commit()
    # v3.0：probe_url——启动可达性探测（复用任一已转存技能对象签名，
    # 验证完整链路：网络+签名+读取；空库返回空，worker 声明能力 false）
    probe_url = ""
    try:
        from src.storage import get_storage
        _storage = get_storage()
        if _storage is not None:
            with db_lock:
                _c = db.cursor()
                _row = _c.execute(
                    "SELECT file_path FROM skill WHERE file_path != '' AND is_active=1 LIMIT 1").fetchone()
            if (_row and _row[0]):
                probe_url = _storage.temporary_url(_row[0], expires=int(os.environ.get("RUNTIME_PROBE_URL_TTL", "300")))
    except Exception:
        pass
    return {"runtimes": registered, "server_url": _server_url(), "probe_url": probe_url}


@router.post("/heartbeat")
def runtime_heartbeat(body: HeartbeatBody, request: Request, credential_id: int = Depends(_require_credential)):
    """更新 runtime 在线态 / last_seen_at。

    归属校验：只允许更新当前凭据名下的 runtime（credential_id 匹配），
    防止已吊销设备的旧 runtime id 被其它 active 凭据的心跳刷回 online
    （revoke 物理删除 agent_runtimes 行；无校验则吊销的设备行会被残留 agent 复活）。

    响应携带 cancelled_tasks 列表——worker 收到后立即杀引擎进程，
    实现任务取消实时感知（延迟 15 秒，覆盖 step 很长时 report_messages 410 不触发的场景）。
    """
    now = datetime.now(timezone.utc).isoformat()
    client_ip = _client_ip(request)
    cancelled_tasks = []
    with db_lock:
        c = db.cursor()
        # spec 为空时不覆盖（兼容旧版 worker 不上报 spec）
        if body.spec:
            c.execute("UPDATE runtime_credentials SET last_seen_at=?, last_ip=?, cpu_percent=?, memory_percent=?, disk_percent=?, tmp_percent=?, bandwidth_percent=?, bandwidth_mbps=?, load_avg=?, spec=? WHERE id=?",
                      (now, client_ip, body.cpu_percent, body.memory_percent, body.disk_percent, body.tmp_percent, body.bandwidth_percent, body.bandwidth_mbps, body.load_avg, body.spec, credential_id))
        else:
            c.execute("UPDATE runtime_credentials SET last_seen_at=?, last_ip=?, cpu_percent=?, memory_percent=?, disk_percent=?, tmp_percent=?, bandwidth_percent=?, bandwidth_mbps=?, load_avg=? WHERE id=?",
                      (now, client_ip, body.cpu_percent, body.memory_percent, body.disk_percent, body.tmp_percent, body.bandwidth_percent, body.bandwidth_mbps, body.load_avg, credential_id))
        if body.runtime_ids:
            ph = ",".join("?" for _ in body.runtime_ids)
            c.execute(f"UPDATE agent_runtimes SET status='online', last_seen_at=? WHERE id IN ({ph}) AND credential_id=?",
                      [now] + body.runtime_ids + [credential_id])
            # worker 上报的引擎探测结果写入 DB（probe_* 列已由 db_setup.py 自动迁移添加）
            if body.probe_results:
                for _pname, _pdata in body.probe_results.items():
                    _pstatus = (_pdata.get("status") or "pending") if isinstance(_pdata, dict) else "pending"
                    _platency = (_pdata.get("latency") or 0) if isinstance(_pdata, dict) else 0
                    _plast = (_pdata.get("last_probe") or now) if isinstance(_pdata, dict) else now
                    _perror = (_pdata.get("error") or "") if isinstance(_pdata, dict) else ""
                    # probe_status 联动 healthy：ok→升级（纠正 --version 误判），fail→降级（实际不能干活）
                    # skip/pending 不动 healthy，保留 --version 的结果
                    if _pstatus == "ok":
                        c.execute(
                            "UPDATE agent_runtimes SET probe_status=?, probe_latency=?, probe_last=?, probe_error=?, "
                            "probe_fail_count=0, healthy=1 "
                            "WHERE name=? AND credential_id=?",
                            (_pstatus, _platency, _plast, _perror, _pname, credential_id)
                        )
                    elif _pstatus == "fail":
                        c.execute(
                            "UPDATE agent_runtimes SET probe_status=?, probe_latency=?, probe_last=?, probe_error=?, "
                            "probe_fail_count=probe_fail_count+1, "
                            "healthy=CASE WHEN probe_fail_count+1>=2 THEN 0 ELSE healthy END "
                            "WHERE name=? AND credential_id=?",
                            (_pstatus, _platency, _plast, _perror, _pname, credential_id)
                        )
                    else:
                        c.execute(
                            "UPDATE agent_runtimes SET probe_status=?, probe_latency=?, probe_last=?, probe_error=? "
                            "WHERE name=? AND credential_id=?",
                            (_pstatus, _platency, _plast, _perror, _pname, credential_id)
                        )
            # 查该 worker 的已取消任务——通知 worker 立即杀进程
            ph2 = ",".join("?" for _ in body.runtime_ids)
            rows = c.execute(
                f"SELECT id FROM agent_tasks WHERE runtime_id IN ({ph2}) AND status='cancelled'",
                body.runtime_ids
            ).fetchall()
            cancelled_tasks = [r[0] for r in rows]

            # busy_slots 对账（2026-08-29 修复）：
            # 原代码只在 claim 成功时对账，但 claim 不到任务时永远不对账 → 泄漏永久不修复。
            # 在心跳中定期对账：busy_slots 与实际 running/dispatched 任务数不一致时重置。
            try:
                for _rid in body.runtime_ids:
                    c.execute(
                        "SELECT COUNT(*) FROM agent_tasks "
                        "WHERE runtime_id=? AND status IN ('dispatched','running')",
                        (_rid,))
                    _actual = c.fetchone()[0] or 0
                    c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (_rid,))
                    _busy = c.fetchone()[0] or 0
                    if _busy != _actual:
                        log.info(f"heartbeat busy_slots reconcile: runtime={_rid} "
                                 f"busy={_busy} actual={_actual} -> reset")
                        c.execute("UPDATE agent_runtimes SET busy_slots=? WHERE id=?",
                                  (_actual, _rid))
            except Exception as _bre:
                log.warning(f"heartbeat busy_slots reconcile failed: {_bre}")
        db.commit()

    # 写入 worker 上报的日志到 data/logs/workers/{credential_id}.log
    if body.log_lines:
        import os as _os, json as _json
        from src.config import LOG_DIR
        _worker_log_dir = _os.path.join(LOG_DIR, "workers")
        _os.makedirs(_worker_log_dir, exist_ok=True)
        # 查 worker 名称
        _wname = ""
        try:
            with db_lock:
                c2 = db.cursor()
                c2.execute("SELECT name, device_name FROM runtime_credentials WHERE id=?", (credential_id,))
                row = c2.fetchone()
                if row:
                    _wname = row[1] or row[0] or f"worker-{credential_id}"
        except Exception:
            _wname = f"worker-{credential_id}"
        _wf = _os.path.join(_worker_log_dir, f"{credential_id}.log")
        with open(_wf, "a") as _wf_fp:
            for ll in body.log_lines:
                _entry = {
                    "ts": ll.ts or now,
                    "level": ll.level,
                    "component": "worker",
                    "request_id": "-",
                    "message": ll.message,
                    "worker_id": credential_id,
                    "worker_name": _wname,
                }
                _wf_fp.write(_json.dumps(_entry, ensure_ascii=False) + "\n")

    # 返回 worker 代码版本（仅 git commit，不含 wheels/python 指纹）
    # worker 端对比此版本号决定是否自动升级
    from src.worker_package import worker_code_version
    _latest_ver = worker_code_version()

    return {"status": "ok", "cancelled_tasks": cancelled_tasks, "latest_version": _latest_ver}


@router.get("/admin/event-bus", operation_id="runtime_event_bus", summary="Event bus debug")
def event_bus_debug(user: dict = Depends(require_no_session_token)):
    """Debug endpoint: show event bus history and listeners."""
    from src.api.event_bus import get_event_bus
    bus = get_event_bus()
    return {
        "history": bus.get_history(),
        "listeners": {k: len(v) for k, v in bus._listeners.items()},
        "total_events": len(bus.get_history()),
    }


# ===== Issue #134: 机器健康度查询 API =====

@router.get("/admin/machine-health", operation_id="runtime_machine_health", summary="Machine health overview", description="Machine health overview for all engines: {runtime_id, name, health_score, total_tasks, failed_tasks, consecutive_failures, failure_rate, ...}.")
def machine_health_overview(user: dict = Depends(require_no_session_token)):
    """获取所有引擎的机器健康度概览（供前端/运维查看）。

    返回 [{runtime_id, name, health_score, total_tasks, failed_tasks,
           consecutive_failures, failure_rate, ...}]
    """
    from src.runtime.health_scorer import get_all_health_scores
    from src.runtime.instances import runtime_instance_name
    scores = get_all_health_scores()
    # 补充引擎名
    for s in scores:
        s["engine_name"] = runtime_instance_name(s.get("runtime_id", 0))
    return {"engines": scores}


@router.get("/admin/machine-health/{runtime_id}", operation_id="runtime_machine_health_detail", summary="Single runtime health detail", description="Health detail for a single engine.")
def machine_health_detail(runtime_id: int, user: dict = Depends(require_no_session_token)):
    """获取单个引擎的健康度详情。"""
    from src.runtime.health_scorer import get_health_score
    from src.runtime.instances import runtime_instance_name
    data = get_health_score(runtime_id, force_refresh=True)
    data["engine_name"] = runtime_instance_name(runtime_id)
    return data


@router.post("/admin/batch-recover", operation_id="runtime_batch_recover", summary="Batch recover stale tasks")
def batch_recover_stale_tasks_api(user: dict = Depends(require_no_session_token)):
    """Manually trigger batch stale task recovery (Phase 5)."""
    from src.runtime.service import batch_recover_stale_tasks
    result = batch_recover_stale_tasks()
    return result


class RuntimeClaimBody(BaseModel):
    """v3.0：worker 能力动态声明（零落库）——claim 每次携带"""
    supports_obs_url: bool = False   # 本 worker 支持 OBS 直连（启动探测结果）
    prefer_fallback: bool = False    # 直连不稳，服务端直接给代拉模式


def _attach_skill_obs(task: dict) -> None:
    """v3.0：任务附 OBS 直连信息（claim 现签 URL——file_path 权威、永远最新）。

    skill_obs_url：预签名 URL（TTL env RUNTIME_SKILL_URL_TTL，默认 1800s）
    skill_key：对象键（worker 侧可记录/排查用）
    mode：obs（直连）/ ""（无 OBS——worker 走 skill_files）
    """
    try:
        from src.storage import get_storage
        _storage = get_storage()
        if _storage is None:
            return
        skill = (task.get("skill") or "").strip()
        if not skill:
            return
        from src.user.db import resolve_skill_id, get_skill_object_key, get_skill_by_id
        _skill_id = task.get("skill_id") or 0
        _task_repo = task.get("repo_key") or ""
        _task_uid = task.get("user_id") or ""
        if _skill_id:
            _sk = get_skill_by_id(_skill_id)
            if _sk:
                _okey = _sk.get("file_path") or ""
            else:
                # stale skill_id → 回退按名字解析
                _sid = resolve_skill_id(skill, repo_key=_task_repo, user_id=_task_uid)
                _okey = get_skill_object_key(_sid) if _sid else ""
        else:
            _sid = resolve_skill_id(skill, repo_key=_task_repo, user_id=_task_uid)
            _okey = get_skill_object_key(_sid) if _sid else ""
        if not _okey:
            return
        ttl = int(os.environ.get("RUNTIME_SKILL_URL_TTL", "1800"))
        task["skill_key"] = _okey
        task["skill_obs_url"] = _storage.temporary_url(_okey, expires=ttl)
        task["mode"] = "obs"
    except Exception as e:
        log.warning(f"attach skill_obs failed: {e}")


@router.post("/runtimes/{runtime_id}/tasks/claim")
def runtime_claim(runtime_id: int, body: RuntimeClaimBody | None = None,
                        credential_id: int = Depends(_require_credential)):
    """按 runtime_id 拉取队首任务或 null（确定路由 D3 + 串行槽 D9）。

    v3.0：能力声明（supports_obs_url/prefer_fallback）动态携带；
    任务附 skill_obs_url（claim 现签预签名 URL）——prefer_fallback
    worker 直接给 mode=server-fetch（跳过 URL 直连尝试）。
    """
    task = service.claim(runtime_id)
    # 检查是否有 pending skill scan 指令
    scan_request = False
    if not task:
        # 空闲时检查 scan 指令（有 task 时不打扰执行流程，下次空闲 claim 再带）
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT pending_skill_scan FROM agent_runtimes WHERE id=? AND credential_id=?",
                          (runtime_id, credential_id))
                _row = c.fetchone()
                if _row and _row[0]:
                    scan_request = True
        except Exception:
            pass
    if task:
        if body and body.prefer_fallback:
            task["mode"] = "server-fetch"
        else:
            _attach_skill_obs(task)
    return {"task": task, "scan_request": scan_request}


@router.get("/runtimes/{runtime_id}/tags")
def runtime_get_tags(runtime_id: int, credential_id: int = Depends(_require_credential)):
    """Return this engine's claimable tags from DB (worker polls this instead of ENGINE_TAGS env).

    Tags are managed by users via the console (settings.py CRUD endpoints).
    Workers call this endpoint to get their current tag list, enabling real-time
    tag updates without restart.
    """
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 ORDER BY tag",
                (runtime_id,)
            )
            tags = [r[0] for r in c.fetchall()]
        except Exception:
            tags = []
    return {"tags": tags}


@router.get("/tasks/{task_id}/env")
def runtime_task_env(task_id: str, credential_id: int = Depends(_require_credential)):
    """Return latest env (AK/SK + credentials) for a task from DB.

    Workers call this before executing to get fresh credentials,
    instead of relying on the env_extra snapshot from task creation time.
    """
    from src.session_manager import db, db_lock
    from src.runtime.service import _collect_server_env
    with db_lock:
        c = db.cursor()
        c.execute("SELECT user_id, account FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
    if not row:
        return {"env": {}}
    user_id, account = row[0] or 0, row[1] or ""
    env = _collect_server_env(user_id, account)
    return {"env": env}


@router.post("/runtimes/{runtime_id}/tasks/claim-by-tag/{tag}")
def runtime_claim_by_tag(runtime_id: int, tag: str,
                               body: RuntimeClaimBody | None = None,
                               credential_id: int = Depends(_require_credential)):
    """Claim task by tag + scope isolation (event-dispatch-redesign-v2.md 2.4).

    Workers with engine tags can claim tasks matching their tag and scope.
    """
    task = service.claim_by_tag(tag, runtime_id)
    # 检查是否有 pending skill scan 指令（与 claim 端点一致）
    scan_request = False
    if not task:
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT pending_skill_scan FROM agent_runtimes WHERE id=? AND credential_id=?",
                          (runtime_id, credential_id))
                _row = c.fetchone()
                if _row and _row[0]:
                    scan_request = True
        except Exception:
            pass
    if task:
        if body and body.prefer_fallback:
            task["mode"] = "server-fetch"
        else:
            _attach_skill_obs(task)
    return {"task": task, "scan_request": scan_request}


@router.post("/tasks/{task_id}/skill-zip")
def runtime_task_skill_zip(task_id: str, credential_id: int = Depends(_require_credential)):
    """v3.0 服务端代拉（降级链 ②）：worker URL 直连失败的兜底。

    服务端现查 file_path → get_skill_zip_bytes（自动走 zip_cache：
    命中零 OBS / miss 拉 OBS 更新缓存）→ base64 返回。
    """
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    skill = (task.get("skill") or "").strip()
    if not skill:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "task has no skill"})
    try:
        from src.user.db import resolve_skill_id, get_skill_object_key, get_skill_by_id
        from src.storage import get_skill_zip_bytes
        _skill_id = task.get("skill_id") or 0
        _task_repo = task.get("repo_key") or ""
        _task_uid = task.get("user_id") or ""
        if _skill_id:
            _sk = get_skill_by_id(_skill_id)
            if _sk:
                _okey = _sk.get("file_path") or ""
            else:
                _sid = resolve_skill_id(skill, repo_key=_task_repo, user_id=_task_uid)
                _okey = get_skill_object_key(_sid) if _sid else ""
        else:
            _sid = resolve_skill_id(skill, repo_key=_task_repo, user_id=_task_uid)
            _okey = get_skill_object_key(_sid) if _sid else ""
        if not _okey:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "skill not on OBS"})
        _zip = get_skill_zip_bytes(_okey)
        if not _zip:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "OBS fetch failed"})
        import base64 as _b64
        return {"zip_base64": _b64.b64encode(_zip).decode()}
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"skill-zip fetch failed: {e}")
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"fetch failed: {e}"})


@router.post("/tasks/{task_id}/start")
def runtime_task_start(task_id: str, credential_id: int = Depends(_require_credential)):
    t = service.get_task(task_id)
    if not t:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    service.update_task_status(task_id, "running")
    return {"status": "running"}


@router.post("/tasks/{task_id}/progress")
def runtime_task_progress(task_id: str, body: ProgressBody, credential_id: int = Depends(_require_credential)):
    service.enqueue_async_report("progress", task_id, {"msg": body.summary, "step": body.step, "total": body.total})
    return {"status": "ok"}


@router.post("/tasks/{task_id}/messages")
def runtime_task_messages(task_id: str, body: MessagesBody, credential_id: int = Depends(_require_credential)):
    # task 已终态（failed/cancelled/completed）时拒绝消息，返回 410 Gone。
    # worker 端 _execute_task 检测到 410 后中断执行（用户终止后残留进程不再继续产出）。
    task = service.get_task(task_id)
    if task and task.get("status") in ("failed", "cancelled", "completed"):
        raise HTTPException(status_code=410, detail={"code": 410, "message": f"task already {task.get('status')}"})
    # 自动修复：worker 上报了消息说明已在执行，但 task_start 可能调用失败
    # 导致 status 仍为 queued/dispatched → 自动更新为 running
    if task and task.get("status") in ("queued", "dispatched"):
        try:
            service.update_task_status(task_id, "running")
            log.info(f"auto-fix: task {task_id} status {task.get('status')} -> running (messages reported)")
        except Exception as e:
            log.warning(f"auto-fix: failed to update task {task_id} to running: {e}")
    service.enqueue_async_report("messages", task_id, body.messages)
    return {"status": "ok"}


@router.post("/tasks/{task_id}/usage")
def runtime_task_usage(task_id: str, body: UsageBody, credential_id: int = Depends(_require_credential)):
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET usage_json=? WHERE id=?",
                  (json.dumps(body.model_dump()), task_id))
        db.commit()
    # 可观察性埋点：LLM token 用量
    try:
        from src.observability import record_llm_usage
        record_llm_usage(body.provider, body.model, body.input, body.output, body.cache)
    except Exception:
        pass
    return {"status": "ok"}


@router.post("/tasks/{task_id}/complete")
def runtime_task_complete(task_id: str, body: CompleteBody, credential_id: int = Depends(_require_credential)):
    """完成（幂等）：解析结构化 output → finalize_event_archive → update_event_complete。

    C（stream-end-finalize-single-signal-design.md）：worker 进程结束 = 流结束，单信号终态。
    落 stream_ended（仪器）+ finalize_by_stream_end（优先已显式 self_report 的结构化结论）；
    不再区分自报/普通技能、不再等双信号。
    """
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    _usage = {"session_id": body.session_id, "work_dir": body.work_dir, "engine": body.engine}
    service.set_task_signal(task_id, "stream_ended")
    service.finalize_by_stream_end(task_id, body.result_text or "", body.exit_code, usage=_usage)
    return {"status": "completed"}


@router.post("/tasks/{task_id}/fail")
def runtime_task_fail(task_id: str, body: FailBody, credential_id: int = Depends(_require_credential)):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    # 透传 worker 上报的真实 exit_code（旧硬编码 1 → 死 session 检测的"连续3次负数"
    # 永不触发，坏指针清不掉）；旧 worker 不传则默认 -1，与 fail_task 签名一致。
    service.fail_task(task_id, body.error or body.failure_reason or "unknown error", exit_code=body.exit_code)
    return {"status": "failed"}


@router.post("/tasks/{task_id}/requeue")
def runtime_task_requeue(task_id: str, credential_id: int = Depends(_require_credential)):
    """客户端并发冲突跳过时回滚任务为 queued（避免 dispatched 悬空卡死，见 seq#622 教训）。"""
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    ok = service.requeue_task(task_id)
    return {"status": "requeued", "recycled": ok}


@router.post("/runtimes/{runtime_id}/recover-orphans")
def runtime_recover_orphans(runtime_id: int, credential_id: int = Depends(_require_credential)):
    """启动/重注册时清理该 runtime 遗留的 dispatched/running 任务（置回 queued）。

    2026-10-08 自噬循环修复（生产 exec#310 实锤）：
    部署类任务流程中包含"重启 master"——master 重启导致 worker 心跳断 3 次
    触发 _reregister → recover_orphans 一刀切把**正在执行**的任务置回 queued
    → worker 重新 claim 从头再跑 → 又重启 master → 循环。任务的本地进程
    从头到尾没死过，不是孤儿。

    修复（与 service.py claim 兜底回收的活跃度检查同一思路）：
    1. 活跃度检查：task_messages 最近 RECOVER_ACTIVITY_WINDOW（1200s，兼容
       hermes 长命令静默窗口 + 分次等待策略）内有新消息 → agent 还在跑，
       跳过回炉（真孤儿不会有新消息）。
    2. attempts 封顶：回炉时 attempts+1，attempts>=3 的任务直接置 failed
       （此前 recover_orphans 不维护 attempts，无限回炉无封顶）。
    """
    RECOVER_ACTIVITY_WINDOW = int(os.environ.get("RECOVER_ACTIVITY_WINDOW", "1200"))
    RECOVER_MAX_ATTEMPTS = int(os.environ.get("RECOVER_MAX_ATTEMPTS", "3"))
    _active_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=RECOVER_ACTIVITY_WINDOW)).isoformat()
    recovered, skipped_active, force_failed = [], [], []
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, attempts FROM agent_tasks WHERE runtime_id=? AND status IN ('dispatched','running')",
            (runtime_id,))
        rows = c.fetchall()
        for tid, attempts in rows:
            # 活跃度检查：近期有 task_messages → 任务在跑，不是孤儿
            try:
                c.execute(
                    "SELECT 1 FROM task_messages WHERE task_id=? AND created_at > ? LIMIT 1",
                    (tid, _active_cutoff))
                if c.fetchone():
                    skipped_active.append(tid)
                    continue
            except Exception:
                pass  # 查询失败保守跳过检查（保持旧行为：回收）
            # attempts 封顶：循环回炉保护
            _att = (attempts or 0) + 1
            if _att >= RECOVER_MAX_ATTEMPTS:
                c.execute(
                    "UPDATE agent_tasks SET status='failed', attempts=?, errors=CONCAT(COALESCE(errors,''), ?) WHERE id=?",
                    (_att, "recover-orphans attempts cap reached (possible self-restart loop)", tid))
                force_failed.append(tid)
            else:
                c.execute(
                    "UPDATE agent_tasks SET status='queued', started_at=NULL, attempts=? WHERE id=?",
                    (_att, tid))
                recovered.append(tid)
        db.commit()
    if recovered or skipped_active or force_failed:
        log.info(f"recover-orphans runtime={runtime_id}: recovered={recovered} "
                 f"skipped_active={skipped_active} force_failed={force_failed}")
    return {"message": "orphans recovered", "recovered": recovered,
            "skipped_active": skipped_active, "force_failed": force_failed}


# ===== 用户态端点（M2，保留）=====

@router.get("/tasks/{task_id}/status", operation_id="runtime_task_status", summary="Task status polling", description="Task status polling (chat front-end D5 typewriter fallback).")
def get_task_status(task_id: str, user: dict = Depends(require_no_session_token)):
    """任务状态轮询（chat 前端用，D5 打字机降级）。"""
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    return {
        "id": task["id"],
        "status": task["status"],
        "result_text": task.get("result_text") or "",
        "exit_code": task.get("exit_code"),
        "errors": task.get("errors") or "",
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
    }


@router.get("/tasks/{task_id}/state")
def runtime_task_state(task_id: str, credential_id: int = Depends(_require_credential)):
    """worker 端轻量状态查询：只返回 status，用于 sid 看门狗检测幽灵任务。

    worker 的 active_sessions 是内存 set，_wrap 的 finally 在 opencode 进程
    hang 住时永远不执行 → sid 永久泄漏 → 同 sid 后续任务全部被假冲突挡住。
    worker 主循环定期调本端点查 active_tasks 里的 task 是否已在服务端终态，
    是则主动清理 active_sessions（seq#1485 根因）。
    """
    task = service.get_task(task_id)
    if not task:
        return {"status": "not_found"}
    return {"status": task.get("status") or ""}


@router.post("/tasks/{task_id}/cancel", operation_id="runtime_task_cancel", summary="Cancel task", description="Cancels a task (status=cancelled); the worker exits on the next heartbeat.")
def cancel_task(task_id: str, user: dict = Depends(require_no_session_token)):
    """取消任务（M2-5）：置 cancelled；执行侧由 worker 感知退出。"""
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    service.cancel_task(task_id)
    return {"message": f"任务 {task_id} 已取消", "status": "cancelled"}


@router.get("/runtimes/{runtime_id}/tasks", operation_id="runtime_tasks_list", summary="List runtime tasks", description="Tasks of a device (front-end device view).")
def list_runtime_tasks(runtime_id: int, user: dict = Depends(require_no_session_token),
                             status: str = "", limit: int = 50, repo: str = ""):
    """设备任务列表（M4 前端设备 [查看任务]）。"""
    from src.session_manager import db, db_lock
    clauses = ["runtime_id=?"]
    params = [runtime_id]
    if status:
        clauses.append("status=?")
        params.append(status)
    if repo:
        _base = repo.split(":", 1)[1] if ":" in repo else repo
        repo_key = f"gitcode:{_base}"
        _rk_list = [f"gitcode:{_base}", f"github:{_base}"]
        clauses.append(f"repo_key IN ({','.join('?' * len(_rk_list))})")
        params.extend(_rk_list)
    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT id, repo_key, agent_type, skill, status, exit_code, result_text, created_at, started_at, completed_at, issue_id, event_seq FROM agent_tasks WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
                  params + [limit])
        return {"tasks": [
            {"id": r[0], "repo_key": r[1], "agent_type": r[2], "skill": r[3],
             "status": r[4], "exit_code": r[5], "result_text": (r[6] or "")[:200],
             "created_at": normalize_event_time(r[7]), "started_at": normalize_event_time(r[8]), "completed_at": normalize_event_time(r[9]),
             "issue_id": r[10] or "", "event_seq": r[11] or 0}
            for r in c.fetchall()
        ]}


@router.get("/runtimes/{runtime_id}/sessions", operation_id="runtime_sessions_list", summary="List runtime sessions", description="Engine sessions filtered by agent_tasks.runtime_id; only sessions with matching tasks are returned.")
def list_runtime_sessions(runtime_id: int, user: dict = Depends(require_no_session_token),
                                repo: str = ""):
    """引擎 session 列表。

    按 agent_tasks.runtime_id 过滤——同一个 session（repo+agent_type）的
    不同 task 可能在不同引擎上执行，因此不按 session.runtime_id 过滤，
    而是查所有 session 后按 task 的 runtime_id 过滤 tasks。
    只返回有匹配 task 的 session。
    """
    from src.session_manager import db, db_lock

    repo_clause = ""
    params: list = []
    if repo:
        _base = repo.split(":", 1)[1] if ":" in repo else repo
        _rk_list = [f"gitcode:{_base}", f"github:{_base}"]
        repo_clause = f" AND repo_key IN ({','.join('?' * len(_rk_list))})"
        params.extend(_rk_list)

    with db_lock:
        c = db.cursor()
        # 查所有 session（按 repo 过滤），不按 session.runtime_id 过滤
        c.execute(
            f"SELECT id, agent_type, repo_key, status, skill, created_at, last_active_at, "
            f"runtime_session_id "
            f"FROM devops_agent_session WHERE 1=1{repo_clause} ORDER BY last_active_at DESC",
            params
        )
        sessions = []
        for r in c.fetchall():
            sid = r[0]
            agent_type = r[1] or ""
            repo_key_raw = r[2] or ""
            # 查该 session 当前正在运行的 task（按 runtime_id 过滤）
            c.execute(
                "SELECT id, issue_id, event_seq, agent_type, status, exit_code, created_at, started_at "
                "FROM agent_tasks WHERE agent_session_id=? AND runtime_id=? "
                "AND status IN ('running','queued','dispatched') ORDER BY created_at DESC LIMIT 1",
                (sid, runtime_id)
            )
            current = None
            cr = c.fetchone()
            if cr:
                current = {
                    "id": cr[0], "issue_id": cr[1] or "", "event_seq": cr[2] or 0,
                    "agent_type": cr[3] or "", "status": cr[4] or "",
                    "created_at": normalize_event_time(cr[5]),
                    "started_at": normalize_event_time(cr[6]),
                }
            # 查该 session 最近已完成的任务（按 runtime_id 过滤）
            c.execute(
                "SELECT id, issue_id, event_seq, agent_type, status, exit_code, created_at, started_at, completed_at "
                "FROM agent_tasks WHERE agent_session_id=? AND runtime_id=? "
                "AND status IN ('completed','failed','cancelled') ORDER BY event_seq DESC LIMIT 20",
                (sid, runtime_id)
            )
            tasks = []
            for tr in c.fetchall():
                tasks.append({
                    "id": tr[0], "issue_id": tr[1] or "", "event_seq": tr[2] or 0,
                    "agent_type": tr[3] or "", "status": tr[4] or "",
                    "exit_code": tr[5], "created_at": normalize_event_time(tr[6]),
                    "started_at": normalize_event_time(tr[7]),
                    "completed_at": normalize_event_time(tr[8]),
                })
            # 只返回有匹配 task 的 session（current_task 或 tasks 非空）
            if not current and not tasks:
                continue
            sessions.append({
                "id": sid,
                "agent_type": agent_type,
                "repo_key": repo_key_raw.split(":", 1)[1] if ":" in repo_key_raw else repo_key_raw,
                "status": "running" if current else "idle",
                "skill": r[4] or "",
                "created_at": normalize_event_time(r[5]),
                "last_active_at": normalize_event_time(r[6]),
                "runtime_session_id": r[7] or "",
                "current_task": current,
                "tasks": tasks,
            })
        return {"sessions": sessions, "total": len(sessions)}


@router.get("/credentials/{cid}/tasks", operation_id="runtime_credential_tasks", summary="Tasks aggregated by credential", description="Tasks aggregated by credential across all its engine instances.")
def list_credential_tasks(cid: int, user: dict = Depends(require_no_session_token),
                                status: str = "", limit: int = 50):
    """按凭据聚合任务（该凭据所有引擎实例的 agent_tasks，见 runtime-admin-two-view-design.md §4.3）。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM agent_runtimes WHERE credential_id=?", (cid,))
        rid_rows = c.fetchall()
    if not rid_rows:
        return {"tasks": []}
    rids = [r[0] for r in rid_rows]
    ph = ",".join("?" for _ in rids)
    clauses = [f"runtime_id IN ({ph})"]
    params = list(rids)
    if status:
        clauses.append("status=?")
        params.append(status)
    params.append(limit)
    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT id, repo_key, agent_type, skill, status, exit_code, result_text, created_at, started_at, completed_at FROM agent_tasks WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
                  params)
        return {"tasks": [
            {"id": r[0], "repo_key": r[1], "agent_type": r[2], "skill": r[3],
             "status": r[4], "exit_code": r[5], "result_text": (r[6] or "")[:200],
             "created_at": normalize_event_time(r[7]), "started_at": normalize_event_time(r[8]), "completed_at": normalize_event_time(r[9])}
            for r in c.fetchall()
        ]}


def _server_url() -> str:
    try:
        from src.user.db import get_server_base_url
        return get_server_base_url()
    except Exception:
        return ""


@router.get("/install")
def runtime_install_script():
    """返回 Worker 一键安装脚本（只读，无敏感信息；token 由调用方以 --token 注入）。

    见 worker-server-delivery-design.md。用法：
      curl -sSL <server>/api/runtime/install | bash -s -- --token <rta_...>
    """
    from fastapi.responses import PlainTextResponse
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                               "scripts", "install-worker.sh")
    if not os.path.isfile(script_path):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "install script not found"})
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "read install script failed"})
    return PlainTextResponse(content, media_type="text/plain")


@router.get("/install.ps1")
def runtime_install_script_ps1():
    """返回 Windows Worker PowerShell 一键安装脚本（只读，无敏感信息）。

    用法（Windows 设备上执行）：
      irm <server>/api/runtime/install.ps1 | iex
    或带参数：
      & ([scriptblock]::Create((irm <server>/api/runtime/install.ps1))) -Token <rta_...> -Server <server>
    """
    from fastapi.responses import PlainTextResponse
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                               "scripts", "install-worker.ps1")
    if not os.path.isfile(script_path):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "install script not found"})
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "read install script failed"})
    return PlainTextResponse(content, media_type="text/plain")


@router.get("/start-worker.ps1")
def runtime_start_worker_script_ps1():
    """返回 Windows Worker 启动脚本 start-worker.ps1（只读）。

    供 install-worker.ps1 下载后写入文件，避免 here-string 转义问题。
    """
    from fastapi.responses import PlainTextResponse
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                               "scripts", "start-worker.ps1")
    if not os.path.isfile(script_path):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "start-worker script not found"})
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "read start-worker script failed"})
    return PlainTextResponse(content, media_type="text/plain")


@router.get("/install/latest")
def runtime_install_latest(credential_id: int = Depends(_require_credential)):
    """返回当前 Worker 代码版本号（rta_ 鉴权）。

    供 worker 启动自检（见 worker-server-delivery-design.md §2.4）。
    返回 worker_code_version（仅 git commit），与 heartbeat latest_version 一致。
    """
    from src.worker_package import worker_code_version
    return {"version": worker_code_version()}


@router.get("/grape-worker.tar.gz")
def runtime_worker_package_public():
    """公开下发 Worker 绿色安装包 tar.gz（无需鉴权）。

    包内仅含 worker 运行代码（无任何敏感信息，凭据由安装命令 --token 注入），
    供「凭据申请页一键安装命令」直接 curl 下载：
      curl -fsSL <server>/api/runtime/grape-worker.tar.gz -o grape-worker.tar.gz
    首次请求动态打包 + 缓存（代码变更自动重建，见 src/worker_package.py）。

    并发限制：同时下载不超过 PKG_DOWNLOAD_MAX_CONCURRENT（默认 3），
    超出返回 429 让 worker 重试。
    """
    global _pkg_download_count
    with _pkg_download_lock:
        if _pkg_download_count >= _PKG_DOWNLOAD_LIMIT:
            raise HTTPException(status_code=429, detail={"code": 429, "message": "too many concurrent downloads, retry later"})
        _pkg_download_count += 1
    try:
        from fastapi.responses import FileResponse
        from src.worker_package import build_package
        try:
            pkg = build_package()
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"build worker package failed: {e}"})
        if not pkg or not os.path.isfile(pkg):
            raise HTTPException(status_code=500, detail={"code": 500, "message": "worker package not found"})
        return FileResponse(pkg, media_type="application/gzip", filename="grape-worker.tar.gz")
    finally:
        with _pkg_download_lock:
            _pkg_download_count -= 1


@router.get("/install/package")
def runtime_install_package(credential_id: int = Depends(_require_credential)):
    """下发 Worker 安装包 tar.gz（rta_ 鉴权，首次请求动态打包 + 缓存）。

    见 worker-server-delivery-design.md §2.1。用法（安装脚本内部调用）：
      curl -fsSL -H "Authorization: Bearer <rta_...>" <server>/api/runtime/install/package
    """
    from fastapi.responses import FileResponse
    from src.worker_package import build_package
    try:
        pkg = build_package()
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"build worker package failed: {e}"})
    if not pkg or not os.path.isfile(pkg):
        raise HTTPException(status_code=500, detail={"code": 500, "message": "worker package not found"})
    return FileResponse(pkg, media_type="application/gzip", filename="grape-worker.tar.gz")


# ===== Engine local_skills 刷新 =====

def _scan_engine_skills_detail(engine_name: str) -> list[dict]:
    """本机扫描指定 engine 的 skill 详情（description / 路径 / 文件数）。"""
    from src.runtime.agent import _scan_local_skills
    import os as _os

    all_skills = _scan_local_skills()
    engine_key = (engine_name or "").strip().lower()
    skill_names = []
    for entry in all_skills:
        if (entry.get("engine") or "").strip().lower() == engine_key:
            skill_names = entry.get("skills") or []
            break

    from src.runtime.os_adapter import engine_skill_dirs, extra_skill_dirs
    scan_dirs = list(engine_skill_dirs(engine_key))
    scan_dirs.extend(extra_skill_dirs())

    skill_details = []
    for sname in skill_names:
        detail = {"name": sname, "description": "", "path": "", "file_count": 0, "provider": engine_name}
        for base_dir in scan_dirs:
            p1 = _os.path.join(base_dir, sname)
            if _os.path.isfile(_os.path.join(p1, "SKILL.md")):
                detail["path"] = p1
                break
            if _os.path.isdir(base_dir):
                for cat in _os.listdir(base_dir):
                    p2 = _os.path.join(base_dir, cat, sname)
                    if _os.path.isfile(_os.path.join(p2, "SKILL.md")):
                        detail["path"] = p2
                        break
                if detail["path"]:
                    break
        if detail["path"]:
            try:
                with open(_os.path.join(detail["path"], "SKILL.md"), "r", encoding="utf-8") as f:
                    content = f.read()
                if content.startswith("---"):
                    end = content.find("---", 3)
                    if end > 0:
                        fm = content[3:end]
                        for line in fm.split("\n"):
                            if line.strip().startswith("description:"):
                                desc = line.split("description:", 1)[1].strip()
                                desc = desc.strip("\"'")
                                if desc:
                                    detail["description"] = desc[:200]
                                break
            except Exception:
                pass
            try:
                detail["file_count"] = sum(1 for _ in _os.walk(detail["path"]))
            except Exception:
                pass
        skill_details.append(detail)
    return skill_details


@router.post("/runtimes/{runtime_id}/refresh-skills", operation_id="runtime_refresh_skills", summary="Rescan runtime skills", description="Rescans an engine's local skills and returns the detail list. Local engines scan directly; remote workers set pending_skill_scan and report on the next claim.")
def runtime_refresh_skills(runtime_id: int, force: bool = False, user: dict = Depends(require_no_session_token)):
    """重新扫描指定 engine 的本地 skill 并返回详情列表。

    本机直连引擎（credential_id=0）：直接扫描本机目录，立即返回结果。
    远端 worker 引擎（credential_id>0）：标记 pending_skill_scan=1，
    worker 下次 claim 时收到 scan_request 指令，执行扫描后回报。
    前端需轮询本接口获取结果（返回 status=pending 时继续轮询）。
    """
    from src.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        c.execute("SELECT name, credential_id, pending_skill_scan, skill_scan_result FROM agent_runtimes WHERE id=?", (runtime_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
    engine_name, cred_id, pending, scan_result = row[0], row[1], row[2], row[3]

    # 本机直连：直接扫描
    if cred_id == 0:
        skill_details = _scan_engine_skills_detail(engine_name)
        _ls_json = json.dumps([s["name"] for s in skill_details], ensure_ascii=False)
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_runtimes SET local_skills=?, skill_scan_result=?, pending_skill_scan=0 WHERE id=?",
                      (_ls_json, json.dumps(skill_details, ensure_ascii=False), runtime_id))
            db.commit()
        return {"skills": skill_details, "count": len(skill_details), "status": "done"}

    # 远端 worker：标记扫描指令，等待 worker 回报
    if pending:
        # 已有 pending 标记 — 检查是否已有结果
        if scan_result:
            return {"skills": json.loads(scan_result), "count": len(json.loads(scan_result)),
                    "status": "done", "cached": True}
        return {"status": "pending", "skills": [], "count": 0}

    # pending=0：如果已有缓存结果且非 force，直接返回（轮询场景下 worker 已回报）
    if scan_result and not force:
        _cached = json.loads(scan_result)
        return {"skills": _cached, "count": len(_cached), "status": "done", "cached": True}

    # 首次扫描或 force 重新扫描：标记 pending
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_runtimes SET pending_skill_scan=1, skill_scan_result='' WHERE id=?", (runtime_id,))
        db.commit()
    return {"status": "pending", "skills": [], "count": 0}


@router.post("/runtimes/{runtime_id}/skills-report")
def runtime_skills_report(runtime_id: int, body: dict, credential_id: int = Depends(_require_credential)):
    """worker 回报 skill 扫描结果。

    worker claim 时收到 scan_request → 执行扫描 → 调本接口回报。
    """
    from src.session_manager import db, db_lock

    # 校验 runtime 归属
    with db_lock:
        c = db.cursor()
        c.execute("SELECT credential_id FROM agent_runtimes WHERE id=?", (runtime_id,))
        row = c.fetchone()
    if not row or row[0] != credential_id:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "runtime not owned by credential"})

    skills = body.get("skills") or []
    skill_names = [s.get("name", "") for s in skills if s.get("name")]
    _ls_json = json.dumps(skill_names, ensure_ascii=False)
    _result_json = json.dumps(skills, ensure_ascii=False)

    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_runtimes SET local_skills=?, skill_scan_result=?, pending_skill_scan=0 WHERE id=?",
                  (_ls_json, _result_json, runtime_id))
        db.commit()
    return {"status": "ok"}


# ===== Engine 角色标签 =====

def _apply_tag_rules(tags: list[str]) -> list[str]:
    """应用标签互斥规则。
    - 全能与其他互斥：有其他标签时去掉全能。
    - 5 个角色全选时自动替换为全能。
    - 允许空标签（不自动回全能）。
    """
    AGENT_TAGS = {"vod", "issue", "coding", "qa", "sre"}
    tag_set = set(tags)
    has_all = "all" in tag_set
    has_others = tag_set & AGENT_TAGS

    if has_all and has_others:
        tag_set.discard("all")
    elif has_others and len(has_others) == len(AGENT_TAGS):
        tag_set = {"all"}

    return sorted(tag_set)


@router.get("/runtimes/{runtime_id}/role-tags", operation_id="runtime_role_tags", summary="Runtime role tags", description="Role tags of an engine (web admin view).")
def get_runtime_tags(runtime_id: int, user: dict = Depends(require_no_session_token)):
    """返回 engine 的角色标签列表（web 管理视图；worker 取 tags 用 GET /runtimes/{id}/tags）。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT agent_tags FROM agent_runtimes WHERE id=?", (runtime_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
    return {"tags": json.loads(row[0]) if row[0] else []}


@router.put("/runtimes/{runtime_id}/tags", operation_id="runtime_tags_set", summary="Set role tags", description="Sets an engine's role tags (mutual-exclusion applied). engine_capabilities is the single source; agent_tags is the derived cache.")
def set_runtime_tags(runtime_id: int, body: dict, user: dict = Depends(require_no_session_token)):
    """设置 engine 的角色标签。应用互斥规则后写入。

    以 engine_capabilities 为单一数据源，agent_tags 列为派生缓存。
    写入顺序：先写 engine_capabilities，再 _sync_agent_tags 回写 agent_tags。
    """
    from src.session_manager import db, db_lock
    from datetime import datetime, timezone
    from src.api.routes.settings import _sync_agent_tags
    tags = _apply_tag_rules(body.get("tags") or [])
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM agent_runtimes WHERE id=?", (runtime_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
        # 先写 engine_capabilities：先删旧标签，再插新标签
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (runtime_id,))
        for tag in tags:
            try:
                c.execute(
                    "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled, created_at, updated_at) "
                    "VALUES (?,?,1,50,1,?,?)",
                    (runtime_id, tag, now, now)
                )
            except Exception:
                pass
        # 标记用户已手动设过标签，心跳注册不再自动补 'all'
        c.execute("UPDATE agent_runtimes SET tags_user_set=1 WHERE id=?", (runtime_id,))
        # 再回写 agent_tags（派生缓存，保持与 engine_capabilities 一致）
        _sync_agent_tags(runtime_id, c)
        db.commit()
    log.info(f"Runtime tags set: rid={runtime_id} tags={tags} (engine_capabilities → agent_tags synced)")
    return {"tags": tags}


class MaxConcurrentBody(BaseModel):
    max_concurrent: int = 20


@router.put("/runtimes/{runtime_id}/max-concurrent", operation_id="runtime_max_concurrent_set", summary="Set max concurrent", description="Sets the engine-level max concurrency.")
def set_runtime_max_concurrent(runtime_id: int, body: MaxConcurrentBody,
                                user: dict = Depends(require_no_session_token)):
    """设置引擎级最大并发数（方案一：并发纯引擎级，标签只做路由）。"""
    from src.session_manager import db, db_lock
    if body.max_concurrent < 1 or body.max_concurrent > 100:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "max_concurrent 必须在 1-100 之间"})
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM agent_runtimes WHERE id=?", (runtime_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
        c.execute("UPDATE agent_runtimes SET max_concurrent=? WHERE id=?", (body.max_concurrent, runtime_id))
        db.commit()
    log.info(f"Runtime max_concurrent set: rid={runtime_id} max_concurrent={body.max_concurrent}")
    return {"max_concurrent": body.max_concurrent}


# ===== 场景标签 =====

@router.get("/scene-tags", operation_id="runtime_scene_tags_list", summary="List scene tags", description="All scenes and their available role tags for the scene picker: [{name, display_name, roles: [...]}].")
def list_scene_tags(user: dict = Depends(require_no_session_token)):
    """返回场景市场所有场景及其可用角色标签（供前端场景选择器使用）。

    返回格式：[{name, display_name, roles: ["vod","issue","coding","qa","sre"]}, ...]
    """
    from src.user.db import list_scene_market
    scenes, _ = list_scene_market(limit=0)
    result = []
    for s in scenes:
        nodes = json.loads(s.get("nodes_json") or "[]") if isinstance(s.get("nodes_json"), str) else (s.get("nodes_json") or [])
        roles = []
        for n in nodes:
            if isinstance(n, dict) and n.get("tag") and n["tag"] not in roles:
                roles.append(n["tag"])
        result.append({
            "name": s["name"],
            "display_name": s.get("display_name") or s["name"],
            "roles": roles,
        })
    return {"scenes": result}


@router.get("/runtimes/{runtime_id}/scene-tags", operation_id="runtime_scene_tags_get", summary="Runtime scene tags", description="Scene tags of an engine.")
def get_runtime_scene_tags(runtime_id: int, user: dict = Depends(require_no_session_token)):
    """返回引擎的场景标签列表。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT scene_tags FROM agent_runtimes WHERE id=?", (runtime_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
    tags = json.loads(row[0]) if row[0] else ["default"]
    return {"scene_tags": tags}


class SceneTagsBody(BaseModel):
    scene_tags: list[str] = ["default"]


@router.put("/runtimes/{runtime_id}/scene-tags", operation_id="runtime_scene_tags_set", summary="Set runtime scene tags", description="Sets an engine's scene tags (metadata constraining selectable role tags).")
def set_runtime_scene_tags(runtime_id: int, body: SceneTagsBody,
                            user: dict = Depends(require_no_session_token)):
    """设置引擎的场景标签。

    场景标签是元数据，约束可选角色标签范围。
    保存时不修改角色标签——前端根据场景的可用角色自动过滤角色标签 checkbox。
    """
    tags = body.scene_tags or ["default"]
    # 去重保序
    seen = set()
    deduped = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM agent_runtimes WHERE id=?", (runtime_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
        c.execute("UPDATE agent_runtimes SET scene_tags=? WHERE id=?", (json.dumps(deduped, ensure_ascii=False), runtime_id))
        db.commit()
    log.info(f"Runtime scene_tags set: rid={runtime_id} scene_tags={deduped}")
    return {"scene_tags": deduped}


# ===== 引擎别名 =====

class AliasBody(BaseModel):
    alias: str = ""


@router.put("/runtimes/{runtime_id}/alias", operation_id="runtime_alias_set", summary="Set runtime alias", description="Sets an engine alias; an empty string clears it.")
def set_runtime_alias(runtime_id: int, body: AliasBody,
                       user: dict = Depends(require_no_session_token)):
    """设置引擎别名。空字符串表示清除别名。"""
    alias = (body.alias or "").strip()
    if len(alias) > 64:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "alias 最长 64 字符"})
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM agent_runtimes WHERE id=?", (runtime_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
        c.execute("UPDATE agent_runtimes SET alias=? WHERE id=?", (alias, runtime_id))
        db.commit()
    log.info(f"Runtime alias set: rid={runtime_id} alias={alias!r}")
    return {"alias": alias}


