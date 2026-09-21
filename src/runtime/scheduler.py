"""Autopilot 定时调度引擎（Issue #150 §5.3.1 + §5.3.3）

基于 croniter + asyncio 的轻量 cron 调度器：
- 每分钟扫描启用任务，判断 cron 是否到期
- 到期任务通过 HTTP POST 调用 /api/issues 创建新 Issue 触发场景流水线
- 失败按指数退避重试（base_delay * 2^retry_count，上限 300s）
- 单任务并发互斥（同一 task_id 同时只允许一个执行）
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional

from src.config import log
from src.session_manager import db, db_lock
from fastapi import HTTPException

_A2A_BASE_URL = os.environ.get("GRAPE_API_BASE_URL", "http://127.0.0.1:3000")
_SCHEDULER_INTERVAL = 60  # 秒：调度器扫描间隔
_BASE_RETRY_DELAY = 10    # 秒：指数退避基数
_MAX_RETRY_DELAY = 300    # 秒：退避上限
_RUNNING_TASKS: set[int] = set()  # 内存互斥锁：正在执行的 task_id 集合

_utc_now = lambda: datetime.now(timezone.utc)


def _get_enabled_tasks() -> list[dict]:
    """获取所有启用的 autopilot 任务。"""
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT id, name, repo_key, scene_id, description, cron, agent_chain, issue_number, max_retry, created_by "
            "FROM autopilot_tasks WHERE enabled=1"
        ).fetchall()
    tasks = []
    for r in rows:
        tasks.append({
            "id": r[0], "name": r[1], "repo_key": r[2],
            "scene_id": r[3], "description": r[4], "cron": r[5],
            "agent_chain": r[6], "issue_number": r[7], "max_retry": r[8],
            "created_by": r[9] if len(r) > 9 else 0,
        })
    return tasks


def _is_cron_due(cron_expr: str, last_run: str) -> bool:
    """判断 cron 表达式是否在当前时间窗口（最近 60s）内到期。

    使用 croniter 计算下次触发时间，与当前时间对比。
    last_run 为空（从未运行）时，检查上次理论触发时间是否在 60s 内。
    使用本地时区，与用户 UI 选择的时间一致。
    """
    try:
        from croniter import croniter
        now = datetime.now().astimezone()
        if last_run:
            try:
                last_dt = datetime.fromisoformat(last_run.replace(" ", "T"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.astimezone()
            except Exception:
                last_dt = now
            cron = croniter(cron_expr, last_dt)
            next_fire = cron.get_next(datetime)
            if next_fire.tzinfo is None:
                next_fire = next_fire.astimezone()
            return next_fire <= now
        else:
            # 从未运行：检查上次理论触发时间是否在 60s 内
            cron = croniter(cron_expr, now)
            prev_fire = cron.get_prev(datetime)
            if prev_fire.tzinfo is None:
                prev_fire = prev_fire.astimezone()
            delta = (now - prev_fire).total_seconds()
            return delta < _SCHEDULER_INTERVAL
    except Exception as e:
        log.warning(f"Autopilot: cron parse failed '{cron_expr}': {e}")
        return False


def _compute_next_run(cron_expr: str) -> str:
    """计算 cron 表达式的下次触发时间（本地时区，ISO 格式）。

    用户在 UI 上选择的时间是本地时间（如 09:00 = 北京时间 09:00），
    cron 表达式按本地时间解释。返回带时区的 ISO 字符串供前端显示。
    """
    try:
        from croniter import croniter
        # 用本地时间计算 cron，保持与用户 UI 选择一致
        now_local = datetime.now().astimezone()
        cron = croniter(cron_expr, now_local)
        nxt = cron.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.astimezone()
        return nxt.isoformat()
    except Exception:
        return ""


def _create_execution(task_id: int) -> int:
    """创建执行记录，返回 execution_id。"""
    now_str = datetime.now().astimezone().isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO autopilot_executions "
            "(task_id, status, retry_count, agent_chain_step, started_at, created_at) "
            "VALUES (?, 'running', 0, 0, ?, ?)",
            (task_id, now_str, now_str)
        )
        db.commit()
        return c.lastrowid


def _update_execution(exec_id: int, status: str, result: str = "", error: str = "",
                      a2a_seq: Optional[int] = None, retry_count: Optional[int] = None,
                      issue_number: Optional[str] = None):
    """更新执行记录状态。

    completed_at 仅在终态（success/failed）时设置，retrying 状态不更新完成时间。
    """
    fields = ["status=?", "result=?", "error=?"]
    vals = [status, result, error]
    if status in ("success", "failed"):
        fields.append("completed_at=?")
        vals.append(_utc_now().isoformat())
    if a2a_seq is not None:
        fields.append("a2a_seq=?")
        vals.append(a2a_seq)
    if retry_count is not None:
        fields.append("retry_count=?")
        vals.append(retry_count)
    if issue_number is not None:
        fields.append("issue_number=?")
        vals.append(issue_number)
    vals.append(exec_id)
    with db_lock:
        c = db.cursor()
        c.execute(
            f"UPDATE autopilot_executions SET {', '.join(fields)} WHERE id=?",
            vals
        )
        db.commit()


def _update_task_run_info(task_id: int, next_run: str):
    """更新任务最近运行时间和下次预计运行时间。"""
    now_str = datetime.now().astimezone().isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE autopilot_tasks SET last_run_at=?, next_run_at=?, updated_at=? WHERE id=?",
            (now_str, next_run, now_str, task_id)
        )
        db.commit()


def _get_repo_bot_token(repo_key: str) -> str:
    """获取仓库的 bot token 用于 A2A 鉴权。"""
    try:
        from src.user.db import get_repo_bot_token_no_global
        return get_repo_bot_token_no_global(repo_key) or ""
    except Exception:
        return ""


async def _create_issue_and_trigger(task: dict, exec_id: int) -> dict:
    """直接调用 create_task_via_issue 创建新 Issue，grape 自动触发场景流水线。

    返回 {"ok": bool, "seq": int, "issue_number": str, "error": str}。
    """
    repo_key = task["repo_key"]
    repository = repo_key.split(":", 1)[1] if ":" in repo_key else repo_key
    parts = repository.split("/")
    if len(parts) < 2:
        return {"ok": False, "seq": 0, "issue_number": "", "error": f"invalid repo_key: {repo_key}"}
    owner, repo = parts[0], parts[1]

    # 标题 = 任务名称 + 时间戳
    timestamp = _utc_now().strftime("%Y%m%d-%H%M")
    issue_title = f"{task['name']} {timestamp}"

    # 用仓库 bot token 创建 Issue（不过期），username 用创建者身份（绕过 self_loop）
    from src.session_manager import db, db_lock
    _bot_token = _get_repo_bot_token(repo_key)
    _creator_uid = task.get("created_by", 0) or 0
    _creator_username = owner
    if _creator_uid:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT gitcode_user FROM unified_user_mapping WHERE user_id=? LIMIT 1",
                    (_creator_uid,)
                )
                row = c.fetchone()
                if row:
                    _creator_username = row[0] or owner
        except Exception:
            pass

    # 直接调用 create_task_via_issue（不走 HTTP，无需认证）
    try:
        from src.api.routes.tasks import TaskCreateBody, create_task_via_issue
        body = TaskCreateBody(
            repo_key=repo_key,
            title=issue_title,
            description=task.get("description", ""),
            scene_id=task.get("scene_id", "default"),
        )
        internal_user = {"id": _creator_uid, "username": _creator_username, "token": _bot_token, "auth_type": "internal"}
        result = await create_task_via_issue(body, user=internal_user)
        issue_number = str(result.get("issue_number", ""))
        if issue_number and issue_number != "0":
            return {"ok": True, "seq": 0, "issue_number": issue_number, "error": ""}
        return {"ok": False, "seq": 0, "issue_number": "", "error": f"create issue returned no number: {result}"}
    except HTTPException as e:
        msg = e.detail.get("message", str(e)) if isinstance(e.detail, dict) else str(e)
        return {"ok": False, "seq": 0, "issue_number": "", "error": f"create issue failed (http {e.status_code}): {msg}"}
    except Exception as e:
        return {"ok": False, "seq": 0, "issue_number": "", "error": f"create issue failed: {e}"}


async def _execute_task(task: dict):
    """执行单个 autopilot 任务（含重试逻辑）。"""
    task_id = task["id"]
    if task_id in _RUNNING_TASKS:
        log.info(f"Autopilot: task {task_id} already running, skip")
        return
    _RUNNING_TASKS.add(task_id)
    try:
        max_retry = task["max_retry"]
        exec_id = _create_execution(task_id)
        log.info(f"Autopilot: executing task {task_id} ('{task['name']}'), exec_id={exec_id}")

        for attempt in range(max_retry + 1):
            result = await _create_issue_and_trigger(task, exec_id)
            if result["ok"]:
                _update_execution(exec_id, "success", result=f"issue #{result['issue_number']}",
                                  a2a_seq=result["seq"], retry_count=attempt,
                                  issue_number=result.get("issue_number", ""))
                log.info(f"Autopilot: task {task_id} exec {exec_id} success (attempt {attempt})")
                break
            if attempt < max_retry:
                delay = min(_BASE_RETRY_DELAY * (2 ** attempt), _MAX_RETRY_DELAY)
                _update_execution(exec_id, "retrying", error=result["error"], retry_count=attempt)
                log.warning(f"Autopilot: task {task_id} exec {exec_id} attempt {attempt} failed, "
                            f"retrying in {delay}s: {result['error']}")
                await asyncio.sleep(delay)
            else:
                _update_execution(exec_id, "failed", error=result["error"], retry_count=attempt)
                log.error(f"Autopilot: task {task_id} exec {exec_id} failed after {max_retry} retries: {result['error']}")

        next_run = _compute_next_run(task["cron"])
        _update_task_run_info(task_id, next_run)
    except Exception as e:
        log.error(f"Autopilot: task {task_id} execution exception: {e}")
    finally:
        _RUNNING_TASKS.discard(task_id)


async def scheduler_loop():
    """Autopilot 调度主循环——每 60 秒扫描到期任务。"""
    log.info("Autopilot scheduler loop started (60s interval)")
    while True:
        try:
            tasks = _get_enabled_tasks()
            for task in tasks:
                task_id = task["id"]
                if task_id in _RUNNING_TASKS:
                    continue
                with db_lock:
                    c = db.cursor()
                    row = c.execute("SELECT last_run_at FROM autopilot_tasks WHERE id=?", (task_id,)).fetchone()
                    last_run = row[0] if row else ""
                if _is_cron_due(task["cron"], last_run):
                    log.info(f"Autopilot: cron due for task {task_id} ('{task['name']}')")
                    asyncio.ensure_future(_execute_task(task))
        except Exception as e:
            log.warning(f"Autopilot scheduler loop error: {e}")
        await asyncio.sleep(_SCHEDULER_INTERVAL)


_scheduler_task: Optional[asyncio.Task] = None


def start_scheduler():
    """启动 Autopilot 调度器（在 FastAPI lifespan 中调用）。"""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        log.info("Autopilot scheduler already running")
        return
    _scheduler_task = asyncio.ensure_future(scheduler_loop())
    log.info("Autopilot scheduler started")


async def stop_scheduler():
    """停止 Autopilot 调度器（在 FastAPI lifespan shutdown 中调用）。"""
    global _scheduler_task
    if _scheduler_task:
        _scheduler_task.cancel()
        try:
            await asyncio.wait_for(_scheduler_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        _scheduler_task = None
        log.info("Autopilot scheduler stopped")


async def trigger_task_now(task_id: int) -> dict:
    """手动触发任务执行（异步入口，在事件循环中调用）。

    返回 {"ok": bool, "message": str}。
    """
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id, name, repo_key, scene_id, description, cron, agent_chain, issue_number, max_retry, created_by "
            "FROM autopilot_tasks WHERE id=?", (task_id,)
        ).fetchone()
    if not row:
        return {"ok": False, "message": "task not found"}
    task = {
        "id": row[0], "name": row[1], "repo_key": row[2],
        "scene_id": row[3], "description": row[4], "cron": row[5],
        "agent_chain": row[6], "issue_number": row[7], "max_retry": row[8],
        "created_by": row[9] if len(row) > 9 else 0,
    }
    if task["id"] in _RUNNING_TASKS:
        return {"ok": False, "message": "task already running"}
    asyncio.create_task(_execute_task(task))
    return {"ok": True, "message": "task triggered"}
