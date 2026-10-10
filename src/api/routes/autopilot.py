"""Autopilot 路由 v3.0：定时任务 CRUD + 执行日志 + findings 查询 + 问题派发

v3.0 重构：
- 不再创建 Issue 触发流水线，改为直接执行单个 agent
- 字段调整: scene_id/description → instruction/skill_name
- agent_type 不再由用户选择，内部固定 'issue'
- 所有接口加 check_repo_access 仓库归属校验
- 新增 GET /executions/{id}/findings 返回问题列表
- 新增 POST /executions/{id}/dispatch 派发问题为真实 Issue
- 新增 POST /polish 润色巡检指令
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

# agent_type 不再由用户选择，内部固定为 'issue'
_FIXED_AGENT_TYPE = "issue"


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
    import json as _json
    engine_ids = []
    if len(row) > 14 and row[14]:
        try:
            engine_ids = _json.loads(row[14])
        except Exception:
            engine_ids = []
    custom_fields = []
    if len(row) > 15 and row[15]:
        try:
            custom_fields = _json.loads(row[15])
        except Exception:
            custom_fields = []
    work_branch = row[16] if len(row) > 16 else ""
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
        "engine_runtime_ids": engine_ids,
        "custom_fields": custom_fields,
        "work_branch": work_branch or "",
    }


# ====== 请求模型 ======

class CreateTaskBody(BaseModel):
    name: str
    repo_key: str = ""
    instruction: str = ""
    skill_name: str = ""
    cron: str = "0 9 * * *"
    max_retry: int = 3
    enabled: bool = True
    engine_runtime_ids: list = []
    custom_fields: list = []
    work_branch: str = ""

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("name 不能为空")
        return v.strip()

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
    instruction: Optional[str] = None
    skill_name: Optional[str] = None
    cron: Optional[str] = None
    max_retry: Optional[int] = None
    engine_runtime_ids: Optional[list] = None
    enabled: Optional[bool] = None
    custom_fields: Optional[list] = None
    work_branch: Optional[str] = None

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
           "cron, max_retry, enabled, last_run_at, next_run_at, created_at, updated_at, created_by, engine_runtime_ids, custom_fields, work_branch "
           "FROM autopilot_tasks")
    conditions = []
    params = []
    # 跨平台前缀兼容（与 check_repo_access 对齐）：gitcode:↔github: 互换匹配
    if repo_key:
        base = repo_key.split(":", 1)[1] if ":" in repo_key else repo_key
        conditions.append("(repo_key=? OR repo_key=? OR repo_key=?)")
        params.extend([repo_key, f"gitcode:{base}", f"github:{base}"])
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
    # 跨平台前缀兼容（与 check_repo_access 对齐）：gitcode:↔github: 互换匹配
    from src.user.db import get_managed_repos
    user_repos = {r["repo_key"] for r in get_managed_repos(user.get("id", 0))}
    # 构建跨平台候选集：user_repos 中每个 repo_key 的 gitcode:/github: 变体都算匹配
    _repo_base = set()
    for rk in user_repos:
        base = rk.split(":", 1)[1] if ":" in rk else rk
        _repo_base.add(base)
    tasks = [_row_to_task(r) for r in rows if not r[2] or
             r[2] in user_repos or
             (r[2].split(":", 1)[1] if ":" in r[2] else r[2]) in _repo_base]
    # 引擎绑定真实性同步 §5.1b：绑定引擎已被系统 detach 的任务带提示（两表协同，不删绑定）
    try:
        from src.runtime.engine_select import get_detached_engine_hints
        import json as _json_hint
        _all_eids = set()
        for t in tasks:
            for _eid in (t.get("engine_runtime_ids") or []):
                _all_eids.add(int(_eid))
        if _all_eids:
            # 卡点④：按任务仓库分组取提示（引擎审计全局、任务提示只看本仓库场景）
            _hints_by_repo = {}
            _repos = {t.get("repo_key") or "" for t in tasks}
            for _rk in _repos:
                _hints_by_repo[_rk] = {h["runtime_id"]: h for h in get_detached_engine_hints(runtime_ids=list(_all_eids), repo_key=_rk)}
            for t in tasks:
                _hint_map = _hints_by_repo.get(t.get("repo_key") or "", {})
                _hints = [ _hint_map[int(_eid)] for _eid in (t.get("engine_runtime_ids") or [])
                           if int(_eid) in _hint_map ]
                if _hints:
                    t["engine_detached"] = {
                        "engines": [f"{h['runtime_name']}#{h['runtime_id']}" for h in _hints],
                        "detached_at": _hints[0]["detached_at"],
                    }
    except Exception as _he:
        log.warning(f"list_tasks: engine_detached hints failed: {_he}")
    return {"tasks": tasks}


@router.get("/tasks/{task_id}")
def get_task(task_id: int, user: dict = Depends(get_current_user)):
    """获取单个自动化任务详情。"""
    _check_task_access(user, task_id)
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id, name, repo_key, agent_type, instruction, skill_name, "
            "cron, max_retry, enabled, last_run_at, next_run_at, created_at, updated_at, created_by, engine_runtime_ids, custom_fields, work_branch "
            "FROM autopilot_tasks WHERE id=?", (task_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    task = _row_to_task(row)
    # 引擎绑定真实性同步 §5.1b：详情同样带 engine_detached 提示（与列表口径一致）
    try:
        from src.runtime.engine_select import get_detached_engine_hints
        _eids = [int(x) for x in (task.get("engine_runtime_ids") or [])]
        if _eids:
            _hints = get_detached_engine_hints(runtime_ids=_eids, repo_key=task.get("repo_key") or "")
            if _hints:
                task["engine_detached"] = {
                    "engines": [f"{h['runtime_name']}#{h['runtime_id']}" for h in _hints],
                    "detached_at": _hints[0]["detached_at"],
                }
    except Exception as _he:
        log.warning(f"get_task: engine_detached hints failed: {_he}")
    return task


@router.post("/tasks")
def create_task(body: CreateTaskBody, user: dict = Depends(get_current_user)):
    """创建自动化任务。"""
    # 规范化 repo_key：前端可能硬编码 gitcode: 前缀，需按用户实际关联仓修正
    from src.repo_key import to_repo_key, parse_repo_key
    if body.repo_key:
        # 检查用户 managed_repos 中是否有匹配的仓库（跨平台前缀兼容）
        from src.user.db import get_managed_repos
        user_repos = {r["repo_key"] for r in get_managed_repos(user.get("id", 0))}
        body_base = body.repo_key.split(":", 1)[1] if ":" in body.repo_key else body.repo_key
        _matched_repo_key = ""
        for rk in user_repos:
            rk_base = rk.split(":", 1)[1] if ":" in rk else rk
            if rk_base == body_base:
                _matched_repo_key = rk
                break
        if _matched_repo_key:
            # 用用户实际关联的 repo_key（正确的前缀），而非前端硬编码的
            body.repo_key = _matched_repo_key
        check_repo_access(user, body.repo_key)
    next_run = _compute_next_run(body.cron)
    now_str = datetime.now(timezone.utc).isoformat()
    engine_ids_json = json.dumps(body.engine_runtime_ids) if body.engine_runtime_ids else ""
    custom_fields_json = json.dumps(body.custom_fields, ensure_ascii=False) if body.custom_fields else ""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO autopilot_tasks "
            "(name, repo_key, agent_type, instruction, skill_name, cron, max_retry, enabled, next_run_at, created_at, updated_at, created_by, engine_runtime_ids, custom_fields, work_branch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (body.name, body.repo_key, _FIXED_AGENT_TYPE, body.instruction, body.skill_name,
             body.cron, body.max_retry, 1 if body.enabled else 0, next_run, now_str, now_str, user.get("id", 0) or 0,
             engine_ids_json, custom_fields_json, body.work_branch or "")
        )
        db.commit()
        task_id = c.lastrowid
    log.info(f"Autopilot: task created id={task_id} name='{body.name}' by user={user.get('username', '')}")
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
        elif key == "engine_runtime_ids":
            fields.append("engine_runtime_ids=?")
            params.append(json.dumps(val) if val else "")
        elif key == "custom_fields":
            fields.append("custom_fields=?")
            params.append(json.dumps(val, ensure_ascii=False) if val else "")
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
        # 反查 agent_task_id（event_seq = exec_id）
        agent_task_id = ""
        try:
            with db_lock:
                c2 = db.cursor()
                c2.execute("SELECT id FROM agent_tasks WHERE event_seq=? ORDER BY created_at DESC LIMIT 1", (r[0],))
                at_row = c2.fetchone()
                if at_row:
                    agent_task_id = at_row[0]
        except Exception:
            pass
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
            "agent_task_id": agent_task_id,
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
    title: Optional[str] = None
    description: Optional[str] = None
    scene_id: Optional[str] = None


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

    # 创建真实 Issue（前端可覆盖标题/描述/场景）
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

    # 调 create_task_via_issue（前端传的 title/desc/scene_id 优先，否则用 finding 原值）
    from src.api.issues_center import TaskCreateBody, create_task_via_issue
    issue_body = TaskCreateBody(
        repo_key=repo_key,
        title=body.title or finding["title"],
        description=body.description or finding.get("description", ""),
        scene_id=body.scene_id or "",
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


# ====== 巡检指令润色 ======

class PolishBody(BaseModel):
    name: str = ""
    instruction: str = ""


@router.post("/polish")
async def polish_instruction(body: PolishBody, user: dict = Depends(get_current_user)):
    """用 LLM 润色巡检指令，返回优化后的指令内容。"""
    if not body.instruction or not body.instruction.strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "巡检指令不能为空"})

    import os, json, urllib.request, time
    import yaml

    # 从 hermes config.yaml 读 LLM 配置
    hermes_home = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
    cfg_path = os.path.join(hermes_home, "config.yaml")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            hermes_cfg = yaml.safe_load(f) or {}
    except Exception as e:
        raise HTTPException(status_code=503, detail={"code": 503, "message": f"无法读取 LLM 配置: {e}"})

    model_cfg = hermes_cfg.get("model", {})
    base_url = model_cfg.get("base_url", "").rstrip("/")
    # 润色用轻量模型，不走默认的 glm-5.2（响应慢）
    model_name = "deepseek-v4-flash-0731"
    api_key = ""

    # api_key 可能是 ${ENV_VAR} 格式
    raw_key = model_cfg.get("api_key", "")
    if raw_key.startswith("${") and raw_key.endswith("}"):
        env_name = raw_key[2:-1]
        api_key = os.environ.get(env_name, "")
    elif raw_key:
        api_key = raw_key

    # fallback: 如果主配置缺 api_key，试 fallback_providers
    if not api_key or not base_url:
        for fb in hermes_cfg.get("fallback_providers", []):
            fb_url = fb.get("base_url", "").rstrip("/")
            fb_model = fb.get("model") or model_name
            key_env = fb.get("key_env", "")
            fb_key = os.environ.get(key_env, "") if key_env else fb.get("api_key", "")
            if fb_url and fb_key:
                base_url = base_url or fb_url
                model_name = model_name or fb_model
                api_key = api_key or fb_key
                break

    if not base_url or not api_key or not model_name:
        raise HTTPException(status_code=503, detail={"code": 503, "message": "LLM 配置不完整（缺 base_url/api_key/model）"})

    polish_prompt = f"""请优化以下自动化任务指令，使其更清晰、更结构化。

