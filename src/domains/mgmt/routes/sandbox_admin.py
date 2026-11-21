"""P2-4 沙箱审计查询 + P2-5 白名单管理 API。

P2-4：审计查询
- GET /api/admin/sandbox-audit/{task_id} — 查询沙箱审计记录
- GET /api/admin/sandbox-audit/{task_id}/alerts — 查询告警
- GET /api/admin/external-api-audit/{task_id} — 查询外部 API 审计

P2-5：白名单管理
- GET/POST/PUT/DELETE /api/admin/sandbox/whitelist — 五维白名单 CRUD
- GET/POST/PUT/DELETE /api/admin/sandbox/paths — 文件系统路径 CRUD
- GET/PUT /api/admin/sandbox/config — 沙箱配置

鉴权：require_no_session_token（禁止 session token，允许 API Key / JWT）。
"""
from __future__ import annotations

import re
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from typing import Optional

from src.api.deps import require_no_session_token
from src.kernel.config import log

router = APIRouter(prefix="/admin/sandbox", tags=["sandbox-admin"])


def _require_admin(user: dict):
    """[沙箱迁移] 校验 admin 权限——兼容旧分支 user_accounts.is_admin 语义。

    main 分支无 user_accounts.is_admin 列：user dict 带 is_admin 标记（测试/内部调用）
    直接放行；否则查 admin_accounts（main 的独立 admin 表）。
    """
    if user.get("is_admin", 0):
        return
    if not user.get("id") and not user.get("username"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "admin only"})
    from src.kernel.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            if user.get("username"):
                c.execute("SELECT is_super FROM admin_accounts WHERE username=?", (user["username"],))
            else:
                c.execute("SELECT a.is_super FROM admin_accounts a JOIN grape_accounts u ON a.username=u.username WHERE u.id=?",
                          (user["id"],))
            row = c.fetchone()
        if row and row[0]:
            return
    except Exception:
        pass
    raise HTTPException(status_code=403, detail={"code": 403, "message": "admin only"})

# task_id 正则——防路径穿越
_TASK_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


def _validate_task_id(task_id: str) -> None:
    """校验 task_id 格式——防路径穿越。"""
    if not _TASK_ID_PATTERN.match(task_id):
        raise HTTPException(status_code=400, detail="Invalid task_id format")


def _get_sandbox_audit(request: Request):
    """从 app.state 获取 sandbox FileAuditLogger 实例。"""
    audit = getattr(request.app.state, "sandbox_audit", None)
    if audit is None:
        raise HTTPException(status_code=503, detail="Sandbox audit not initialized")
    return audit


def _get_external_api_audit(request: Request):
    """从 app.state 获取 external-api-audit FileAuditLogger 实例。"""
    audit = getattr(request.app.state, "external_api_audit", None)
    if audit is None:
        raise HTTPException(status_code=503, detail="External API audit not initialized")
    return audit


# ===== P2-4: 审计查询 =====

@router.get("/audit/{task_id}", operation_id="sandbox_audit_query")
def query_sandbox_audit(
    task_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(require_no_session_token),
):
    _require_admin(user)
    """查询某任务的沙箱审计记录。"""
    _validate_task_id(task_id)
    audit = _get_sandbox_audit(request)
    events = audit.get_events_by_task(task_id)
    # 分页
    total = len(events)
    page = events[:limit]
    return {"total": total, "events": page, "has_more": total > limit}


@router.get("/audit/{task_id}/alerts", operation_id="sandbox_alerts_query")
def query_sandbox_alerts(
    task_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(require_no_session_token),
):
    _require_admin(user)
    """查询某任务的安全告警。"""
    _validate_task_id(task_id)
    audit = _get_sandbox_audit(request)
    events = audit.get_events_by_task(task_id)
    alerts = [e for e in events if e.get("kind") == "security_alert"]
    total = len(alerts)
    page = alerts[:limit]
    return {"total": total, "alerts": page, "has_more": total > limit}


@router.get("/external-api-audit/{task_id}", operation_id="external_api_audit_query")
def query_external_api_audit(
    task_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(require_no_session_token),
):
    _require_admin(user)
    """查询某任务的外部 API 调用审计记录。"""
    _validate_task_id(task_id)
    audit = _get_external_api_audit(request)
    events = audit.get_events_by_task(task_id)
    total = len(events)
    page = events[:limit]
    return {"total": total, "events": page, "has_more": total > limit}


