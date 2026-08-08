"""Runtime API 路由。

M2：任务状态轮询 / 取消（用户 token）。
M3：/api/runtime/* 协议端点（register/heartbeat/claim/report），仅 rta_ 凭据可过（D2 凭据隔离）。
见 agent-runtime-event-full-design.md §6 / tech-impl.md §3。
"""
import hashlib, json
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from src.api.deps import get_current_user
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
async def runtime_register(body: RuntimeRegBody, credential_id: int = Depends(_require_credential)):
    """凭据校验 + upsert agent_runtimes（幂等），返回 server 权威 runtime IDs。"""
    registered = []
    with db_lock:
        c = db.cursor()
        # 回填 device_name
        c.execute("UPDATE runtime_credentials SET device_name=?, last_seen_at=? WHERE id=?",
                  (body.device_name, datetime.now(timezone.utc).isoformat(), credential_id))
        for rt in body.runtimes or []:
            name = (rt.get("name") or "").strip().lower()
            if not name:
                continue
            c.execute("SELECT id FROM agent_runtimes WHERE lower(name)=? AND scope='global' AND owner_id='' LIMIT 1", (name,))
            row = c.fetchone()
            if row:
                rid = row[0]
                c.execute("UPDATE agent_runtimes SET credential_id=?, version=?, healthy=?, installed=1, status='online', last_seen_at=?, cli_path=? WHERE id=?",
                          (credential_id, rt.get("version", ""), 1 if rt.get("healthy", True) else 0,
                           datetime.now(timezone.utc).isoformat(), rt.get("cli_path", ""), rid))
            else:
                c.execute(
                    "INSERT INTO agent_runtimes (credential_id, name, display_name, binary, version, healthy, installed, status, last_seen_at, cli_path, scope, owner_id) VALUES (?,?,?,?,?,?,?,?,?,?,'global','')",
                    (credential_id, name, rt.get("display_name", name), name, rt.get("version", ""),
                     1 if rt.get("healthy", True) else 0, 1, "online",
                     datetime.now(timezone.utc).isoformat(), rt.get("cli_path", "")))
                rid = c.lastrowid
            registered.append({"id": rid, "name": name})
        db.commit()
    return {"runtimes": registered, "server_url": _server_url()}


@router.post("/heartbeat")
async def runtime_heartbeat(body: HeartbeatBody, credential_id: int = Depends(_require_credential)):
    """更新 runtime 在线态 / last_seen_at。"""
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE runtime_credentials SET last_seen_at=? WHERE id=?", (now, credential_id))
        if body.runtime_ids:
            ph = ",".join("?" for _ in body.runtime_ids)
            c.execute(f"UPDATE agent_runtimes SET status='online', last_seen_at=? WHERE id IN ({ph})", [now] + body.runtime_ids)
        db.commit()
    return {"status": "ok"}


@router.post("/runtimes/{runtime_id}/tasks/claim")
async def runtime_claim(runtime_id: int, credential_id: int = Depends(_require_credential)):
    """按 runtime_id 拉取队首任务或 null（确定路由 D3 + 串行槽 D9）。"""
    task = service.claim(runtime_id)
    return {"task": task}


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
    service.enqueue_async_report("messages", task_id, body.messages)
    return {"status": "ok"}


@router.post("/tasks/{task_id}/usage")
async def runtime_task_usage(task_id: str, body: UsageBody, credential_id: int = Depends(_require_credential)):
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET usage_json=? WHERE id=?",
                  (json.dumps(body.model_dump()), task_id))
        db.commit()
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
        c.execute(f"SELECT id, repo_key, agent_type, skill, status, exit_code, result_text, created_at, started_at, completed_at FROM agent_tasks WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
                  params + [limit])
        return {"tasks": [
            {"id": r[0], "repo_key": r[1], "agent_type": r[2], "skill": r[3],
             "status": r[4], "exit_code": r[5], "result_text": (r[6] or "")[:200],
             "created_at": r[7], "started_at": r[8], "completed_at": r[9]}
            for r in c.fetchall()
        ]}


def _server_url() -> str:
    try:
        from src.user.db import get_meta_value
        return get_meta_value("webhook_url") or ""
    except Exception:
        return ""
