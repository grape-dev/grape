"""Runtime API 路由。

M2：任务状态轮询 / 取消（用户 token）。
M3：/api/runtime/* 协议端点（register/heartbeat/claim/report），仅 rta_ 凭据可过（D2 凭据隔离）。
见 agent-runtime-event-full-design.md §6 / tech-impl.md §3。
"""
import hashlib, json, os
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from src.api.deps import get_current_user
from src.config import log
from src.api.time_utils import normalize_event_time
from src.session_manager import db, db_lock
from src.runtime import service

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


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


class RuntimeInfo(BaseModel):
    name: str
    version: str = ""
    healthy: bool = True


class HeartbeatBody(BaseModel):
    runtime_ids: list[int] = []


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


class FailBody(BaseModel):
    error: str = ""
    failure_reason: str = ""


# ===== 认证中间件 =====

def _require_credential(request: Request) -> int:
    return _auth_credential(request)


# ===== 协议端点（M3）=====

@router.post("/register")
async def runtime_register(body: RuntimeRegBody, request: Request, credential_id: int = Depends(_require_credential)):
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
        c.execute("SELECT scope, owner_name, owner_id, repo_key FROM runtime_credentials WHERE id=?", (credential_id,))
        cred_row = c.fetchone()
        cred_scope = (cred_row[0] if cred_row and cred_row[0] else "global").strip().lower()
        owner_id = (cred_row[2] or "") if cred_row else ""
        cred_repo_key = (cred_row[3] or "") if cred_row else ""
        if cred_scope == "user" and cred_row and cred_row[1]:
            # owner_id 与 user_accounts.id 对齐（按用户名），找不到保持空
            try:
                c.execute("SELECT id FROM user_accounts WHERE username=? LIMIT 1", (cred_row[1],))
                urow = c.fetchone()
                if urow:
                    owner_id = str(urow[0])
            except Exception:
                pass
        for rt in body.runtimes or []:
            name = (rt.get("name") or "").strip().lower()
            if not name:
                continue
            c.execute("SELECT id FROM agent_runtimes WHERE credential_id=? AND lower(name)=? AND scope=? AND owner_id=? LIMIT 1",
                      (credential_id, name, cred_scope, owner_id))
            row = c.fetchone()
            if row:
                rid = row[0]
                c.execute("UPDATE agent_runtimes SET version=?, healthy=?, installed=1, status='online', last_seen_at=?, cli_path=? WHERE id=?",
                          (rt.get("version", ""), 1 if rt.get("healthy", True) else 0,
                           datetime.now(timezone.utc).isoformat(), rt.get("cli_path", ""), rid))
            else:
                c.execute(
                    "INSERT INTO agent_runtimes (credential_id, name, display_name, `binary`, version, healthy, installed, status, last_seen_at, cli_path, scope, owner_id, repo_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (credential_id, name, rt.get("display_name", name), name, rt.get("version", ""),
                     1 if rt.get("healthy", True) else 0, 1, "online",
                     datetime.now(timezone.utc).isoformat(), rt.get("cli_path", ""), cred_scope, owner_id, cred_repo_key))
                rid = c.lastrowid
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
async def runtime_heartbeat(body: HeartbeatBody, request: Request, credential_id: int = Depends(_require_credential)):
    """更新 runtime 在线态 / last_seen_at。

    归属校验：只允许更新当前凭据名下的 runtime（credential_id 匹配），
    防止已吊销设备的旧 runtime id 被其它 active 凭据的心跳刷回 online
    （revoke 只置 offline，不删行；无校验则吊销的设备行会被残留 agent 复活）。

    响应携带 cancelled_tasks 列表——worker 收到后立即杀引擎进程，
    实现任务取消实时感知（延迟 15 秒，覆盖 step 很长时 report_messages 410 不触发的场景）。
    """
    now = datetime.now(timezone.utc).isoformat()
    client_ip = _client_ip(request)
    cancelled_tasks = []
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE runtime_credentials SET last_seen_at=?, last_ip=? WHERE id=?", (now, client_ip, credential_id))
        if body.runtime_ids:
            ph = ",".join("?" for _ in body.runtime_ids)
            c.execute(f"UPDATE agent_runtimes SET status='online', last_seen_at=? WHERE id IN ({ph}) AND credential_id=?",
                      [now] + body.runtime_ids + [credential_id])
            # 查该 worker 的已取消任务——通知 worker 立即杀进程
            ph2 = ",".join("?" for _ in body.runtime_ids)
            rows = c.execute(
                f"SELECT id FROM agent_tasks WHERE runtime_id IN ({ph2}) AND status='cancelled'",
                body.runtime_ids
            ).fetchall()
            cancelled_tasks = [r[0] for r in rows]
        db.commit()
    return {"status": "ok", "cancelled_tasks": cancelled_tasks}


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
        if _skill_id:
            _sk = get_skill_by_id(_skill_id)
            if _sk:
                _okey = _sk.get("file_path") or ""
            else:
                # stale skill_id → 回退按名字解析
                _sid = resolve_skill_id(skill)
                _okey = get_skill_object_key(_sid) if _sid else ""
        else:
            _sid = resolve_skill_id(skill)
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
async def runtime_claim(runtime_id: int, body: RuntimeClaimBody | None = None,
                        credential_id: int = Depends(_require_credential)):
    """按 runtime_id 拉取队首任务或 null（确定路由 D3 + 串行槽 D9）。

    v3.0：能力声明（supports_obs_url/prefer_fallback）动态携带；
    任务附 skill_obs_url（claim 现签预签名 URL）——prefer_fallback
    worker 直接给 mode=server-fetch（跳过 URL 直连尝试）。
    """
    task = service.claim(runtime_id)
    if task:
        if body and body.prefer_fallback:
            task["mode"] = "server-fetch"   # 直连不稳 → worker 直接代拉
        else:
            _attach_skill_obs(task)
    return {"task": task}


