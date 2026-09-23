"""Autopilot 定时调度引擎 v3.0

重构要点（v3.0）：
- 不再创建 Issue 触发完整流水线，改为直接执行单个 agent
- 不走 run_pipeline / build_devops_agent_prompt / 事件系统
- prompt 在 _build_autopilot_prompt 中直接构建（巡检指令 + findings 格式模板）
- 底层复用 pick_engine / get_agent_workdir / _materialize_direct_workdir / stream_devops_agent
- 执行结果独立存储在 autopilot_executions，不写 issue 表 / events 表
- agent 产出中解析问题列表(findings)，用户可手动派发为真实 Issue 走完整流水线
- event_seq 用 autopilot_executions.id（非0，避免多任务并发覆盖）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

from src.config import log
from src.session_manager import db, db_lock

_SCHEDULER_INTERVAL = 60  # 秒：调度器扫描间隔
_BASE_RETRY_DELAY = 10    # 秒：指数退避基数
_MAX_RETRY_DELAY = 300    # 秒：退避上限
_RUNNING_TASKS: set[int] = set()  # 内存互斥锁：正在执行的 task_id 集合

# 可选 agent 类型（排除 vod 和 coding）
_VALID_AGENT_TYPES = {"issue", "qa", "sre"}

# ====== findings 解析 ======

_FINDINGS_PATTERN = re.compile(
    r'<!--\s*autopilot-findings\s*\n(.*?)\n-->',
    re.DOTALL
)

_FINDINGS_FORMAT_TEMPLATE = """\
请在执行结果的末尾包含以下格式的标记块（即使未发现问题也要包含空列表）：

<!-- autopilot-findings
{
  "summary": "本次巡检总结（一句话）",
  "findings": [
    {
      "id": "f1",
      "title": "问题标题",
      "severity": "high",
      "category": "security",
      "description": "问题描述和建议",
      "suggested_assignee": "coding",
      "suggested_skill": "implementation"
    }
  ]
}
-->

