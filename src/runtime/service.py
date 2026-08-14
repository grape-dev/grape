"""服务端 RuntimeService：任务队列（确定路由 + 串行槽）与本地 worker 执行。

见 agent-runtime-event-tech-impl.md §1.3 / concurrency-design.md §2.2-2.4。
执行路径：业务层 enqueue_task → agent_tasks(status=queued) → claim（原子抢单+D9）
→ 本地线程池 stream_runtime → report/complete/fail。
"""
import os, json, re, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from src.session_manager import db, db_lock
from src.config import log

# ===== 执行模式开关（M2，见 implementation-plan §三 / concurrency-design.md）=====
# auto（按绑定引擎宿主自动推导，见 execution-mode-auto-routing-design.md §3）
# / direct（就地 stream_agent）/ queue_local（入队 + 本地 worker）/ queue_remote（远端 RuntimeAgent）
EXEC_MODE_AUTO = "auto"
EXEC_MODE_DIRECT = "direct"
EXEC_MODE_LOCAL = "queue_local"
EXEC_MODE_REMOTE = "queue_remote"

_DEFAULT_MODE = EXEC_MODE_AUTO


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
    """设置执行模式（落 meta 表，运维可改键值；读取实时生效，无需重启）。"""
    try:
        from src.user.db import set_meta_value
        set_meta_value("execution_mode", mode)
        log.info(f"execution_mode set to {mode}")
    except Exception as e:
        log.warning(f"set_execution_mode failed: {e}")


# ===== 按绑定引擎宿主自动推导执行通道（execution-mode-auto-routing-design.md §3.1/§3.6）=====

def runtime_credential_id(runtime_id: int):
    """按 runtime_id 反查引擎宿主 credential_id；查不到返回 None。

    credential_id=0 → 本机引擎（服务器装着该 CLI）；>0 → 远端 worker 承载的引擎。
    """
    if not runtime_id:
        return None
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT credential_id FROM agent_runtimes WHERE id=?", (runtime_id,))
            row = c.fetchone()
        return (row[0] or 0) if row else None
    except Exception:
        return None


_RUNTIME_STALE_SECONDS = int(os.environ.get("RUNTIME_STALE_SECONDS", "300"))


def _remote_runtime_ready(runtime_id: int) -> bool:
    """远端引擎是否可用：引擎 status=online、last_seen 新鲜、凭据未吊销。

    心跳间隔 15s，失联 > _RUNTIME_STALE_SECONDS 视为不可用（防 auto 模式任务卡 queued）。
    """
    try:
        from src.runtime.health import parse_dt
        now = time.time()
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status, last_seen_at, credential_id FROM agent_runtimes WHERE id=?", (runtime_id,))
            row = c.fetchone()
        if not row:
            return False
        engine_status, last_seen_at, cid = row
        if (engine_status or "") != "online":
            return False
        ts = parse_dt(last_seen_at)
        if ts is not None and now - ts > _RUNTIME_STALE_SECONDS:
            return False
        if cid:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT status FROM runtime_credentials WHERE id=?", (cid,))
                cred = c.fetchone()
            if cred and cred[0] != "active":
                return False
        return True
    except Exception as e:
        log.warning(f"runtime_execution_kind: health check failed: {e}")
        return False


def runtime_execution_kind(runtime_id: int) -> str:
    """返回该任务的有效执行通道（auto 语义下的推导 / 强制模式直接生效）：

    - 全局显式 direct/queue_local/queue_remote → 直接返回该强制值（向后兼容）
    - auto 下按绑定引擎宿主推导：
      - credential_id == 0  → 本机引擎（服务器装着的 CLI）        → EXEC_MODE_DIRECT
      - credential_id >  0  → 远端 worker 承载的引擎             → EXEC_MODE_REMOTE
      - runtime_id=0 / 查不到（未绑定）→ EXEC_MODE_DIRECT（本机默认，见 §3.6）
    - auto 下远端引擎失联/凭据吊销 → 降级本机 direct 兜底（避免任务卡 queued）
    """
    mode = get_execution_mode()
    if mode != EXEC_MODE_AUTO:
        return mode
    cid = runtime_credential_id(runtime_id)
    if cid is None:
        return EXEC_MODE_DIRECT
    if cid > 0:
        if _remote_runtime_ready(runtime_id):
            return EXEC_MODE_REMOTE
        log.warning(f"runtime_execution_kind: runtime_id={runtime_id} 远端引擎不可用，降级本机 direct")
        return EXEC_MODE_DIRECT
    return EXEC_MODE_DIRECT