@router.post("/tasks/{task_id}/skill-zip")
async def runtime_task_skill_zip(task_id: str, credential_id: int = Depends(_require_credential)):
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
        if _skill_id:
            _sk = get_skill_by_id(_skill_id)
            if _sk:
                _okey = _sk.get("file_path") or ""
            else:
                _sid = resolve_skill_id(skill)
                _okey = get_skill_object_key(_sid) if _sid else ""
        else:
            _sid = resolve_skill_id(skill)
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
async def runtime_task_start(task_id: str, credential_id: int = Depends(_require_credential)):
    t = service.get_task(task_id)
    if not t:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    service.update_task_status(task_id, "running")
    return {"status": "running"}


@router.post("/tasks/{task_id}/progress")
async def runtime_task_progress(task_id: str, body: ProgressBody, credential_id: int = Depends(_require_credential)):
    service.enqueue_async_report("progress", task_id, {"msg": body.summary, "step": body.step, "total": body.total})
    return {"status": "ok"}


@router.post("/tasks/{task_id}/messages")
async def runtime_task_messages(task_id: str, body: MessagesBody, credential_id: int = Depends(_require_credential)):
    # task 已终态（failed/cancelled/completed）时拒绝消息，返回 410 Gone。
    # worker 端 _execute_task 检测到 410 后中断执行（用户终止后残留进程不再继续产出）。
    task = service.get_task(task_id)
    if task and task.get("status") in ("failed", "cancelled", "completed"):
        raise HTTPException(status_code=410, detail={"code": 410, "message": f"task already {task.get('status')}"})
    service.enqueue_async_report("messages", task_id, body.messages)
    return {"status": "ok"}


@router.post("/tasks/{task_id}/usage")
async def runtime_task_usage(task_id: str, body: UsageBody, credential_id: int = Depends(_require_credential)):
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
async def runtime_task_complete(task_id: str, body: CompleteBody, credential_id: int = Depends(_require_credential)):
    """完成（幂等）：解析结构化 output → finalize_event_archive → update_event_complete。"""
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    service.complete_task(task_id, body.result_text, body.exit_code,
                          usage={"session_id": body.session_id, "work_dir": body.work_dir})
    return {"status": "completed"}


@router.post("/tasks/{task_id}/fail")
async def runtime_task_fail(task_id: str, body: FailBody, credential_id: int = Depends(_require_credential)):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    service.fail_task(task_id, body.error or body.failure_reason or "unknown error", exit_code=1)
    return {"status": "failed"}


@router.post("/tasks/{task_id}/requeue")
async def runtime_task_requeue(task_id: str, credential_id: int = Depends(_require_credential)):
    """客户端并发冲突跳过时回滚任务为 queued（避免 dispatched 悬空卡死，见 seq#622 教训）。"""
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    ok = service.requeue_task(task_id)
    return {"status": "requeued", "recycled": ok}


@router.post("/runtimes/{runtime_id}/recover-orphans")
async def runtime_recover_orphans(runtime_id: int, credential_id: int = Depends(_require_credential)):
    """启动时清理该 runtime 遗留的 dispatched/running 任务（置回 queued）。"""
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET status='queued', started_at=NULL WHERE runtime_id=? AND status IN ('dispatched','running')",
                  (runtime_id,))
        db.commit()
    return {"message": "orphans recovered"}


# ===== 用户态端点（M2，保留）=====

@router.get("/tasks/{task_id}/status")
async def get_task_status(task_id: str, user: dict = Depends(get_current_user)):
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
async def runtime_task_state(task_id: str, credential_id: int = Depends(_require_credential)):
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


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, user: dict = Depends(get_current_user)):
    """取消任务（M2-5）：置 cancelled；执行侧由 worker 感知退出。"""
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    service.cancel_task(task_id)
    return {"message": f"任务 {task_id} 已取消", "status": "cancelled"}


@router.get("/runtimes/{runtime_id}/tasks")
async def list_runtime_tasks(runtime_id: int, user: dict = Depends(get_current_user),
                             status: str = "", limit: int = 50):
    """设备任务列表（M4 前端设备 [查看任务]）。"""
    from src.session_manager import db, db_lock
    clauses = ["runtime_id=?"]
    params = [runtime_id]
    if status:
        clauses.append("status=?")
        params.append(status)
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


@router.get("/credentials/{cid}/tasks")
async def list_credential_tasks(cid: int, user: dict = Depends(get_current_user),
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
async def runtime_install_script():
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


@router.get("/install/latest")
async def runtime_install_latest(credential_id: int = Depends(_require_credential)):
    """返回当前 Worker 安装包版本号（rta_ 鉴权）。

    供 worker 启动自检（见 worker-server-delivery-design.md §2.4）。
    """
    from src.worker_package import package_version
    return {"version": package_version()}


@router.get("/grape-worker.tar.gz")
async def runtime_worker_package_public():
    """公开下发 Worker 绿色安装包 tar.gz（无需鉴权）。

    包内仅含 worker 运行代码（无任何敏感信息，凭据由安装命令 --token 注入），
    供「凭据申请页一键安装命令」直接 curl 下载：
      curl -fsSL <server>/api/runtime/grape-worker.tar.gz -o grape-worker.tar.gz
    首次请求动态打包 + 缓存（代码变更自动重建，见 src/worker_package.py）。
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


@router.get("/install/package")
async def runtime_install_package(credential_id: int = Depends(_require_credential)):
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
