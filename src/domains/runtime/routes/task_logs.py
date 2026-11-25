"""Task 运行日志路由：SSE 实时流 + 增量拉取（实时日志功能，对齐 multica TaskMessage 广播模型）。

- GET /api/task-logs/{task_id}/stream?since=<seq> — SSE 实时流（先发存量，再订阅 fanout）
- GET /api/task-logs/{task_id}/messages?since=<seq> — 增量拉取（断线/轮询兜底）

鉴权：登录用户（Bearer token）。前端用 fetch + ReadableStream 消费（同 chat SSE 模式）。
"""

import asyncio

import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from fastapi.responses import StreamingResponse

from src.api.deps import require_no_session_token

from src.kernel.config import log

from src.domains.runtime.engine.service import list_task_messages, list_wrote_files

router = APIRouter(prefix="/api/task-logs", tags=["task-logs"])

# 无消息时的心跳间隔：防代理/浏览器超时断连
_HEARTBEAT_S = 15

# DB 增量尾随周期：跨 master 场景下，worker 上报落在另一台 master（fanout 不互通），
# 本进程 SSE 每 _TAIL_POLL_S 秒查一次 task_messages（共享 DB）补发增量。
# 延迟 = 尾随周期 + ingestion 批处理（~0.2s），1.5s 可接受；环境变量可调。
_TAIL_POLL_S = float(os.getenv("TASK_LOG_TAIL_POLL_S", "1.5"))

def _require_task_exists(task_id: str):
    from src.domains.runtime.engine.service import get_task
    if not get_task(task_id):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})

_WROTE_FILES_SCHEMA = {
    "type": "object",
    "properties": {
        "agent_name": {"type": "string"},
        "agent_version": {"type": "string"},
        "device_name": {"type": "string"},
        "last_ip": {"type": "string"},
        "owner_name": {"type": "string"},
        "prompt": {"type": "string"},
        "prompt_full_length": {"type": "integer"},
        "created_at": {"type": "string"},
        "started_at": {"type": "string"},
        "completed_at": {"type": "string"},
        "status": {"type": "string"},
        "issue_id": {"type": "string"},
        "event_seq": {"type": "integer"},
        "repo_key": {"type": "string"},
        "spec": {"type": "string"},
        "cpu_percent": {"type": "number"},
        "memory_percent": {"type": "number"},
        "disk_percent": {"type": "number"},
        "load_avg": {"type": "string"},
        "wrote_files": {
            "type": "array",
            "description": "任务写过的文件清单（派生自 task_messages 的 write/patch tool_use 入参，不扫工作区；无数据为 []）",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "bytes": {"type": "integer", "description": "写入内容的 utf-8 字节数（patch 取 new_string）"},
                    "tool": {"type": "string"},
                    "seq": {"type": "integer"},
                    "first_seen_at": {"type": "string"},
                },
            },
        },
    },
}