# ===== 本地 worker 执行池（有界，替代无界 threading.Thread，见 concurrency-design.md §2.2）=====
_DEFAULT_MAX_CONCURRENT = int(os.environ.get("RUNTIME_MAX_CONCURRENT_TASKS", "20"))
_worker_pool = ThreadPoolExecutor(max_workers=_DEFAULT_MAX_CONCURRENT, thread_name_prefix="rt-worker")

_TASK_COLUMNS = (
    "id, agent_session_id, runtime_id, event_seq, issue_id, repo_key, agent_type, skill, "
    "prompt, cwd, runtime_session_id, event_type, account, user_id, repo_gitcode_token, "
    "env_extra, skill_files, server_url, issue_session_id, project_info_files, project_info_meta, "
    "knowledge_files, attempts, pool_agent_type, affinity_runtime_id, "
    "status, exit_code, result_text, errors, stderr, "
    "usage_json, started_at, completed_at, created_at"
)

_TASK_COLUMNS_LIST = [c.strip() for c in _TASK_COLUMNS.split(",")]


def _row_to_dict(row):
    return dict(zip(_TASK_COLUMNS_LIST, row))


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


_PROJECT_INFO_FILES = ("project-graph.md", "project-capabilities.md")


def load_project_info(repo_key: str) -> tuple[dict, dict]:
    """读取服务端权威 project-info（见 multi-issue-isolation-design.md §2.4）。

    返回 (files, meta)：
    - files: {rel_path: content}（project-graph.md / project-capabilities.md / .metadata.json）
    - meta: {last_commit_sha, branches, last_updated, degraded}
    无基线 → (files={}, meta={})。
    """
    if not repo_key:
        return {}, {}
    try:
        from src.config import WORKSPACE_DIR
        from src.repo_key import split_owner_repo
        owner, rname = split_owner_repo(repo_key)
        pi = os.path.join(WORKSPACE_DIR, owner, rname, "project-info")
        if not os.path.isdir(pi):
            return {}, {}
        files = {}
        for rel in _PROJECT_INFO_FILES:
            p = os.path.join(pi, rel)
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        files[rel] = f.read()
                except Exception:
                    pass
        meta = {}
        mp = os.path.join(pi, ".metadata.json")
        if os.path.isfile(mp):
            try:
                with open(mp, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
        if files:
            files[".metadata.json"] = json.dumps(meta, ensure_ascii=False)
        return files, meta
    except Exception as e:
        log.warning(f"load_project_info failed for {repo_key}: {e}")
        return {}, {}


def enqueue_task(*, agent_session_id, runtime_id, prompt, cwd="",
                 runtime_session_id="", event_type="", account="", user_id=0,
                 repo_gitcode_token="", env_extra=None,
                 event_seq=None, issue_id="", repo_key="", agent_type="",
                 skill="", skill_path="", server_url="", issue_session_id="",
                 project_info_files=None, project_info_meta=None, knowledge_files=None,
                 pool_agent_type="", affinity_runtime_id=0):
    """业务层入队：确定路由（D3）+ 快照 + 技能打包 + 凭据注入。返回 task_id。

    引擎池（agent-cluster-design.md §4.1/§4.2）：runtime_id=0 + pool_agent_type 非空
    → 池调度任务，由池内任意空闲引擎 claim 时领取（claim 会把 runtime_id 定为实际引擎）。
    显式 runtime_id（>0）→ 确定路由，行为与现状一致。

    knowledge_files：共享知识快照（event-conversation-design.md §3.1.1），None 时自动打包；
    远端 worker 侧 materialize 到 cwd/.knowledge 并替换 {{KNOWLEDGE_DIR}}。
    """
    if not agent_session_id:
        raise ValueError("agent_session_id is required")
    if not runtime_id and not pool_agent_type:
        raise ValueError("runtime_id or pool_agent_type is required (确定路由 D3)")
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
    # project-info 随任务（服务端权威，多 VM 共享，见 multi-issue-isolation-design.md §2.4）
    if project_info_files is None:
        _pif, _pim = load_project_info(repo_key)
        project_info_files = _pif
        project_info_meta = _pim
    # 共享知识快照（仅任务作用域：issue/issue_agent/project 三级，随任务交付远端 worker）
    if knowledge_files is None:
        try:
            from src.api.knowledge import knowledge_files as _know_files
            knowledge_files = _know_files(repo_key, agent_type=agent_type, issue_num=_issue_num(issue_id))
        except Exception as e:
            log.warning(f"enqueue: knowledge pack failed for {repo_key}: {e}")
            knowledge_files = {}
    env = dict(env_extra or {})
    for k, v in _collect_server_env(user_id, account).items():
        env.setdefault(k, v)
    _server_url = server_url or ""
    if not _server_url:
        try:
            from src.user.db import get_server_base_url
            _server_url = get_server_base_url()
        except Exception:
            pass
    with db_lock:
        c = db.cursor()
        c.execute(
            f"INSERT INTO agent_tasks ({_TASK_COLUMNS}) VALUES "
            f"({','.join(['?'] * 34)})",
            (task_id, agent_session_id, runtime_id, event_seq, issue_id or "", repo_key or "",
             agent_type or "", skill or "", prompt, cwd or "", runtime_session_id or "",
             event_type or "", account or "", user_id or 0, repo_gitcode_token or "",
             json.dumps(env), json.dumps(skill_files), _server_url,
             issue_session_id or "", json.dumps(project_info_files), json.dumps(project_info_meta),
             json.dumps(knowledge_files),
             0, pool_agent_type or "", affinity_runtime_id or 0,
             "queued", None, "", "", "", "{}", None, None, datetime.now(timezone.utc).isoformat())
        )
        db.commit()
    from src.runtime.instances import runtime_instance_name
    _inst = runtime_instance_name(runtime_id)
    log.info(f"Task enqueued: {task_id} session={agent_session_id} runtime={runtime_id}({_inst}) skill={skill}")
    return task_id


# ===== 引擎池绑定（agent-cluster-design.md §4.1）=====

def set_agent_pool(agent_type: str, runtime_ids: list[int], weight: int = 1, priority: int = 5):
    """替换某 Agent 类型的引擎池绑定（先删后插，单事务）。runtime_ids 为空列表 → 清空池（回到兜底链）。"""
    if not agent_type:
        raise ValueError("agent_type is required")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_type_runtime_pool WHERE agent_type=?", (agent_type,))
        for rid in (runtime_ids or []):
            c.execute(
                "INSERT OR IGNORE INTO agent_type_runtime_pool (agent_type, runtime_id, weight, priority) VALUES (?,?,?,?)",
                (agent_type, int(rid), int(weight), int(priority)))
        db.commit()
    log.info(f"Agent pool set: {agent_type} -> {runtime_ids or []} (weight={weight}, priority={priority})")


def get_agent_pool(agent_type: str) -> list[dict]:
    """返回某 Agent 类型的引擎池绑定（含引擎实时状态，供管理端/调度展示）。"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT p.runtime_id, p.weight, p.priority, p.enabled, p.remark, "
                "       r.name, r.status, r.healthy, r.busy_slots, r.max_concurrent, r.version, "
                "       r.credential_id, COALESCE(rc.device_name, ''), COALESCE(rc.last_ip, '') "
                "FROM agent_type_runtime_pool p "
                "LEFT JOIN agent_runtimes r ON r.id = p.runtime_id "
                "LEFT JOIN runtime_credentials rc ON rc.id = r.credential_id "
                "WHERE p.agent_type=? ORDER BY p.priority ASC, p.id ASC",
                (agent_type,))
            rows = c.fetchall()
        except Exception as e:
            log.warning(f"get_agent_pool failed: {e}")
            return []
    return [{
        "runtime_id": r[0], "weight": r[1], "priority": r[2], "enabled": bool(r[3]),
        "remark": r[4] or "", "name": r[5] or "", "status": r[6] or "offline",
        "healthy": bool(r[7]), "busy_slots": r[8] or 0, "max_concurrent": r[9] or 0,
        "version": r[10] or "", "credential_id": r[11] or 0,
        "device_name": r[12] or "", "last_ip": r[13] or "",
    } for r in rows]


def pool_runtime_ids(agent_type: str) -> list[int]:
    """池内 enabled 的引擎 id 列表（claim 池调度用）。"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT runtime_id FROM agent_type_runtime_pool WHERE agent_type=? AND enabled=1", (agent_type,))
            return [r[0] for r in c.fetchall()]
        except Exception:
            return []