要求：
1. 理清执行步骤，用编号列表排列
2. 明确期望的输出或完成标准
3. 不要扩展用户意图之外的内容
4. 输出纯文本，不要 markdown 标题语法（不要用 #）

任务名称：{body.name}
初步指令：
{body.instruction}

请直接输出优化后的指令内容，不要加任何解释说明。"""

    def _call_llm():
        _t0 = time.time()
        body_json = json.dumps({
            "model": model_name,
            "messages": [{"role": "user", "content": polish_prompt}],
            "temperature": 0.3,
            "max_tokens": 1000,
            "thinking": {"type": "disabled"},
        }).encode("utf-8")
        req = urllib.request.Request(f"{base_url}/chat/completions", data=body_json, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {api_key}")
        _t1 = time.time()
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _t2 = time.time()
        log.info(f"Autopilot polish LLM: build={(_t1-_t0)*1000:.0f}ms request={(_t2-_t1)*1000:.0f}ms total={(_t2-_t0)*1000:.0f}ms finish={data.get('choices',[{}])[0].get('finish_reason','?')}")
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        return content.strip()

    try:
        import asyncio
        polished = await asyncio.to_thread(_call_llm)
        if not polished:
            raise HTTPException(status_code=500, detail={"code": 500, "message": "润色结果为空"})
        return {"instruction": polished}
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"Autopilot polish: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"润色失败: {e}"})