@router.get("/{task_id}/info", operation_id="task_logs_info", summary="Task runtime info", description="Task runtime info: agent/device/machine + received context (prompt).")
def get_task_runtime_info(task_id: str, user: dict = Depends(require_no_session_token)):
    """任务关联的执行引擎/机器信息 + 接收上下文（prompt）。

    返回：{agent_name, agent_version, device_name, last_ip, owner_name, prompt, prompt_full_length,
           created_at, started_at, completed_at, status, issue_id, event_seq, repo_key}
    agent_runtimes.name/version（引擎名+版本号）、runtime_credentials.name/last_ip/owner_name。
    Issue #701：device_name 字段下发凭据自定义名 rc.name（空则空值，前端统一占位），
    不再下发边端自报 device_name。
    prompt 为 agent 接收到的完整上下文（截断至 8000 字符），prompt_full_length 为原始长度。
    """
    from src.kernel.session_manager import db_lock
    from src.api.time_utils import normalize_event_time
    # 三库模式：agent_tasks / agent_runtimes / runtime_credentials 三张表都属 runtime 域，
    # 而 `/api/task-logs/*` 路由挂在 mgmt 进程（nginx 把其余路径都发给 38081）——用进程主库
    # 会撞 `Table 'grape_stage_mgmt.agent_tasks' doesn't exist`（1146），事件详情页的
    # 「查看实时日志」整块 500。改走 runtime 域游标：本进程即 runtime 时用主库，
    # 三库跨进程时新建 runtime 连接，单库/测试拓扑回落进程库。
    from src.domains.runtime.engine.service import _rt_db as _task_db
    _require_task_access(task_id, user)  # 扫雷⑨-H5：任务归属校验
    with db_lock:
        c = _task_db.cursor()
        c.execute(
            """SELECT ar.name, ar.version, rc.name, rc.last_ip, rc.owner_name, t.prompt,
                      t.created_at, t.started_at, t.completed_at, t.status, t.issue_id, t.event_seq, t.repo_key,
                      rc.cpu_percent, rc.memory_percent, rc.disk_percent, rc.load_avg, rc.spec, t.runtime_id,
                      COALESCE(rc.credential_uid,'')
               FROM agent_tasks t
               LEFT JOIN agent_runtimes ar ON ar.id = t.runtime_id
               LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id
               WHERE t.id = ?""",
            (task_id,),
        )
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    agent_name, agent_version, device_name, last_ip, owner_name, prompt = row[:6]
    created_at, started_at, completed_at, status, issue_id, event_seq, repo_key = row[6:13]
    cpu_pct, mem_pct, disk_pct, load_avg, spec = row[13:18] if len(row) > 17 else (0, 0, 0, "", "")
    runtime_id = row[18] if len(row) > 18 else 0
    # [spec 2026-11-14 cred_uid8] 凭据稳定键前 8 位——沙箱 worker 的 systemd 服务名/安装目录单锚
    # （install-worker.sh:112-113 sandbox-worker-<cred_uid8>）。credential_id 不下发（跨主控撞号，
    # 且服务名不按 id 命名——spec 2026-11-14-eventview-cred-uid8-fix 拍板①）。
    _cred_uid8 = ((row[19] or "") if len(row) > 19 else "")[:8]
    # 云引擎四段名派生 provider（与 dashboard.py:1789 同口径，[沙箱迁移 i7]）；普通引擎为空
    _sandbox_provider = (agent_name or "").split(":")[2] if (agent_name or "").count(":") >= 3 else ""
    # Issue #1255：执行模型（用量优先、配置模型回落，与事件详情同一取值函数，口径一致）
    try:
        from src.domains.insight.public import task_model_summary
        model_summary = task_model_summary(task_id, int(runtime_id or 0))
    except Exception:
        model_summary = {"model": "", "source": "unknown", "models": []}
    import re as _re
    _m = _re.search(r"(\d+\.\d+\.\d+)", str(agent_version or ""))
    ver = _m.group(1) if _m else str(agent_version or "")
    # Issue #3818：任务详情回显同源脱敏——autopilot prompt 里的预签名 query / 口令
    # 不应通过执行详情接口再泄露一次；全文长度保留原始值，便于排查截断。
    from src.domains.runtime.engine.payload_guard import redact as _payload_redact
    _prompt_len = len(prompt or "")
    _prompt = _payload_redact(prompt or "")
    _PROMPT_CAP = 8000
    return {
        "agent_name": agent_name or "",
        "agent_version": ver,
        "device_name": device_name or "",
        "last_ip": last_ip or "",
        "owner_name": owner_name or "",
        "prompt": _prompt[:_PROMPT_CAP],
        "prompt_full_length": _prompt_len,
        "created_at": normalize_event_time(created_at),
        "started_at": normalize_event_time(started_at),
        "completed_at": normalize_event_time(completed_at),
        "status": status or "",
        "issue_id": issue_id or "",
        "event_seq": event_seq or 0,
        "repo_key": repo_key or "",
        "spec": spec or "",
        "cpu_percent": cpu_pct or 0,
        "memory_percent": mem_pct or 0,
        "disk_percent": disk_pct or 0,
        "load_avg": load_avg or "",
        # [spec 2026-11-14 cred_uid8] 沙箱 worker 服务名单锚（sandbox-worker-<cred_uid8>）+
        # 云引擎四段名 provider（[沙箱迁移 i7]，与 dashboard agent_engines 同口径）。
        # credential_id 不下发（spec 拍板①）；sandbox_id 本期不做（拍板②，🌐 位空置）。
        "credential_uid8": _cred_uid8,
        "sandbox_provider": _sandbox_provider,
        "wrote_files": list_wrote_files(task_id),
        "model": model_summary.get("model", ""),
        "model_source": model_summary.get("source", "unknown"),
        "models": model_summary.get("models", []),
    }

@router.get("/{task_id}/messages", operation_id="task_logs_messages", summary="Task messages", description="Incremental messages: everything after `since` (monotonic seq).")
def get_task_messages(task_id: str, user: dict = Depends(require_no_session_token),
                            since: int = Query(0, ge=0)):
    """增量拉取：since 之后的消息（seq 单调递增）。"""
    _require_task_access(task_id, user)  # 扫雷⑨-H5：任务归属校验（兼 404）
    return list_task_messages(task_id, since=since)

