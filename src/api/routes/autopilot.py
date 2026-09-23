"""Autopilot 路由 v3.0：定时任务 CRUD + 执行日志 + findings 查询 + 问题派发

v3.0 重构：
- 不再创建 Issue 触发流水线，改为直接执行单个 agent
- 字段调整: scene_id/description → agent_type/instruction/skill_name
- 所有接口加 check_repo_access 仓库归属校验
- 新增 GET /executions/{id}/findings 返回问题列表
- 新增 POST /executions/{id}/dispatch 派发问题为真实 Issue
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from src.api.deps import get_current_user, check_repo_access
from src.config import log
from src.session_manager import db, db_lock

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])

_VALID_AGENT_TYPES = {"issue", "qa", "sre"}


def _validate_cron(cron_expr: str) -> bool:
    """校验 cron 表达式格式（5 段制）。"""
    try:
        from croniter import croniter
        croniter(cron_expr, datetime.now(timezone.utc))
        return True
    except Exception:
        return False


def _compute_next_run(cron_expr: str) -> str:
    """计算 cron 下次触发时间（本地时区，ISO 格式）。"""
    try:
        from croniter import croniter
        now_local = datetime.now().astimezone()
        cron = croniter(cron_expr, now_local)
        nxt = cron.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.astimezone()
        return nxt.isoformat()
    except Exception:
        return ""


def _check_task_access(user: dict, task_id: int) -> dict:
    """校验 task 归属并返回 task dict（含 repo_key）。"""
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id, repo_key FROM autopilot_tasks WHERE id=?", (task_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    task_repo_key = row[1] or ""
    if task_repo_key:
        check_repo_access(user, task_repo_key)
    return {"id": row[0], "repo_key": task_repo_key}


def _check_exec_access(user: dict, exec_id: int) -> dict:
    """校验 execution 归属并返回 dict（含 task_id, repo_key）。"""
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT e.id, e.task_id, t.repo_key FROM autopilot_executions e "
            "JOIN autopilot_tasks t ON e.task_id=t.id WHERE e.id=?",
            (exec_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "execution not found"})
    repo_key = row[2] or ""
    if repo_key:
        check_repo_access(user, repo_key)
    return {"id": row[0], "task_id": row[1], "repo_key": repo_key}


def _row_to_task(row) -> dict:
    """将 DB 行转为 API 响应 dict。"""
    return {
        "id": row[0],
        "name": row[1],
        "repo_key": row[2],
        "agent_type": row[3],
        "instruction": row[4],
        "skill_name": row[5],
        "cron": row[6],
        "max_retry": row[7],
        "enabled": bool(row[8]),
        "last_run_at": row[9] or "",
        "next_run_at": row[10] or "",
        "created_at": row[11] or "",
        "updated_at": row[12] or "",
        "created_by": row[13] if len(row) > 13 else 0,
    }


# ====== 请求模型 ======

class CreateTaskBody(BaseModel):
    name: str
    repo_key: str = ""
    agent_type: str = "issue"
    instruction: str = ""
    skill_name: str = ""
    cron: str = "0 9 * * *"
    max_retry: int = 3
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("name 不能为空")
        return v.strip()

    @field_validator("agent_type")
    @classmethod
    def agent_type_valid(cls, v):
        if v not in _VALID_AGENT_TYPES:
            raise ValueError(f"agent_type 必须是 {', '.join(_VALID_AGENT_TYPES)} 之一")
        return v

    @field_validator("instruction")
    @classmethod
    def instruction_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("巡检指令不能为空")
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
    agent_type: Optional[str] = None
    instruction: Optional[str] = None
    skill_name: Optional[str] = None
    cron: Optional[str] = None
    max_retry: Optional[int] = None
    enabled: Optional[bool] = None

    @field_validator("agent_type")
    @classmethod
    def agent_type_valid(cls, v):
        if v is not None and v not in _VALID_AGENT_TYPES:
            raise ValueError(f"agent_type 必须是 {', '.join(_VALID_AGENT_TYPES)} 之一")
        return v

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
    """列出自动化任务（按 repo_key 筛选，校验仓库归属）。"""
    # 如果指定了 repo_key，校验归属
    if repo_key:
        check_repo_access(user, repo_key)
    sql = ("SELECT id, name, repo_key, agent_type, instruction, skill_name, "
           "cron, max_retry, enabled, last_run_at, next_run_at, created_at, updated_at, created_by "
           "FROM autopilot_tasks")
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
    # 过滤：只返回当前用户有权访问的仓库的任务
    from src.user.db import get_managed_repos
    user_repos = {r["repo_key"] for r in get_managed_repos(user.get("id", 0))}
    tasks = [_row_to_task(r) for r in rows if r[2] in user_repos or not r[2]]
    return {"tasks": tasks}


@router.get("/tasks/{task_id}")
def get_task(task_id: int, user: dict = Depends(get_current_user)):
    """获取单个自动化任务详情。"""
    _check_task_access(user, task_id)
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id, name, repo_key, agent_type, instruction, skill_name, "
            "cron, max_retry, enabled, last_run_at, next_run_at, created_at, updated_at, created_by "
            "FROM autopilot_tasks WHERE id=?", (task_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    return _row_to_task(row)


@router.post("/tasks")
def create_task(body: CreateTaskBody, user: dict = Depends(get_current_user)):
    """创建自动化任务。"""
    if body.repo_key:
        check_repo_access(user, body.repo_key)
    next_run = _compute_next_run(body.cron)
    now_str = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO autopilot_tasks "
            "(name, repo_key, agent_type, instruction, skill_name, cron, max_retry, enabled, next_run_at, created_at, updated_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (body.name, body.repo_key, body.agent_type, body.instruction, body.skill_name,
             body.cron, body.max_retry, 1 if body.enabled else 0, next_run, now_str, now_str, user.get("id", 0) or 0)
        )
        db.commit()
        task_id = c.lastrowid
    log.info(f"Autopilot: task created id={task_id} name='{body.name}' agent={body.agent_type} by user={user.get('username', '')}")
    return {"id": task_id, "message": "task created", "next_run_at": next_run}


@router.put("/tasks/{task_id}")
def update_task(task_id: int, body: UpdateTaskBody, user: dict = Depends(get_current_user)):
    """更新自动化任务（部分更新）。"""
    _check_task_access(user, task_id)
    # 如果要改 repo_key，校验新仓库归属
    if body.repo_key:
        check_repo_access(user, body.repo_key)
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
    _check_task_access(user, task_id)
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_executions WHERE task_id=?", (task_id,))
        c.execute("DELETE FROM autopilot_tasks WHERE id=?", (task_id,))
        db.commit()
    log.info(f"Autopilot: task {task_id} deleted by user={user.get('username', '')}")
    return {"message": "task deleted"}


# ====== 手动触发 ======

@router.post("/tasks/{task_id}/trigger")
async def trigger_task(task_id: int, user: dict = Depends(get_current_user)):
    """手动触发自动化任务执行。"""
    _check_task_access(user, task_id)
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
    # 如果指定了 task_id，校验归属
    if task_id is not None:
        _check_task_access(user, task_id)
    sql = ("SELECT e.id, e.task_id, e.status, e.retry_count, e.result, e.error, "
           "e.started_at, e.completed_at, e.created_at, e.agent_type, e.findings_json "
           "FROM autopilot_executions e WHERE 1=1")
    count_sql = "SELECT COUNT(*) FROM autopilot_executions e WHERE 1=1"
    conditions = []
    params = []
    if task_id is not None:
        conditions.append("e.task_id=?")
        params.append(task_id)
    if status:
        conditions.append("e.status=?")
        params.append(status)
    if conditions:
        sql += " AND " + " AND ".join(conditions)
        count_sql += " AND " + " AND ".join(conditions)
    sql += " ORDER BY e.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with db_lock:
        c = db.cursor()
        rows = c.execute(sql, params).fetchall()
        total = c.execute(count_sql, params[:-2]).fetchone()[0]
    executions = []
    for r in rows:
        executions.append({
            "id": r[0],
            "task_id": r[1],
            "status": r[2],
            "retry_count": r[3],
            "result": r[4] or "",
            "error": r[5] or "",
            "started_at": r[6] or "",
            "completed_at": r[7] or "",
            "created_at": r[8] or "",
            "agent_type": r[9] or "",
            "has_findings": bool(r[10]),
        })
    return {"executions": executions, "total": total, "limit": limit, "offset": offset}


# ====== findings 查询 ======

@router.get("/executions/{exec_id}/findings")
def get_findings(exec_id: int, user: dict = Depends(get_current_user)):
    """获取执行结果的问题列表。"""
    _check_exec_access(user, exec_id)
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT findings_json, result FROM autopilot_executions WHERE id=?",
            (exec_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "execution not found"})
    findings_json = row[0] or ""
    if findings_json:
        try:
            data = json.loads(findings_json)
            return {"summary": data.get("summary", ""), "findings": data.get("findings", []),
                    "parse_error": data.get("parse_error", "")}
        except json.JSONDecodeError:
            return {"summary": "", "findings": [], "parse_error": "invalid json in findings_json"}
    return {"summary": "", "findings": [], "parse_error": "no findings"}


# ====== 问题派发 ======

class DispatchBody(BaseModel):
    finding_id: str


@router.post("/executions/{exec_id}/dispatch")
async def dispatch_finding(exec_id: int, body: DispatchBody, user: dict = Depends(get_current_user)):
    """将某个 finding 派发为真实 Issue，走完整流水线。"""
    exec_info = _check_exec_access(user, exec_id)
    repo_key = exec_info["repo_key"]

    # 读取 findings_json
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT findings_json FROM autopilot_executions WHERE id=?",
            (exec_id,)
        ).fetchone()
    if not row or not row[0]:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无 findings 可派发"})

    try:
        findings_data = json.loads(row[0])
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "findings_json 格式错误"})

    # 找到对应的 finding
    finding = None
    for f in findings_data.get("findings", []):
        if f.get("id") == body.finding_id:
            finding = f
            break
    if not finding:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"finding {body.finding_id} not found"})

    # 检查是否已派发
    if finding.get("dispatched_issue_number"):
        raise HTTPException(status_code=409, detail={"code": 409,
                                                      "message": f"已派发过 (Issue #{finding['dispatched_issue_number']})",
                                                      "issue_number": finding["dispatched_issue_number"]})

    # 创建真实 Issue
    from src.repo_key import split_owner_repo
    owner, rname = split_owner_repo(repo_key)

    # 获取 bot token
    repo_token = ""
    try:
        if repo_key.startswith("github:"):
            from src.user.db import get_github_token
            repo_token = get_github_token(repo_key=repo_key) or ""
        else:
            from src.user.db import get_gitcode_token
            repo_token = get_gitcode_token(repo_key=repo_key) or ""
    except Exception:
        pass

    # 调 create_task_via_issue
    from src.api.issues_center import TaskCreateBody, create_task_via_issue
    issue_body = TaskCreateBody(
        repo_key=repo_key,
        title=finding["title"],
        description=finding.get("description", ""),
    )
    internal_user = {"id": user.get("id", 0), "username": user.get("username", owner),
                     "token": repo_token, "auth_type": "internal"}
    result = await create_task_via_issue(issue_body, user=internal_user)
    issue_number = str(result.get("issue_number", ""))
    if issue_number and issue_number != "0":
        # 更新 findings_json
        finding["dispatched_issue_number"] = issue_number
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE autopilot_executions SET findings_json=? WHERE id=?",
                (json.dumps(findings_data, ensure_ascii=False), exec_id)
            )
            db.commit()
        log.info(f"Autopilot: finding {body.finding_id} dispatched as Issue #{issue_number} (exec={exec_id})")
        return {"ok": True, "issue_number": issue_number, "message": f"已创建 Issue #{issue_number}"}
    return {"ok": False, "message": f"创建 Issue 失败: {result}"}


# ====== agent 列表（供前端选择器使用）======

@router.get("/agents")
def list_agents(user: dict = Depends(get_current_user)):
    """返回可用的 agent 列表（issue/qa/sre）。"""
    return {"agents": [
        {"id": "issue", "name": "Issue Agent", "desc": "综合处理：看板巡检/stale清理/周报"},
        {"id": "qa", "name": "QA Agent", "desc": "漏洞检查/代码质量扫描/PR审查"},
        {"id": "sre", "name": "SRE Agent", "desc": "定时部署/健康检查/资源巡检"},
    ]}