def _issue_num(issue_id) -> int:
    """'#5' / '5' / 5 → 5；无法解析返回 0。"""
    if not issue_id:
        return 0
    m = re.search(r"(\d+)", str(issue_id))
    return int(m.group(1)) if m else 0


def claim(runtime_id) -> dict | None:
    """原子抢单 + 串行槽（见 multi-issue-isolation-design.md §3.3 / concurrency-design.md §2.3）。

    串行粒度（M2）：任务带 issue_session_id → 同 Issue 严格串行（跨 Issue 并行）；
    issue_session_id 为空（旧任务/#0 事件）→ 回退按 agent_session_id 串行（现状语义）。

    引擎池（agent-cluster-design.md §4.2）：本引擎属于某 Agent 池时，除自己的队列外，
    还可领取该池的未定任务（runtime_id=0 + pool_agent_type 匹配，领取后 runtime_id 定为
    本引擎）。affinity_runtime_id 命中本引擎的任务优先（会话亲和，§4.5）。
    """
    with db_lock:
        c = db.cursor()
        try:
            # ---- 兜底回收（M1：dispatched 超时自动重排，防 worker 丢弃任务死循环）----
            # worker 端 max_concurrent 满时 claim 后可能丢弃任务（不 start 不释放），
            # 导致 dispatched 任务永久卡死、busy_slots 泄漏。这里在每次 claim 前
            # 将超时(120s)未 start 的任务回池重排；attempts>=3 直接置 failed。
            try:
                _stale = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
                c.execute(
                    "SELECT id, runtime_id, event_seq FROM agent_tasks "
                    "WHERE status='dispatched' AND started_at < ? AND started_at IS NOT NULL",
                    (_stale,))
                _stale_rows = c.fetchall()
                for _tid, _rid, _seq in _stale_rows:
                    c.execute(
                        "UPDATE agent_tasks SET attempts=attempts+1, "
                        "status=CASE WHEN attempts+1 >= 3 THEN 'failed' ELSE 'queued' END, "
                        "runtime_id=CASE WHEN attempts+1 >= 3 THEN runtime_id ELSE 0 END, "
                        "started_at=NULL, "
                        "result_text=CASE WHEN attempts+1 >= 3 THEN '回收超限(dispatched 超时 3 次)' ELSE result_text END "
                        "WHERE id=? AND status='dispatched'",
                        (_tid,))
                    if c.rowcount and _rid:
                        c.execute(
                            "UPDATE agent_runtimes SET busy_slots = CASE WHEN busy_slots > 0 THEN busy_slots - 1 ELSE 0 END WHERE id=?",
                            (_rid,))
                    # 回收置 failed（attempts 超限）→ 同步事件终态，避免事件永远 running
                    if c.rowcount and _seq:
                        c.execute("SELECT status FROM agent_tasks WHERE id=?", (_tid,))
                        _after = c.fetchone()
                        if _after and _after[0] == "failed":
                            try:
                                from src.api.event_store import update_event_status
                                update_event_status(_seq, "failed")
                            except Exception:
                                pass
                if _stale_rows:
                    log.info(f"recover stale dispatched: {len(_stale_rows)} tasks")
            except Exception as _e:
                log.warning(f"recover stale dispatched failed: {_e}")

            # 该 runtime 所属引擎池（enabled 才参与）
            pools = []
            try:
                c.execute(
                    "SELECT agent_type FROM agent_type_runtime_pool WHERE runtime_id=? AND enabled=1",
                    (runtime_id,))
                pools = [r[0] for r in c.fetchall()]
            except Exception:
                pass
            if pools:
                # MySQL 兼容抢单：拆两步——先 SELECT 候选（含串行槽 NOT EXISTS），再 UPDATE 该 id
                ph = ",".join(["?"] * len(pools))
                c.execute(f"""
                    SELECT id FROM agent_tasks
                    WHERE status='queued'
                      AND (runtime_id=? OR (runtime_id=0 AND pool_agent_type IN ({ph})))
                      AND NOT EXISTS (
                        SELECT 1 FROM agent_tasks a2
                        WHERE a2.status IN ('dispatched','running')
                          AND (
                            (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)
                            OR
                            (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)
                          )
                      )
                    ORDER BY CASE WHEN affinity_runtime_id = ? THEN 0 ELSE 1 END, created_at ASC LIMIT 1
                """, (runtime_id, *pools, runtime_id))
            else:
                c.execute("""
                    SELECT id FROM agent_tasks
                    WHERE runtime_id=? AND status='queued'
                      AND NOT EXISTS (
                        SELECT 1 FROM agent_tasks a2
                        WHERE a2.status IN ('dispatched','running')
                          AND (
                            (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)
                            OR
                            (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)
                          )
                      )
                    ORDER BY created_at ASC LIMIT 1
                """, (runtime_id,))
            row = c.fetchone()
            if not row:
                db.commit()
                return None
            task_id = row[0]
            c.execute(
                "UPDATE agent_tasks SET status='dispatched', runtime_id=?, started_at=? WHERE id=? AND status='queued'",
                (runtime_id, datetime.now(timezone.utc).isoformat(), task_id))
            claimed = c.rowcount > 0
            if claimed:
                # 槽位 +1（观测/池调度排序用；不硬限流，边端 RUNTIME_MAX_CONCURRENT_TASKS 仍控制执行并发）
                c.execute("UPDATE agent_runtimes SET busy_slots=busy_slots+1 WHERE id=?", (runtime_id,))
            db.commit()
        except Exception as e:
            log.warning(f"claim failed: {e}")
            db.rollback()
            return None
        if not claimed:
            return None
        task = get_task(task_id)
        if task:
            task["status"] = "dispatched"
        from src.runtime.instances import runtime_instance_name
        log.info(f"claim: runtime_id={runtime_id}({runtime_instance_name(runtime_id)}) task={task_id}")
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
        c.execute("SELECT status, runtime_id FROM agent_tasks WHERE id=?", (task_id,))
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
        # 槽位释放（agent-cluster-design.md §4.2.1）：非终态 → 终态，且任务已绑定引擎 → busy_slots-1
        if status in ("completed", "failed", "cancelled") and row[1] and row[1] > 0:
            c.execute(
                "UPDATE agent_runtimes SET busy_slots = CASE WHEN busy_slots > 0 THEN busy_slots - 1 ELSE 0 END WHERE id=?",
                (row[1],))
        db.commit()


