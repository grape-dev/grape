"""服务端 RuntimeService：任务队列（确定路由 + 串行槽）与本地 worker 执行。

见 agent-runtime-event-tech-impl.md §1.3 / concurrency-design.md §2.2-2.4。
执行路径：业务层 enqueue_task → agent_tasks(status=queued) → claim（原子抢单+D9）
→ 本地线程池 stream_runtime → report/complete/fail。
"""
import os, json, threading, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from src.session_manager import db, db_lock
from src.config import log

# ===== 执行模式开关（M2，见 implementation-plan §三 / concurrency-design.md）=====
# direct（就地 stream_agent）/ queue_local（入队 + 本地 worker）/ queue_remote（远端 RuntimeAgent）
EXEC_MODE_DIRECT = "direct"
EXEC_MODE_LOCAL = "queue_local"
EXEC_MODE_REMOTE = "queue_remote"

_DEFAULT_MODE = os.environ.get("DEVOPS_EXECUTION_MODE", EXEC_MODE_DIRECT)


def get_execution_mode() -> str:
    """返回当前执行模式：env 优先，其次 meta 表。"""
    _m = os.environ.get("DEVOPS_EXECUTION_MODE", "")
    if _m:
        return _m
    try:
        from src.user.db import get_meta_value
        _m = get_meta_value("execution_mode") or ""
    except Exception:
        pass
    return _m or _DEFAULT_MODE


def set_execution_mode(mode: str):
    """设置执行模式（落 meta 表，运维可改键值 + 重启生效）。"""
    try:
        from src.user.db import set_meta_value
        set_meta_value("execution_mode", mode)
        log.info(f"execution_mode set to {mode}")
    except Exception as e:
        log.warning(f"set_execution_mode failed: {e}")

# ===== 本地 worker 执行池（有界，替代无界 threading.Thread，见 concurrency-design.md §2.2）=====
_DEFAULT_MAX_CONCURRENT = int(os.environ.get("RUNTIME_MAX_CONCURRENT_TASKS", "20"))
_worker_pool = ThreadPoolExecutor(max_workers=_DEFAULT_MAX_CONCURRENT, thread_name_prefix="rt-worker")

_TASK_COLUMNS = (
    "id, agent_session_id, runtime_id, event_seq, issue_id, repo_key, agent_type, skill, "
    "prompt, cwd, runtime_session_id, event_type, account, user_id, repo_gitcode_token, "
    "env_extra, skill_files, server_url, status, exit_code, result_text, errors, stderr, "
    "usage_json, started_at, completed_at, created_at"
)


def _row_to_dict(row):
    return {
        "id": row[0], "agent_session_id": row[1], "runtime_id": row[2], "event_seq": row[3],
        "issue_id": row[4], "repo_key": row[5], "agent_type": row[6], "skill": row[7],
        "prompt": row[8], "cwd": row[9], "runtime_session_id": row[10], "event_type": row[11],
        "account": row[12], "user_id": row[13], "repo_gitcode_token": row[14],
        "env_extra": row[15], "skill_files": row[16], "server_url": row[17],
        "status": row[18], "exit_code": row[19], "result_text": row[20], "errors": row[21],
        "stderr": row[22], "usage_json": row[23], "started_at": row[24], "completed_at": row[25],
        "created_at": row[26],
    }


def _pack_skill_dir(skill_name, skill_path):
    """把技能目录（SKILL.md + references/**）打包为 {rel_path: content}（远端交付，见 skill-delivery-remote.md）"""
    import os as _os
    files = {}
    base = _os.path.dirname(skill_path) if skill_path else ""
    if not base or not _os.path.isdir(base):
        return {skill_name: {}}
    for root, _dirs, names in _os.walk(base):
        for n in names:
            full = _os.path.join(root, n)
            rel = _os.path.relpath(full, base).replace("\\", "/")
            try:
                with open(full, "r", encoding="utf-8") as f:
                    files[rel] = f.read()
            except Exception:
                pass
    return {skill_name: files}