字段说明：
- severity: high/medium/low
- category: security/quality/documentation/performance/other
- findings 为空数组表示未发现问题"""


def parse_findings(agent_output: str) -> dict:
    """从 agent 产出中解析问题列表。

    返回: {"summary": str, "findings": list, "parse_error": str}
    解析失败时 findings 为空列表。
    """
    match = _FINDINGS_PATTERN.search(agent_output or "")
    if not match:
        return {"summary": "", "findings": [], "parse_error": "no findings block found"}

    try:
        data = json.loads(match.group(1))
        findings = data.get("findings", [])
        valid = []
        for f in findings:
            if not isinstance(f, dict):
                continue
            if not f.get("id") or not f.get("title") or not f.get("description"):
                continue
            f.setdefault("severity", "medium")
            f.setdefault("suggested_assignee", "coding")
            f.setdefault("suggested_skill", "implementation")
            valid.append(f)
        return {
            "summary": data.get("summary", ""),
            "findings": valid,
            "parse_error": "" if valid else "no valid findings"
        }
    except json.JSONDecodeError as e:
        return {"summary": "", "findings": [], "parse_error": str(e)}


# ====== prompt 构建 ======

def _build_autopilot_prompt(task: dict, repo_key: str, repository: str) -> str:
    """构建定时任务的 prompt。不依赖 issue/scene/pipeline 上下文。"""
    parts = []
    parts.append("## 定时巡检任务\n")
    parts.append(f"任务名称: {task['name']}\n")
    parts.append(f"仓库: {repository}\n")
    parts.append(f"巡检指令:\n{task.get('instruction', '')}\n")

    # skill 注入（可选）
    skill_name = task.get("skill_name", "")
    if skill_name:
        parts.append(f"\n## Skill: {skill_name}\n")
        parts.append(f"Skill 文件：{{{{SKILL_DIR}}}}/{skill_name}/SKILL.md\n")
        parts.append("**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令。\n")

    parts.append("\n请执行以上巡检指令，分析仓库代码/依赖/文档/配置等。")
    parts.append("\n## 输出格式要求\n")
    parts.append(_FINDINGS_FORMAT_TEMPLATE)
    parts.append("\n## 项目知识\n")
    parts.append("项目知识文档位于工作目录中，存在则用 read tool 读取。")
    parts.append("\n执行以上指令，不要操作 GitCode Issue（不需要回帖/打标签/关 Issue），结果直接输出。")
    return "\n".join(parts)


# ====== agent 执行 ======

def _execute_autopilot_agent(repo_key: str, task: dict, exec_id: int):
    """执行单个 autopilot agent（在线程池中运行）。

    不走 _execute_agent_inner / build_devops_agent_prompt / 事件系统。
    直接构建 prompt → 物化 workdir → 调 stream_devops_agent。
    """
    from src.repo_key import from_repo_key, split_owner_repo
    from src.runtime.engine_select import pick_engine, engine_name
    from src.runtime.instances import runtime_instance_name
    from src.api.event_pipeline import (
        get_agent_workdir, _materialize_direct_workdir, get_agent_display_name,
        _create_direct_task,
    )

    agent_type = task.get("agent_type", "issue")
    skill_name = task.get("skill_name", "")
    repository = from_repo_key(repo_key) or repo_key

    # 1. 选引擎
    engine_id = pick_engine(repo_key, agent_type, fallback_runtime_id=0)
    if engine_id is None:
        log.warning(f"Autopilot exec {exec_id}: no engine for {agent_type}")
        _update_execution_result(exec_id, "failed", error="无可用引擎")
        return

    inst_name = runtime_instance_name(engine_id) or engine_name(engine_id) or ""
    backend = inst_name

    # 判断执行模式：DIRECT（本机）或 REMOTE（远端 worker）/ 广播
    from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT, get_execution_mode
    from src.api.event_pipeline import is_plugin_mode
    if engine_id:
        _kind = runtime_execution_kind(engine_id)
    else:
        # engine_id=0: plugin 模式下是广播，否则 direct
        _kind = EXEC_MODE_DIRECT if (not is_plugin_mode() or get_execution_mode() == EXEC_MODE_DIRECT) else "broadcast"
    _is_broadcast = (_kind == "broadcast")
    _is_direct = (_kind == EXEC_MODE_DIRECT)

    # 2. 工作目录
    ws = get_agent_workdir(repo_key, agent_type, "")
    os.makedirs(ws, exist_ok=True)

    # 3. bot token（按 provider 分流）
    repo_token = ""
    try:
        if repo_key.startswith("github:"):
            from src.user.db import get_github_token
            repo_token = get_github_token(repo_key=repo_key) or ""
        else:
            from src.user.db import get_gitcode_token
            repo_token = get_gitcode_token(repo_key=repo_key) or ""
    except Exception as e:
        log.warning(f"Autopilot exec {exec_id}: get bot token failed: {e}")

    # 4. 构建 prompt
    prompt = _build_autopilot_prompt(task, repo_key, repository)

    # 5. 物化 workdir（project-info + skill 如果有）
    evt = {"repo_key": repo_key, "issue_id": "", "payload": {}}
    prompt = _materialize_direct_workdir(ws, repo_key, agent_type, skill_name, evt, "", prompt)

    # 6. 环境变量（定时任务不需要场景/issue相关变量）
    agent_display_name = get_agent_display_name(agent_type)
    env_extra = {
        "REPO_FULL_NAME": repository,
        "ACCOUNT": repository,
    }

    # 7. 分流执行
    if _is_direct:
        # DIRECT 模式：本机执行
        _execute_autopilot_direct(repo_key, task, exec_id, agent_type, skill_name,
                                  engine_id, backend, inst_name, ws, prompt,
                                  repo_token, agent_display_name, env_extra, repository)
    else:
        # REMOTE / 广播模式：入队让远端 worker 认领
        _execute_autopilot_remote(repo_key, task, exec_id, agent_type, skill_name,
                                  engine_id, backend, inst_name, ws, prompt,
                                  repo_token, agent_display_name, env_extra, repository, evt)


def _execute_autopilot_direct(repo_key, task, exec_id, agent_type, skill_name,
                              engine_id, backend, inst_name, ws, prompt,
                              repo_token, agent_display_name, env_extra, repository):
    """DIRECT 模式：本机 stream_devops_agent 执行。"""
    from src.api.event_pipeline import _create_direct_task

    # 创建 agent_tasks 记录（用 exec_id 作为 event_seq，skip_event_config=True 不写 events 表）
    task_id = _create_direct_task(
        repo_key, exec_id, agent_type, backend, skill_name,
        "", engine_id, {"repo_key": repo_key, "issue_id": "", "payload": {}},
        issue_id="", skill_id=0, skip_event_config=True,
    )
    if not task_id:
        log.warning(f"Autopilot exec {exec_id}: _create_direct_task failed")
        _update_execution_result(exec_id, "failed", error="创建任务记录失败")
        return

    log.info(f"Autopilot exec {exec_id}: starting direct agent={agent_type} engine={inst_name}")

    exit_code = -1
    final_text = ""

    try:
        from src.agent.executor import stream_devops_agent
        for evt_type, data in stream_devops_agent(
            prompt, ws, session_id="",
            event_type="autopilot_event",
            account=repository, bot_token=repo_token,
            agent_name=agent_display_name,
            agent_label=f"[Autopilot:{agent_type}|repo:{repository}|engine:{inst_name}]",
            env_extra=env_extra,
            repo_key=repo_key,
        ):
            if evt_type == "done":
                d = data
                exit_code = d.get("exit_code", -1)
                final_text = d.get("text", "")
                log.info(f"Autopilot exec {exec_id}: done exit={exit_code} text_len={len(final_text)}")
    except Exception as e:
        log.error(f"Autopilot exec {exec_id}: agent execution crashed: {e}")
        final_text = f"执行异常: {e}"
        exit_code = 1

    # 更新 agent_tasks 状态（用 task_id 精确更新）
    _finalize_autopilot_task(task_id, exec_id, exit_code, final_text)


def _execute_autopilot_remote(repo_key, task, exec_id, agent_type, skill_name,
                              engine_id, backend, inst_name, ws, prompt,
                              repo_token, agent_display_name, env_extra, repository, evt):
    """REMOTE / 广播模式：入队让远端 worker 认领执行。"""
    from src.runtime.service import enqueue_task
    from src.api.event_pipeline import get_agent_workdir as _gaw
    from src.api.session_store import get_or_create_agent_session
    from src.prompt_builder import _resolve_server_url

    # 获取会话
    session = get_or_create_agent_session(repo_key, agent_type, backend or "")
    agent_session_id = session.get("id", "")
    if not agent_session_id:
        log.warning(f"Autopilot exec {exec_id}: no agent_session_id for {repo_key}/{agent_type}")
        _update_execution_result(exec_id, "failed", error="无 agent 会话")
        return

    # 广播模式设 pool_tag
    _pool_tag = agent_type if engine_id == 0 else ""

    log.info(f"Autopilot exec {exec_id}: enqueuing remote agent={agent_type} engine={inst_name} pool_tag={_pool_tag}")

    try:
        task_id = enqueue_task(
            agent_session_id=agent_session_id,
            runtime_id=engine_id,
            prompt=prompt,
            cwd=ws,
            runtime_session_id="",
            event_type="autopilot_event",
            account=repository,
            user_id=0,
            repo_gitcode_token=repo_token,
            user_gitcode_token="",
            env_extra=json.dumps(env_extra),
            event_seq=exec_id,
            issue_id="",
            repo_key=repo_key,
            agent_type=agent_type,
            skill=skill_name,
            skill_path="",
            skill_id=0,
            server_url=_resolve_server_url(engine_id),
            issue_session_id="",
            pool_agent_type=_pool_tag,
            pool_tag=_pool_tag,
            pool_engine_ids="",
        )
        if not task_id:
            log.warning(f"Autopilot exec {exec_id}: enqueue_task failed")
            _update_execution_result(exec_id, "failed", error="入队失败")
            return

        log.info(f"Autopilot exec {exec_id}: enqueued task_id={task_id}, waiting for worker...")

        # 远端执行是异步的，worker 认领后执行完会通过 report_messages/task_complete 回调更新 agent_tasks
        # autopilot_executions 状态需要轮询 agent_tasks 的状态来更新
        # 这里标记为 "dispatched"，后续由轮询或手动检查更新终态
        _update_execution_result(exec_id, "dispatched",
                                 result=f"已入队 task_id={task_id}，等待 worker 执行")

    except Exception as e:
        log.error(f"Autopilot exec {exec_id}: enqueue failed: {e}")
        _update_execution_result(exec_id, "failed", error=f"入队异常: {e}")


def _finalize_autopilot_task(task_id: str, exec_id: int, exit_code: int, final_text: str):
    """执行完成后更新 agent_tasks + autopilot_executions + 解析 findings。"""
    # 更新 agent_tasks 状态（用 task_id 精确更新）
    try:
        from src.session_manager import db as _tdb, db_lock as _tdl
        with _tdl:
            _tc = _tdb.cursor()
            _tc.execute(
                "UPDATE agent_tasks SET status=?, exit_code=?, result_text=?, completed_at=? WHERE id=?",
                ("completed" if exit_code == 0 else "failed", exit_code,
                 final_text[:5000], datetime.now(timezone.utc).isoformat(), task_id)
            )
            _tdb.commit()
    except Exception as e:
        log.warning(f"Autopilot exec {exec_id}: update agent_tasks failed: {e}")

    # 解析 findings
    findings_result = parse_findings(final_text)

    # 更新 autopilot_executions
    status = "success" if exit_code == 0 else "failed"
    _update_execution_result(exec_id, status,
                             result=final_text[:5000],
                             findings_json=json.dumps(findings_result, ensure_ascii=False) if findings_result["findings"] else "")

    log.info(f"Autopilot exec {exec_id}: completed status={status} findings={len(findings_result['findings'])}")


def _update_execution_result(exec_id: int, status: str, result: str = "",
                             error: str = "", findings_json: str = ""):
    """更新 autopilot_executions 执行结果。"""
    fields = ["status=?"]
    vals = [status]
    if result:
        fields.append("result=?")
        vals.append(result)
    if error:
        fields.append("error=?")
        vals.append(error)
    if findings_json:
        fields.append("findings_json=?")
        vals.append(findings_json)
    if status in ("success", "failed"):
        fields.append("completed_at=?")
        vals.append(datetime.now(timezone.utc).isoformat())
    vals.append(exec_id)
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"UPDATE autopilot_executions SET {', '.join(fields)} WHERE id=?",
                vals
            )
            db.commit()
    except Exception as e:
        log.warning(f"Autopilot: update execution {exec_id} failed: {e}")


# ====== 调度器 ======

def _get_enabled_tasks() -> list[dict]:
    """获取所有启用的 autopilot 任务。"""
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT id, name, repo_key, agent_type, instruction, skill_name, "
            "cron, max_retry, created_by FROM autopilot_tasks WHERE enabled=1"
        ).fetchall()
    tasks = []
    for r in rows:
        tasks.append({
            "id": r[0], "name": r[1], "repo_key": r[2],
            "agent_type": r[3], "instruction": r[4], "skill_name": r[5],
            "cron": r[6], "max_retry": r[7], "created_by": r[8] if len(r) > 8 else 0,
        })
    return tasks


def _is_cron_due(cron_expr: str, last_run: str) -> bool:
    """判断 cron 表达式是否在当前时间窗口（最近 60s）内到期。"""
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
    """计算 cron 表达式的下次触发时间（本地时区，ISO 格式）。"""
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


def _create_execution(task_id: int, agent_type: str) -> int:
    """创建执行记录，返回 execution_id。"""
    now_str = datetime.now().astimezone().isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO autopilot_executions "
            "(task_id, status, retry_count, started_at, created_at, agent_type) "
            "VALUES (?, 'running', 0, ?, ?, ?)",
            (task_id, now_str, now_str, agent_type)
        )
        db.commit()
        return c.lastrowid


def _update_execution(exec_id: int, status: str, result: str = "", error: str = "",
                      retry_count: Optional[int] = None):
    """更新执行记录状态（兼容旧调用，内部转 _update_execution_result）。"""
    _update_execution_result(exec_id, status, result=result, error=error)


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


async def _poll_dispatched_completion(exec_id: int, timeout: int = 600) -> str:
    """轮询 dispatched 状态的 exec 等待 worker 完成执行。

    通过查 agent_tasks（event_seq=exec_id）的状态推断 autopilot_executions 终态：
    - agent_tasks status=completed/success → exec success
    - agent_tasks status=failed → exec failed
    - agent_tasks status 仍为 queued/dispatched/running → 继续等
    - 超时 → exec failed (timeout)
    """
    import time as _time
    _start = _time.time()
    while _time.time() - _start < timeout:
        await asyncio.sleep(10)  # 每10秒查一次
        try:
            with db_lock:
                c = db.cursor()
                # 查 agent_tasks 按 event_seq=exec_id
                row = c.execute(
                    "SELECT status, result_text, exit_code FROM agent_tasks "
                    "WHERE event_seq=? ORDER BY created_at DESC LIMIT 1",
                    (exec_id,)
                ).fetchone()
            if not row:
                continue
            _at_status = row[0] or ""
            _result_text = row[1] or ""
            _exit_code = row[2]

            if _at_status in ("completed", "success"):
                # worker 完成，同步终态到 autopilot_executions
                findings_result = parse_findings(_result_text)
                _update_execution_result(exec_id, "success",
                                         result=_result_text[:5000],
                                         findings_json=json.dumps(findings_result, ensure_ascii=False) if findings_result["findings"] else "")
                return "success"
            elif _at_status in ("failed", "cancelled"):
                _update_execution_result(exec_id, "failed",
                                         result=_result_text[:5000],
                                         error=f"agent_tasks status={_at_status}")
                return "failed"
            # queued/dispatched/running → 继续等
        except Exception as e:
            log.warning(f"Autopilot poll exec {exec_id}: query failed: {e}")

    # 超时
    _update_execution_result(exec_id, "failed", error=f"worker 执行超时 ({timeout}s)")
    return "failed"


async def _execute_task(task: dict):
    """执行单个 autopilot 任务（含重试逻辑）。"""
    task_id = task["id"]
    if task_id in _RUNNING_TASKS:
        log.info(f"Autopilot: task {task_id} already running, skip")
        return
    _RUNNING_TASKS.add(task_id)
    try:
        max_retry = task["max_retry"]
        agent_type = task.get("agent_type", "issue")

        for attempt in range(max_retry + 1):
            exec_id = _create_execution(task_id, agent_type)
            log.info(f"Autopilot: executing task {task_id} ('{task['name']}'), exec_id={exec_id}, attempt={attempt}")

            # 在线程池中执行 agent（不阻塞事件循环）
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _execute_autopilot_agent, task["repo_key"], task, exec_id)

            # 检查执行结果
            with db_lock:
                c = db.cursor()
                row = c.execute("SELECT status, error FROM autopilot_executions WHERE id=?", (exec_id,)).fetchone()
            exec_status = row[0] if row else "unknown"

            # REMOTE 模式下状态为 "dispatched"，需要轮询等待 worker 完成
            if exec_status == "dispatched":
                log.info(f"Autopilot: task {task_id} exec {exec_id} dispatched, polling worker completion...")
                exec_status = await _poll_dispatched_completion(exec_id, timeout=600)

            if exec_status == "success":
                log.info(f"Autopilot: task {task_id} exec {exec_id} success (attempt {attempt})")
                break
            if attempt < max_retry:
                error_msg = row[1] if row else "unknown"
                delay = min(_BASE_RETRY_DELAY * (2 ** attempt), _MAX_RETRY_DELAY)
                log.warning(f"Autopilot: task {task_id} exec {exec_id} attempt {attempt} failed ({exec_status}), "
                            f"retrying in {delay}s: {error_msg}")
                await asyncio.sleep(delay)
            else:
                log.error(f"Autopilot: task {task_id} exec {exec_id} failed after {max_retry} retries ({exec_status})")

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
    """手动触发任务执行（异步入口）。"""
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id, name, repo_key, agent_type, instruction, skill_name, "
            "cron, max_retry, created_by FROM autopilot_tasks WHERE id=?",
            (task_id,)
        ).fetchone()
    if not row:
        return {"ok": False, "message": "task not found"}
    task = {
        "id": row[0], "name": row[1], "repo_key": row[2],
        "agent_type": row[3], "instruction": row[4], "skill_name": row[5],
        "cron": row[6], "max_retry": row[7], "created_by": row[8] if len(row) > 8 else 0,
    }
    if task["id"] in _RUNNING_TASKS:
        return {"ok": False, "message": "task already running"}
    asyncio.create_task(_execute_task(task))
    return {"ok": True, "message": "task triggered"}