def cancel_task(task_id):
    update_task_status(task_id, "cancelled")


def requeue_task(task_id):
    """将 dispatched/running 任务重置回 queued（客户端并发冲突跳过时回滚，避免任务悬空卡死）。

    见 seq#622 教训：客户端 _run_loop 因并发槽冲突 skip 任务后，服务端任务仍停留 dispatched，
    既不被执行也不重新排队 → 永久卡死。客户端冲突时应调本函数回滚。
    """
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET status='queued', started_at=NULL WHERE id=? AND status IN ('dispatched','running')",
                  (task_id,))
        db.commit()
        return c.rowcount > 0


def report_messages(task_id, messages):
    """批量执行消息 → append_event_archive_log（M1 落档，脱敏）"""
    task = get_task(task_id)
    if not task:
        return
    # 逐步日志前缀：task + 非空的 seq/issue/agent/repo，与 worker 侧（agent.py _esq）对齐便于定位
    _esq_parts = [f"seq={task.get('event_seq') or '?'}"]
    _issue_id = (task.get("issue_id") or "").strip().lstrip("#")
    if _issue_id and _issue_id != "0":
        _esq_parts.append(f"issue:#{_issue_id}")
    _agent_type = task.get("agent_type") or ""
    if _agent_type:
        _esq_parts.append(f"agent:{_agent_type}")
    _repo_key = task.get("repo_key") or ""
    if _repo_key:
        _esq_parts.append(f"repo:{_repo_key}")
    _esq = "|".join(_esq_parts)
    # 逐步日志（服务端观察：step_start/step_finish 等；远端执行时 step 详情在 worker 日志）
    for m in (messages or []):
        if not isinstance(m, dict):
            continue
        _t = m.get("type")
        if _t in ("step_start", "step_finish", "text", "reasoning", "tool_use", "error"):
            _d = m.get("data")
            log.info(f"[task {task_id}] [{_esq}] {_t}: {str(_d)[:120]}")
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
    # 新会话续接指针回写（Issue 级优先，repo 级兼容，见 multi-issue-isolation-design.md §3.3/§3.5）
    _new_sid = (usage or {}).get("session_id") or ""
    if _new_sid:
        try:
            from src.api.session_store import update_issue_session
            if task and task.get("issue_session_id"):
                update_issue_session(task["issue_session_id"], runtime_session_id=_new_sid)
        except Exception as e:
            log.warning(f"complete_task update issue session failed: {e}")
        try:
            with db_lock:
                c = db.cursor()
                if task and task.get("agent_session_id"):
                    c.execute("UPDATE devops_agent_session SET runtime_session_id=?, last_active_at=datetime('now') WHERE id=?",
                              (_new_sid, task["agent_session_id"]))
                    db.commit()
        except Exception as e:
            log.warning(f"complete_task update repo session failed: {e}")
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
    """worker 线程：stream_runtime → 回报。技能 + project-info 落地 + 占位符替换（skill-delivery-remote.md §3.5 / multi-issue-isolation-design.md §2.4）。"""
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
    # project-info 落地（repo 全局共享，多 VM 一致；见 multi-issue-isolation-design.md §2.4）
    proj_dir = _os.path.join(cwd, ".project-info")
    try:
        proj_files = json.loads(task.get("project_info_files") or "{}")
        for rel, content in (proj_files or {}).items():
            if rel == ".metadata.json":
                continue
            dest = _os.path.join(proj_dir, rel)
            _os.makedirs(_os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content)
        prompt = prompt.replace("{{PROJECT_INFO_DIR}}", proj_dir)
    except Exception as e:
        log.warning(f"project-info materialize failed: {e}")
    # 共享知识落地（event-conversation-design.md §3.1.1；随任务交付远端 worker）
    know_dir = _os.path.join(cwd, ".knowledge")
    try:
        know_files = json.loads(task.get("knowledge_files") or "{}")
        for rel, content in (know_files or {}).items():
            dest = _os.path.join(know_dir, rel)
            _os.makedirs(_os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content)
        prompt = prompt.replace("{{KNOWLEDGE_DIR}}", know_dir)
    except Exception as e:
        log.warning(f"knowledge materialize failed: {e}")
    # git worktree（coding/qa/sre，X 工作模型，见 multi-issue-isolation-design.md §3.4.1）
    try:
        from src.runtime import repogit
        _num = repogit.issue_num_from(task.get("issue_id") or "")
        if repogit.is_git_agent(task.get("agent_type") or "") and _num and task.get("issue_session_id"):
            _wt = repogit.ensure_issue_worktree(task.get("repo_key") or "", task.get("agent_type") or "",
                                                _num, token=task.get("repo_gitcode_token") or "")
            if _wt:
                env["GIT_ASKPASS"] = _os.path.join(_wt, ".git-askpass.sh")
    except Exception as e:
        log.warning(f"git worktree ensure failed: {e}")
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
                          usage={"errors": done.get("errors", []), "stderr": done.get("stderr", ""),
                                 "session_id": done.get("oc_session_id", "")})
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


async def wait_task_terminal(task_id: str, timeout: float = 1800.0, poll_interval: float = 3.0) -> dict:
    """异步轮询任务直到终态（completed/failed/cancelled）。

    供 IM / web 等"入队 + 等结果"路径使用（见 multi-runtime-instance-design 配套）。
    超时抛 TimeoutError。返回终态 task dict（含 result_text / exit_code / usage_json.session_id）。
    """
    import asyncio, time as _time
    deadline = _time.monotonic() + timeout
    while True:
        t = get_task(task_id)
        if t and t.get("status") in ("completed", "failed", "cancelled"):
            return t
        if _time.monotonic() > deadline:
            raise TimeoutError(f"task {task_id} not finished within {timeout}s")
        await asyncio.sleep(poll_interval)