def _collect_server_env(user_id, account):
    """服务端 DB 系凭据注入（user credentials / 账户 AK-SK），远端 worker 无本地 DB 也生效。"""
    env = {}
    if user_id and user_id > 0:
        try:
            from src.user.db import get_credentials
            for c in get_credentials(user_id):
                for k, v in c.get("config", {}).items():
                    env[k.upper()] = str(v)
        except Exception:
            pass
    if account:
        try:
            from src.config import get_account
            acc = get_account(account)
            for k in ("CODEARTS_CLI_AK", "CODEARTS_CLI_SK"):
                if acc.get(k):
                    env[k] = acc[k]
        except Exception:
            pass
    return env


def enqueue_task(*, agent_session_id, runtime_id, prompt, cwd="",
                 runtime_session_id="", event_type="", account="", user_id=0,
                 repo_gitcode_token="", env_extra=None,
                 event_seq=None, issue_id="", repo_key="", agent_type="",
                 skill="", skill_path="", server_url=""):
    """业务层入队：确定路由（D3）+ 快照 + 技能打包 + 凭据注入。返回 task_id。"""
    if not agent_session_id or not runtime_id:
        raise ValueError("agent_session_id and runtime_id are required (确定路由 D3)")
    task_id = "t_" + uuid.uuid4().hex[:12]
    skill_files = {}
    if skill:
        try:
            from src.user.db import resolve_skill_id, get_skill_path
            _sid = resolve_skill_id(skill, owner_id=account) or resolve_skill_id(skill)
            _path = get_skill_path(_sid) if _sid else (skill_path or "")
            skill_files = _pack_skill_dir(skill, _path or (skill_path or ""))
        except Exception as e:
            log.warning(f"enqueue: skill pack failed for {skill}: {e}")
    env = dict(env_extra or {})
    for k, v in _collect_server_env(user_id, account).items():
        env.setdefault(k, v)
    _server_url = server_url or ""
    if not _server_url:
        try:
            from src.user.db import get_meta_value
            _server_url = get_meta_value("webhook_url") or ""
        except Exception:
            pass
    with db_lock:
        c = db.cursor()
        c.execute(
            f"INSERT INTO agent_tasks ({_TASK_COLUMNS}) VALUES "
            f"({','.join(['?'] * 27)})",
            (task_id, agent_session_id, runtime_id, event_seq, issue_id or "", repo_key or "",
             agent_type or "", skill or "", prompt, cwd or "", runtime_session_id or "",
             event_type or "", account or "", user_id or 0, repo_gitcode_token or "",
             json.dumps(env), json.dumps(skill_files), _server_url,
             "queued", None, "", "", "", "{}", None, None, datetime.now(timezone.utc).isoformat())
        )
        db.commit()
    log.info(f"Task enqueued: {task_id} session={agent_session_id} runtime={runtime_id} skill={skill}")
    return task_id


def claim(runtime_id) -> dict | None:
    """原子抢单 + 串行槽 D9（见 concurrency-design.md §2.3）。

    仅 queued → dispatched 者胜出，且同 agent_session_id 无活跃 task。
    """
    with db_lock:
        c = db.cursor()
        try:
            c.execute("""
                UPDATE agent_tasks SET status='dispatched', started_at=?
                WHERE id = (
                    SELECT id FROM agent_tasks
                    WHERE runtime_id=? AND status='queued'
                      AND NOT EXISTS (
                        SELECT 1 FROM agent_tasks a2
                        WHERE a2.agent_session_id=agent_tasks.agent_session_id
                          AND a2.status IN ('dispatched','running'))
                    ORDER BY created_at ASC LIMIT 1
                ) AND status='queued'
                RETURNING id
            """, (datetime.now(timezone.utc).isoformat(), runtime_id))
            row = c.fetchone()
            db.commit()
        except Exception as e:
            log.warning(f"claim failed: {e}")
            db.rollback()
            return None
        if not row:
            return None
        task = get_task(row[0])
        if task:
            task["status"] = "dispatched"
        return task


def get_task(task_id) -> dict | None:
    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT {_TASK_COLUMNS} FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
    return _row_to_dict(row) if row else None