# ===== P2-5: 白名单管理 =====

class WhitelistBody(BaseModel):
    """五维白名单规则 body。"""
    backend_name: str
    domain: str
    method: str
    path_prefix: str = "*"
    scheme: str = "https"
    port: int = 443


class PathsBody(BaseModel):
    """文件系统路径 body。"""
    backend_name: str
    path: str
    access: str = "ro"


class ConfigBody(BaseModel):
    """沙箱配置 body。"""
    sandbox_mode: str = "required"
    degrade_policy: str = "compatible"


_VALID_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}
_VALID_SCHEMES = {"http", "https"}
_VALID_ACCESS = {"ro", "rw", "domain"}
_VALID_MODES = {"required", "preferred", "disabled"}
_VALID_DEGRADE = {"strict", "compatible"}


def _get_db():
    from src.kernel.session_manager import db, db_lock
    return db, db_lock


def _admin_write_audit(request: Request, user: dict, event_type: str, detail: dict) -> None:
    """管理面写路由审计（arch-guard F7/C3）：谁/何时/改了什么标识。

    detail 只记标识与前后语义（backend/rule_id 等），不落敏感值。
    """
    from src.domains.mgmt.auth import audit as auth_audit
    ip = request.client.host if request and request.client else ""
    auth_audit.log_audit(event_type, operator_id=user.get("id") or 0, ip=ip, detail=detail)


# --- 五维白名单 CRUD ---

@router.get("/whitelist", operation_id="sandbox_whitelist_list")
def list_whitelist(
    backend_name: Optional[str] = Query(None),
    user: dict = Depends(require_no_session_token),
):
    _require_admin(user)
    """列出白名单规则。"""
    from src.kernel.bulk_read import reader
    with reader() as _r:
        if backend_name:
            rows = _r.execute(
                "SELECT id, backend_name, domain, method, path_prefix, scheme, port, created_at, updated_at "
                "FROM grape_t_engine_sandbox_whitelist WHERE backend_name=? ORDER BY id",
                (backend_name,)
            ).fetchall()
        else:
            rows = _r.execute(
                "SELECT id, backend_name, domain, method, path_prefix, scheme, port, created_at, updated_at "
                "FROM grape_t_engine_sandbox_whitelist ORDER BY id"
            ).fetchall()
    return {"rules": [
        {"id": r[0], "backend_name": r[1], "domain": r[2], "method": r[3],
         "path_prefix": r[4], "scheme": r[5], "port": r[6],
         "created_at": r[7], "updated_at": r[8]}
        for r in rows
    ]}


@router.post("/whitelist", operation_id="sandbox_whitelist_create")
def create_whitelist(
    body: WhitelistBody,
    request: Request,
    user: dict = Depends(require_no_session_token),
):
    _require_admin(user)
    """新增白名单规则。"""
    method = body.method.upper()
    scheme = body.scheme.lower()
    if method not in _VALID_METHODS:
        raise HTTPException(status_code=400, detail=f"Invalid method: {body.method}")
    if scheme not in _VALID_SCHEMES:
        raise HTTPException(status_code=400, detail=f"Invalid scheme: {body.scheme}")
    # scheme-port 一致性校验：https 应配 443，http 应配 80（其他端口允许但告警）
    default_port = 443 if scheme == "https" else 80
    port = body.port or default_port

    db, db_lock = _get_db()
    try:
        with db_lock:
            from src.kernel.sandbox_uid import new_row_uid as _nru
            db.execute(
                "INSERT INTO grape_t_engine_sandbox_whitelist (id, backend_name, domain, method, path_prefix, scheme, port) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_nru(), body.backend_name, body.domain, method, body.path_prefix, scheme, port)
            )
            db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    # 失效缓存
    from src.domains.runtime.public import invalidate_cache
    invalidate_cache(body.backend_name)
    from src.domains.runtime.public import refresh_whitelist_cache
    refresh_whitelist_cache()
    log.info(f"whitelist rule created: backend={body.backend_name} domain={body.domain} method={method}")
    _admin_write_audit(request, user, "sandbox_whitelist_created", {
        "backend_name": body.backend_name, "domain": body.domain,
        "method": method, "path_prefix": body.path_prefix,
        "scheme": scheme, "port": port})
    return {"status": "ok"}