@router.get("/{task_id}/stream", operation_id="task_logs_stream", summary="Task message stream (SSE)", description="SSE live stream: buffered messages after `since`, then a live subscription with heartbeat.")
async def stream_task_messages(task_id: str, request: Request,
                               user: dict = Depends(require_no_session_token),
                               since: int = Query(0, ge=0)):
    """SSE 实时流：先发 since 之后存量 → 阻塞读订阅队列 → 心跳保活。

    跨 master 增量尾随：live 循环在订阅队列等待上加短超时 _TAIL_POLL_S，
    超时则查 task_messages（共享 DB）补发 since=last_seq 之后的增量——
    解决双主 fanout 不互通导致远端 worker 事件面板不刷新（#379）。
    """
    _require_task_access(task_id, user)  # 扫雷⑨-H5：任务归属校验（兼 404）
    from src.domains.runtime.engine.task_log_stream import subscribe, unsubscribe, sse_frame

    async def gen():
        # 1) 存量（断线/新开页 catch-up，seq 去重由前端 merge 保证）
        last_seq = since
        try:
            initial = list_task_messages(task_id, since=since)
            for m in initial:
                yield sse_frame(m)
            if initial:
                last_seq = initial[-1].get("seq", last_seq)
        except Exception as e:
            log.warning(f"task-log stream: catch-up failed: {e}")
        # 2) 实时订阅 + DB 增量尾随（跨 master 补发）
        q = await subscribe(task_id)
        idle = 0.0  # 累计无消息时间，达 _HEARTBEAT_S 发 keepalive
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=_TAIL_POLL_S)
                    yield sse_frame(payload)
                    _sq = payload.get("seq")
                    if isinstance(_sq, int) and _sq > last_seq:
                        last_seq = _sq
                    idle = 0.0
                except asyncio.TimeoutError:
                    # 尾随查询：补发另一台 master 落库的增量（共享 DB）
                    try:
                        tailed = list_task_messages(task_id, since=last_seq)
                        for m in tailed:
                            yield sse_frame(m)
                            _sq = m.get("seq")
                            if isinstance(_sq, int) and _sq > last_seq:
                                last_seq = _sq
                        if tailed:
                            idle = 0.0
                        else:
                            idle += _TAIL_POLL_S
                    except Exception as e:
                        log.warning(f"task-log stream: tail query failed: {e}")
                        idle += _TAIL_POLL_S
                    if idle >= _HEARTBEAT_S:
                        yield ": keepalive\n\n"
                        idle = 0.0
        finally:
            await unsubscribe(task_id, q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关 nginx 缓冲，保证流式实时
        },
    )

def _require_task_access(task_id: str, user: dict):
    """扫雷⑨-H5：任务归属校验（三端点共用）。

    - session_token / api_key：作用域已限定，与 check_repo_access 同口径豁免；
    - 本人：agent_tasks.user_id == 当前用户 id（任务创建者）；
    - 仓库管理方：任务带 repo_key 时，托管人（managed_repos.host_user_id）或
      user_repos.is_admin=1 才可读（**不是** check_repo_access 的“可见即读”）——
      外部 issue 提报者能看见仓库，但不能读日志与 diff；
    - 其余（非本人且非管理方/无 repo_key）→ 403 fail-closed。
    """
    from src.domains.runtime.engine.service import get_task
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    auth_type = user.get("auth_type", "jwt")
    if auth_type in ("session_token", "api_key"):
        return task
    if str(task.get("user_id") or "") == str(user.get("id") or ""):
        return task
    repo_key = task.get("repo_key") or ""
    if repo_key:
        # 收紧：能“看见”仓库（check_repo_access 口径，含外部 issue 提报者的
        # user_repos 可见行）不等于能读任务的日志与 diff。日志/代码泄漏面更大，
        # 因此非本人时只放行仓库管理方（托管人 / is_admin）；判定不出按无权（fail-closed）。
        from src.domains.mgmt import public as _mgmt

        try:
            _can_read = bool(_mgmt.user_can_manage_repo(user.get("id"), repo_key))
        except Exception as _e:  # noqa: BLE001 - 判定不出 = 无权（fail-closed）
            log.warning(f"task logs: manage check failed ({repo_key}): {_e}")
            _can_read = False
        if _can_read:
            return task
        raise HTTPException(status_code=403, detail={
            "code": 403, "error_code": "TASK_LOG_FORBIDDEN",
            "message": "仅任务本人或仓库管理员可读任务日志",
        })
    raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该任务"})