def update_task_status(task_id, status, **kw):
    """终态幂等（重放安全）：已终态则不再更新（见 tech-impl §2.2）。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
        if not row:
            return
        if row[0] in ("completed", "failed", "cancelled"):
            return
        sets = ["status=?", "completed_at=?"]
        params = [status, kw.get("completed_at") or datetime.now(timezone.utc).isoformat()]
        for col in ("exit_code", "result_text", "errors", "stderr", "usage_json"):
            if col in kw:
                sets.append(f"{col}=?")
                params.append(kw[col])
        c.execute(f"UPDATE agent_tasks SET {', '.join(sets)} WHERE id=?", params + [task_id])
        db.commit()


def cancel_task(task_id):
    update_task_status(task_id, "cancelled")


def report_messages(task_id, messages):
    """批量执行消息 → append_event_archive_log（M1 落档，脱敏）"""
    task = get_task(task_id)
    if not task:
        return
    if task.get("event_seq"):
        try:
            from src.api.event_archive import append_event_archive_log
            tokens = [task.get("repo_gitcode_token") or ""]
            hint = task.get("issue_id") or ""
            for m in messages or []:
                if isinstance(m, dict):
                    append_event_archive_log(task["repo_key"], task["event_seq"],
                                             m.get("type", "log"), m, tokens=tokens,
                                             issue_num_hint=hint)
        except Exception as e:
            log.warning(f"report_messages archive failed: {e}")


def report_progress(task_id, progress):
    # 阶段进度：写 result_text 前缀（保持轻量，完整进度落 agent_tasks.result_text）
    task = get_task(task_id)
    if not task:
        return
    p = progress or {}
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET result_text=? WHERE id=?", (str(p.get("msg", "")), task_id))
        db.commit()


def complete_task(task_id, result_text, exit_code=0, usage=None):
    """完成：解析结构化 output → finalize_event_archive → update_event_complete → 终态。"""
    task = get_task(task_id)
    if task and task.get("event_seq"):
        try:
            from src.api.event_store import parse_agent_output_json, update_event_complete
            from src.api.event_archive import finalize_event_archive
            output = parse_agent_output_json(result_text or "")
            status = "completed" if exit_code == 0 else "failed"
            duration = None
            try:
                s = datetime.fromisoformat(task["started_at"]) if task.get("started_at") else None
                if s:
                    duration = round((datetime.now(timezone.utc) - s).total_seconds(), 1)
            except Exception:
                pass
            finalize_event_archive(task["repo_key"], task["event_seq"], status,
                                   exit_code, duration, output,
                                   tokens=[task.get("repo_gitcode_token") or ""],
                                   issue_num_hint=task.get("issue_id") or "")
            _conclusion = ""
            if output and output.get("conclusion"):
                _conclusion = output["conclusion"]
            update_event_complete(task["event_seq"], status, exit_code=exit_code,
                                  duration=duration, result=_conclusion or (result_text or "")[:200])
        except Exception as e:
            log.warning(f"complete_task finalize failed: {e}")
    update_task_status(task_id, "completed" if exit_code == 0 else "failed",
                       exit_code=exit_code, result_text=result_text or "",
                       usage_json=json.dumps(usage or {}))
    log.info(f"Task completed: {task_id} exit={exit_code}")


def fail_task(task_id, error, exit_code=-1):
    task = get_task(task_id)
    if task and task.get("event_seq"):
        try:
            from src.api.event_store import update_event_complete
            from src.api.event_archive import finalize_event_archive
            finalize_event_archive(task["repo_key"], task["event_seq"], "failed",
                                   exit_code, None, None,
                                   tokens=[task.get("repo_gitcode_token") or ""],
                                   issue_num_hint=task.get("issue_id") or "")
            update_event_complete(task["event_seq"], "failed", exit_code=exit_code,
                                  result=(error or "")[:200])
        except Exception as e:
            log.warning(f"fail_task finalize failed: {e}")
    update_task_status(task_id, "failed", exit_code=exit_code,
                       errors=json.dumps([str(error)] if error else []))
    log.warning(f"Task failed: {task_id} exit={exit_code} err={str(error)[:120]}")


def _run_task(task: dict):
    """worker 线程：stream_runtime → 回报。技能落地 + 占位符替换（skill-delivery-remote.md §3.5）。"""
    import os as _os
    cwd = task.get("cwd") or _os.getcwd()
    env = {}
    try:
        env = json.loads(task.get("env_extra") or "{}")
    except Exception:
        pass
    prompt = task.get("prompt") or ""
    skill_dir = _os.path.join(cwd, ".skills")
    prompt = prompt.replace("{{SKILL_DIR}}", skill_dir).replace("{{SERVER_URL}}", task.get("server_url") or "")
    # 技能落地
    try:
        skill_files = json.loads(task.get("skill_files") or "{}")
        for _sn, files in skill_files.items():
            for rel, content in (files or {}).items():
                dest = _os.path.join(skill_dir, _sn, rel)
                _os.makedirs(_os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(content)
        if skill_files:
            env["DEVOPS_SKILLS_DIR"] = skill_dir
    except Exception as e:
        log.warning(f"skill materialize failed: {e}")
    try:
        from src.runtime.executor import stream_runtime
        from src.agent.executor import stream_agent
        done = None
        messages_batch = []
        for evt_type, data in stream_runtime(
            prompt, cwd, session_id=task.get("runtime_session_id") or "",
            event_type=task.get("event_type") or "",
            backend_name="opencode",
            account=task.get("account") or "", user_id=task.get("user_id") or 0,
            env_extra=env, bot_token=task.get("repo_gitcode_token") or "",
        ):
            if evt_type == "done":
                done = data
                continue
            messages_batch.append({"type": evt_type, "data": data})
            if len(messages_batch) >= 50:
                report_messages(task["id"], messages_batch)
                messages_batch = []
        if messages_batch:
            report_messages(task["id"], messages_batch)
        if done:
            complete_task(task["id"], done.get("text", ""), done.get("exit_code", 0),
                          usage={"errors": done.get("errors", []), "stderr": done.get("stderr", "")})
        else:
            fail_task(task["id"], "no done event")
    except Exception as e:
        log.warning(f"worker _run_task failed: {e}")
        fail_task(task["id"], str(e))


def start_local_worker_loop():
    """（T2 进程内 worker）从队列消费。供 queue_local 模式调用；M1 阶段由测试/调用方按需驱动。"""
    pass


def submit_local(task: dict):
    _worker_pool.submit(_run_task, task)


# ===== 回报异步化（M2，见 concurrency-design.md §2.4）=====
# 供 M3 /api/runtime/* 回报端点使用：入队即返回，后台攒批写库，避免阻塞 event loop。
# queue_local 本地 worker 仍走同步 report_*（在 worker 线程内，无 event loop 阻塞）。

def start_report_ingestion_loop(loop=None):
    """启动回报异步落库循环（FastAPI lifespan 调用，见 main.py）。"""
    try:
        import asyncio
        _loop = loop or asyncio.get_running_loop()
        _task = _loop.create_task(_ingestion_loop())
        return _task
    except Exception as e:
        log.warning(f"report ingestion loop not started (sync fallback): {e}")
        return None


async def _ingestion_loop():
    """攒批落库：messages/progress/usage 入队，批量写。complete/fail 仍同步（强一致）。"""
    import asyncio
    _report_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
    # 模块级队列引用（供 async 端点 put）
    global _ASYNC_REPORT_QUEUE
    _ASYNC_REPORT_QUEUE = _report_queue
    while True:
        try:
            batch = [await _report_queue.get()]
            while len(batch) < 50:
                try:
                    batch.append(await asyncio.wait_for(_report_queue.get(), timeout=0.2))
                except asyncio.TimeoutError:
                    break
            with db_lock:
                for kind, tid, payload in batch:
                    try:
                        if kind == "messages":
                            report_messages(tid, payload)
                        elif kind == "progress":
                            report_progress(tid, payload)
                    except Exception as e:
                        log.warning(f"ingestion apply failed: {e}")
                db.commit()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"ingestion loop error: {e}")
            await asyncio.sleep(1)


_ASYNC_REPORT_QUEUE = None


def enqueue_async_report(kind: str, task_id: str, payload):
    """异步回报入队（队列满降级同步，不丢数据）。"""
    q = _ASYNC_REPORT_QUEUE
    if q is None:
        # 队列未启动 → 同步兜底
        if kind == "messages":
            report_messages(task_id, payload)
        elif kind == "progress":
            report_progress(task_id, payload)
        return
    try:
        q.put_nowait((kind, task_id, payload))
    except Exception:
        # 队列满：同步降级
        if kind == "messages":
            report_messages(task_id, payload)
        elif kind == "progress":
            report_progress(task_id, payload)