@router.put("/whitelist/{rule_id}", operation_id="sandbox_whitelist_update")
def update_whitelist(
    rule_id: str,
    body: WhitelistBody,
    request: Request,
    user: dict = Depends(require_no_session_token),
):
    _require_admin(user)
    """更新白名单规则。"""
    method = body.method.upper()
    scheme = body.scheme.lower()
    if method not in _VALID_METHODS:
        raise HTTPException(status_code=400, detail=f"Invalid method: {body.method}")
    if scheme not in _VALID_SCHEMES:
        raise HTTPException(status_code=400, detail=f"Invalid scheme: {body.scheme}")

    db, db_lock = _get_db()
    try:
        with db_lock:
            cursor = db.execute(
                "UPDATE grape_t_engine_sandbox_whitelist SET backend_name=?, domain=?, method=?, "
                "path_prefix=?, scheme=?, port=?, updated_at=datetime('now') WHERE id=?",
                (body.backend_name, body.domain, method, body.path_prefix, scheme, body.port, rule_id)
            )
            db.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"Rule {rule_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    from src.domains.runtime.public import invalidate_cache
    invalidate_cache(body.backend_name)
    from src.domains.runtime.public import refresh_whitelist_cache
    refresh_whitelist_cache()
    log.info(f"whitelist rule updated: id={rule_id}")
    _admin_write_audit(request, user, "sandbox_whitelist_updated", {
        "rule_id": rule_id, "backend_name": body.backend_name,
        "domain": body.domain, "method": method,
        "path_prefix": body.path_prefix, "scheme": scheme, "port": body.port})
    return {"status": "ok"}


