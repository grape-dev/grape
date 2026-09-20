"""Autopilot 路由：自动化任务 CRUD + 执行日志查询 + 手动触发（Issue #150）

Phase 1 全栈能力：
- §5.3.1 Autopilot 定时引擎：任务 CRUD + cron 调度
- §5.3.2 执行日志基础：执行记录查询
- §5.3.3 失败重试：手动触发 + 重试记录
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from src.api.deps import get_current_user
from src.config import log
from src.session_manager import db, db_lock

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])


def _validate_cron(cron_expr: str) -> bool:
    """校验 cron 表达式格式（5 段制）。"""
    try:
        from croniter import croniter
        croniter(cron_expr, datetime.now(timezone.utc))
        return True
    except Exception:
        return False


def _validate_agent_chain(chain_str: str) -> list:
    """校验 agent_chain JSON 格式，返回解析后的列表。"""
    try:
        chain = json.loads(chain_str) if isinstance(chain_str, str) else chain_str
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "agent_chain 不是有效的 JSON"})
    if not isinstance(chain, list) or len(chain) == 0:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "agent_chain 必须是非空数组"})
    for step in chain:
        if not isinstance(step, dict):
            raise HTTPException(status_code=400, detail={"code": 400, "message": "agent_chain 每个元素必须是对象"})
        if not step.get("agent"):
            raise HTTPException(status_code=400, detail={"code": 400, "message": "agent_chain 每个元素必须包含 agent 字段"})
    return chain


def _compute_next_run(cron_expr: str) -> str:
    """计算 cron 下次触发时间（UTC ISO）。"""
    try:
        from croniter import croniter
        cron = croniter(cron_expr, datetime.now(timezone.utc))
        nxt = cron.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        return nxt.isoformat()
    except Exception:
        return ""


def _row_to_task(row) -> dict:
    """将 DB 行转为 API 响应 dict。"""
    return {
        "id": row[0],
        "name": row[1],
        "repo_key": row[2],
        "scene_id": row[3],
        "description": row[4],
        "cron": row[5],
        "agent_chain": json.loads(row[6]) if row[6] else [],
        "issue_number": str(row[7]) if row[7] else "",
        "max_retry": row[8],
        "enabled": bool(row[9]),
        "last_run_at": row[10] or "",
        "next_run_at": row[11] or "",
        "created_at": row[12] or "",
        "updated_at": row[13] or "",
    }


# ====== 请求模型 ======

class CreateTaskBody(BaseModel):
    name: str
    repo_key: str = ""
    scene_id: str = "default"
    description: str = ""
    cron: str = "*/30 * * * *"
    max_retry: int = 3
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("name 不能为空")
        return v.strip()

    @field_validator("max_retry")
    @classmethod
    def max_retry_range(cls, v):
        if v < 0 or v > 10:
            raise ValueError("max_retry 必须在 0-10 之间")
        return v

    @field_validator("cron")
    @classmethod
    def cron_valid(cls, v):
        if not _validate_cron(v):
            raise ValueError(f"无效的 cron 表达式: {v}")
        return v


class UpdateTaskBody(BaseModel):
    name: Optional[str] = None
    repo_key: Optional[str] = None
    scene_id: Optional[str] = None
    description: Optional[str] = None
    cron: Optional[str] = None
    max_retry: Optional[int] = None
    enabled: Optional[bool] = None

    @field_validator("max_retry")
    @classmethod
    def max_retry_range(cls, v):
        if v is not None and (v < 0 or v > 10):
            raise ValueError("max_retry 必须在 0-10 之间")
        return v

    @field_validator("cron")
    @classmethod
    def cron_valid(cls, v):
        if v is not None and not _validate_cron(v):
            raise ValueError(f"无效的 cron 表达式: {v}")
        return v


# ====== 任务 CRUD ======

@router.get("/tasks")
def list_tasks(
    user: dict = Depends(get_current_user),
    repo_key: str = Query("", description="按仓库筛选"),
    enabled: Optional[int] = Query(None, description="按启用状态筛选 (0/1)"),
):
    """列出所有自动化任务（可按 repo_key / enabled 筛选）。"""
    sql = ("SELECT id, name, repo_key, scene_id, description, cron, agent_chain, issue_number, max_retry, "
           "enabled, last_run_at, next_run_at, created_at, updated_at FROM autopilot_tasks")
    conditions = []
    params = []
    if repo_key:
        conditions.append("repo_key=?")
        params.append(repo_key)
    if enabled is not None:
        conditions.append("enabled=?")
        params.append(enabled)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY id DESC"
    with db_lock:
        c = db.cursor()
        rows = c.execute(sql, params).fetchall()
    return {"tasks": [_row_to_task(r) for r in rows]}


@router.get("/tasks/{task_id}")
def get_task(task_id: int, user: dict = Depends(get_current_user)):
    """获取单个自动化任务详情。"""
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id, name, repo_key, scene_id, description, cron, agent_chain, issue_number, max_retry, "
            "enabled, last_run_at, next_run_at, created_at, updated_at "
            "FROM autopilot_tasks WHERE id=?", (task_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    return _row_to_task(row)


@router.post("/tasks")
def create_task(body: CreateTaskBody, user: dict = Depends(get_current_user)):
    """创建自动化任务。"""
    next_run = _compute_next_run(body.cron)
    now_str = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO autopilot_tasks "
            "(name, repo_key, scene_id, description, cron, agent_chain, issue_number, max_retry, enabled, next_run_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (body.name, body.repo_key, body.scene_id, body.description, body.cron,
             '[]', '', body.max_retry, 1 if body.enabled else 0, next_run, now_str, now_str)
        )
        db.commit()
        task_id = c.lastrowid
    log.info(f"Autopilot: task created id={task_id} name='{body.name}' by user={user.get('username', '')}")
    return {"id": task_id, "message": "task created", "next_run_at": next_run}


@router.put("/tasks/{task_id}")
def update_task(task_id: int, body: UpdateTaskBody, user: dict = Depends(get_current_user)):
    """更新自动化任务（部分更新）。"""
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT id FROM autopilot_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    fields = []
    params = []
    updates = body.model_dump(exclude_none=True)
    for key, val in updates.items():
        if key == "enabled":
            fields.append("enabled=?")
            params.append(1 if val else 0)
        elif key == "cron":
            fields.append("cron=?")
            params.append(val)
            next_run = _compute_next_run(val)
            fields.append("next_run_at=?")
            params.append(next_run)
        else:
            fields.append(f"{key}=?")
            params.append(val)
    if not fields:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "no fields to update"})
    fields.append("updated_at=?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(task_id)
    with db_lock:
        c = db.cursor()
        c.execute(f"UPDATE autopilot_tasks SET {', '.join(fields)} WHERE id=?", params)
        db.commit()
    log.info(f"Autopilot: task {task_id} updated by user={user.get('username', '')}")
    return {"message": "task updated"}


@router.delete("/tasks/{task_id}")
def delete_task(task_id: int, user: dict = Depends(get_current_user)):
    """删除自动化任务（同时删除关联执行记录）。"""
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT id FROM autopilot_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
        c.execute("DELETE FROM autopilot_executions WHERE task_id=?", (task_id,))
        c.execute("DELETE FROM autopilot_tasks WHERE id=?", (task_id,))
        db.commit()
    log.info(f"Autopilot: task {task_id} deleted by user={user.get('username', '')}")
    return {"message": "task deleted"}


# ====== 手动触发 ======

@router.post("/tasks/{task_id}/trigger")
async def trigger_task(task_id: int, user: dict = Depends(get_current_user)):
    """手动触发自动化任务执行。"""
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT id FROM autopilot_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    from src.runtime.scheduler import trigger_task_now
    result = await trigger_task_now(task_id)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail={"code": 409, "message": result["message"]})
    log.info(f"Autopilot: task {task_id} manually triggered by user={user.get('username', '')}")
    return {"message": result["message"]}


# ====== 执行日志查询 ======

@router.get("/executions")
def list_executions(
    user: dict = Depends(get_current_user),
    task_id: Optional[int] = Query(None, description="按任务 ID 筛选"),
    status: Optional[str] = Query(None, description="按状态筛选"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """查询执行日志（可按 task_id / status 筛选，分页）。"""
    sql = ("SELECT id, task_id, status, retry_count, agent_chain_step, result, error, "
           "a2a_seq, started_at, completed_at, created_at, issue_number FROM autopilot_executions WHERE 1=1")
    conditions = []
    params = []
    if task_id is not None:
        conditions.append("task_id=?")
        params.append(task_id)
    if status:
        conditions.append("status=?")
        params.append(status)
    if conditions:
        sql += " AND " + " AND ".join(conditions)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with db_lock:
        c = db.cursor()
        rows = c.execute(sql, params).fetchall()
        count_sql = "SELECT COUNT(*) FROM autopilot_executions WHERE 1=1"
        if conditions:
            count_sql += " AND " + " AND ".join(conditions)
        total = c.execute(count_sql, params[:-2]).fetchone()[0]
    executions = []
    for r in rows:
        executions.append({
            "id": r[0],
            "task_id": r[1],
            "status": r[2],
            "retry_count": r[3],
            "agent_chain_step": r[4],
            "result": r[5] or "",
            "error": r[6] or "",
            "a2a_seq": r[7] or 0,
            "started_at": r[8] or "",
            "completed_at": r[9] or "",
            "created_at": r[10] or "",
            "issue_number": r[11] or "",
        })
    return {"executions": executions, "total": total, "limit": limit, "offset": offset}


# ====== 场景列表 ======

@router.get("/scenes")
def list_scenes(repo_key: str = Query("", description="仓库标识"), user: dict = Depends(get_current_user)):
    """返回仓库可用场景列表。"""
    from src.session_manager import db, db_lock
    scenes = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT DISTINCT scene_id, name FROM devops_pipeline_scene "
                "WHERE repo_key=? OR repo_key='' ORDER BY scene_id",
                (repo_key,)
            )
            for r in c.fetchall():
                scenes.append({"scene_id": r[0], "name": r[1]})
    except Exception:
        pass
    if not scenes:
        scenes = [{"scene_id": "default", "name": "通用开发"}]
    return {"scenes": scenes}