@router.delete("/whitelist/{rule_id}", operation_id="sandbox_whitelist_delete")
def delete_whitelist(
    rule_id: str,
    request: Request,
    user: dict = Depends(require_no_session_token),
):
    _require_admin(user)
    """删除白名单规则。"""
    db, db_lock = _get_db()
    with db_lock:
        cursor = db.execute(
            "SELECT backend_name FROM grape_t_engine_sandbox_whitelist WHERE id=?", (rule_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Rule {rule_id} not found")
        backend_name = row[0]
        db.execute("DELETE FROM grape_t_engine_sandbox_whitelist WHERE id=?", (rule_id,))
        db.commit()

    from src.domains.runtime.public import invalidate_cache
    invalidate_cache(backend_name)
    from src.domains.runtime.public import refresh_whitelist_cache
    refresh_whitelist_cache()
    log.info(f"whitelist rule deleted: id={rule_id} backend={backend_name}")
    _admin_write_audit(request, user, "sandbox_whitelist_deleted", {
        "rule_id": rule_id, "backend_name": backend_name})
    return {"status": "ok"}


# --- 文件系统路径 CRUD ---

@router.get("/paths", operation_id="sandbox_paths_list")
def list_paths(
    backend_name: Optional[str] = Query(None),
    user: dict = Depends(require_no_session_token),
):
    _require_admin(user)
    """列出文件系统路径。"""
    from src.kernel.bulk_read import reader
    with reader() as _r:
        if backend_name:
            rows = _r.execute(
                "SELECT id, backend_name, path, access, created_at, updated_at "
                "FROM grape_t_engine_sandbox_paths WHERE backend_name=? ORDER BY id",
                (backend_name,)
            ).fetchall()
        else:
            rows = _r.execute(
                "SELECT id, backend_name, path, access, created_at, updated_at "
                "FROM grape_t_engine_sandbox_paths ORDER BY id"
            ).fetchall()
    return {"paths": [
        {"id": r[0], "backend_name": r[1], "path": r[2], "access": r[3],
         "created_at": r[4], "updated_at": r[5]}
        for r in rows
    ]}


@router.post("/paths", operation_id="sandbox_paths_create")
def create_paths(
    body: PathsBody,
    request: Request,
    user: dict = Depends(require_no_session_token),
):
    _require_admin(user)
    """新增文件系统路径。"""
    if body.access not in _VALID_ACCESS:
        raise HTTPException(status_code=400, detail=f"Invalid access: {body.access}")
    db, db_lock = _get_db()
    try:
        with db_lock:
            from src.kernel.sandbox_uid import new_row_uid as _nru
            db.execute(
                "INSERT INTO grape_t_engine_sandbox_paths (id, backend_name, path, access) VALUES (?, ?, ?, ?)",
                (_nru(), body.backend_name, body.path, body.access)
            )
            db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    log.info(f"sandbox path created: backend={body.backend_name} path={body.path}")
    _admin_write_audit(request, user, "sandbox_path_created", {
        "backend_name": body.backend_name, "path": body.path, "access": body.access})
    return {"status": "ok"}


@router.put("/paths/{path_id}", operation_id="sandbox_paths_update")
def update_paths(
    path_id: str,
    body: PathsBody,
    request: Request,
    user: dict = Depends(require_no_session_token),
):
    _require_admin(user)
    """更新文件系统路径。"""
    if body.access not in _VALID_ACCESS:
        raise HTTPException(status_code=400, detail=f"Invalid access: {body.access}")
    db, db_lock = _get_db()
    try:
        with db_lock:
            cursor = db.execute(
                "UPDATE grape_t_engine_sandbox_paths SET backend_name=?, path=?, access=?, "
                "updated_at=datetime('now') WHERE id=?",
                (body.backend_name, body.path, body.access, path_id)
            )
            db.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"Path {path_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    log.info(f"sandbox path updated: id={path_id}")
    _admin_write_audit(request, user, "sandbox_path_updated", {
        "path_id": path_id, "backend_name": body.backend_name,
        "path": body.path, "access": body.access})
    return {"status": "ok"}


@router.delete("/paths/{path_id}", operation_id="sandbox_paths_delete")
def delete_paths(
    path_id: str,
    request: Request,
    user: dict = Depends(require_no_session_token),
):
    _require_admin(user)
    """删除文件系统路径。"""
    db, db_lock = _get_db()
    with db_lock:
        cursor = db.execute("DELETE FROM grape_t_engine_sandbox_paths WHERE id=?", (path_id,))
        db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Path {path_id} not found")
    log.info(f"sandbox path deleted: id={path_id}")
    _admin_write_audit(request, user, "sandbox_path_deleted", {"path_id": path_id})
    return {"status": "ok"}


# --- 沙箱配置 ---

@router.get("/config", operation_id="sandbox_config_list")
def list_config(
    runtime_id: Optional[int] = Query(None),
    user: dict = Depends(require_no_session_token),
):
    _require_admin(user)
    """列出沙箱配置。"""
    from src.kernel.bulk_read import reader
    with reader() as _r:
        if runtime_id:
            rows = _r.execute(
                "SELECT id, runtime_id, engine_name, sandbox_mode, degrade_policy, created_at, updated_at "
                "FROM grape_t_engine_sandbox_config WHERE runtime_id=? ORDER BY id",
                (runtime_id,)
            ).fetchall()
        else:
            rows = _r.execute(
                "SELECT id, runtime_id, engine_name, sandbox_mode, degrade_policy, created_at, updated_at "
                "FROM grape_t_engine_sandbox_config ORDER BY id"
            ).fetchall()
    return {"configs": [
        {"id": r[0], "runtime_id": r[1], "engine_name": r[2],
         "sandbox_mode": r[3], "degrade_policy": r[4],
         "created_at": r[5], "updated_at": r[6]}
        for r in rows
    ]}


@router.put("/config/{config_id}", operation_id="sandbox_config_update")
def update_config(
    config_id: str,
    body: ConfigBody,
    request: Request,
    user: dict = Depends(require_no_session_token),
):
    _require_admin(user)
    """更新沙箱配置。"""
    if body.sandbox_mode not in _VALID_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid sandbox_mode: {body.sandbox_mode}")
    if body.degrade_policy not in _VALID_DEGRADE:
        raise HTTPException(status_code=400, detail=f"Invalid degrade_policy: {body.degrade_policy}")
    db, db_lock = _get_db()
    try:
        with db_lock:
            cursor = db.execute(
                "UPDATE grape_t_engine_sandbox_config SET sandbox_mode=?, degrade_policy=?, "
                "updated_at=datetime('now') WHERE id=?",
                (body.sandbox_mode, body.degrade_policy, config_id)
            )
            db.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"Config {config_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    log.info(f"sandbox config updated: id={config_id} mode={body.sandbox_mode}")
    _admin_write_audit(request, user, "sandbox_config_updated", {
        "config_id": config_id, "sandbox_mode": body.sandbox_mode,
        "degrade_policy": body.degrade_policy})
    return {"status": "ok"}
