"""Runtime 进程内部端点——仅供其他域 RemoteProvider 调用（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.7.

集群模式下挂载到 Runtime（Event/Worker）进程，提供 /internal/* 端点供
MgmtServiceRemote / InsightServiceRemote 调用。
单机模式不挂载（走 LocalProvider 直接查 DB）。

服务端一律用 Local 实现（get_local_service），否则 remote 模式下会 HTTP 回环自调用。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.kernel.gsb.provider import get_local_service as _get_local

router = APIRouter(prefix="/internal", tags=["internal-runtime"])


@router.get("/tasks", operation_id="runtime_tasks_list", include_in_schema=False)
async def list_tasks(repo_key: str, limit: int = 50) -> list:
    """供 RuntimeServiceRemote.get_tasks() 调用。"""
    return _get_local("runtime").get_tasks(repo_key, limit)


@router.get("/tasks/{task_id}/messages", operation_id="runtime_task_messages_list", include_in_schema=False)
async def get_task_messages(task_id: str, after_id: int = 0) -> list:
    """供 RuntimeServiceRemote.get_task_messages() 调用。"""
    return _get_local("runtime").get_task_messages(task_id, after_id)


@router.get("/events", operation_id="runtime_events_list", include_in_schema=False)
async def list_events(repo_key: str, limit: int = 50) -> list:
    """供 RuntimeServiceRemote.get_events() 调用。"""
    return _get_local("runtime").get_events(repo_key, limit)


@router.post("/events/status-counts", operation_id="runtime_events_status_counts", include_in_schema=False)
async def get_event_status_counts(body: dict) -> dict:
    """供 RuntimeServiceRemote.get_event_status_counts() 调用。"""
    return {"rows": _get_local("runtime").get_event_status_counts(
        body.get("repo_keys") or [], body.get("agent_type") or "")}


@router.post("/events/search", operation_id="runtime_events_search_internal", include_in_schema=False)
async def search_events(body: dict) -> dict:
    """供 RuntimeServiceRemote.search_events() 调用。"""
    return _get_local("runtime").search_events(
        body.get("repo_keys") or [], body.get("agent_type") or "",
        body.get("keyword") or "", int(body.get("page") or 1), int(body.get("page_size") or 10))


@router.get("/events/{seq}", operation_id="runtime_events_detail", include_in_schema=False)
async def get_event(seq: int) -> dict:
    """供 RuntimeServiceRemote.get_event_detail() 调用。"""
    return _get_local("runtime").get_event_detail(seq)


@router.get("/runtimes", operation_id="runtime_runtimes_list", include_in_schema=False)
async def list_runtimes() -> list:
    """供 RuntimeServiceRemote.get_runtimes() 调用。"""
    return _get_local("runtime").get_runtimes()


@router.get("/issues/{repo_key}/{issue_number}/state", operation_id="runtime_issue_state_get", include_in_schema=False)
async def get_issue_state(repo_key: str, issue_number: int) -> dict:
    """供 RuntimeServiceRemote.get_issue_state() 调用。"""
    return _get_local("runtime").get_issue_state(repo_key, issue_number)

@router.get("/credentials/rows", operation_id="runtime_credentials_rows", include_in_schema=False)
async def list_credential_rows() -> dict:
    """供 RuntimeServiceRemote.list_credential_rows() 调用。"""
    return {"rows": _get_local("runtime").list_credential_rows()}


@router.get("/engine-tags", operation_id="runtime_engine_tags", include_in_schema=False)
async def list_engine_tags(runtime_id: int, enabled_only: int = 1) -> dict:
    """供 RuntimeServiceRemote.list_engine_tags() 调用。"""
    return {"tags": _get_local("runtime").list_engine_tags(int(runtime_id), bool(enabled_only))}


@router.post("/agent-sessions/row", operation_id="runtime_agent_session_row", include_in_schema=False)
async def get_agent_session_row(body: dict) -> dict:
    """供 RuntimeServiceRemote.get_agent_session_row() 调用（repo_keys 走 body）。"""
    return {"row": _get_local("runtime").get_agent_session_row(
        body.get("repo_keys") or [], body.get("agent_type") or "", body.get("token_field") or "")}


@router.get("/credentials/health", operation_id="runtime_credentials_health", include_in_schema=False)
async def get_credential_health_rows() -> list:
    """供 RuntimeServiceRemote.get_credential_health_rows() 调用。"""
    return _get_local("runtime").get_credential_health_rows()


@router.post("/tasks/inflight-count", operation_id="runtime_tasks_inflight_count", include_in_schema=False)
async def get_inflight_task_count(body: dict) -> dict:
    """供 RuntimeServiceRemote.get_inflight_task_count() 调用（repo_keys 走 body）。"""
    return {"count": _get_local("runtime").get_inflight_task_count(body.get("repo_keys") or [])}


@router.get("/issues/card", operation_id="runtime_issue_card_get", include_in_schema=False)
async def get_issue_card(repo_key: str, issue_number: str) -> dict:
    """供 RuntimeServiceRemote.get_issue_card() 调用（issue 单表权威卡片）。

    repo_key 形如 ``gitcode:owner/repo``，含 ``:`` 和 ``/`` —— 放进路径参数会被
    URL 切碎成多段导致 404（实测）。这里用 query 参数，与 get_chat_conversations 同款。

    Issue #4065：`issue_number` 由 int 改为 str 并在入口先归一（与 #3999 的 stage 解析
    降级口径统一）。此前 FastAPI 强转 int 失败 → 裸 422（`autopilot-*` 244 条/小时，
    不可读）。现在：
    - 可归一（数字 / 平台原生 ident `IK*` / `autopilot-*` 合成键）→ 正常查卡片；
    - 不可归一（`pr_*` 订阅伪 id、空、含分隔文本、超长）→ 400 + 可读原因，不进查询层。
    """
    from src.kernel.issue_ident import normalize_card_ident

    _ident = normalize_card_ident(issue_number)
    if not _ident:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "error": "invalid_issue_ident",
            "message": (f"issue_number 需为数字 / 平台 ident / autopilot-* 合成键，"
                        f"无法归一为卡片主键（收到 {issue_number!r}）"),
            "issue_number": issue_number,
        })
    return _get_local("runtime").get_issue_card(repo_key, _ident)


@router.post("/issues/{repo_key}/{issue_number}/state", operation_id="runtime_issue_state_update", include_in_schema=False)
async def update_issue_state(repo_key: str, issue_number: int, body: dict) -> dict:
    """供 RuntimeServiceRemote.update_issue_state() 调用。"""
    return _get_local("runtime").update_issue_state(repo_key, issue_number, body)


@router.get("/chat/conversations", operation_id="runtime_chat_conversations_list", include_in_schema=False)
async def list_chat_conversations(repo_key: str = "", limit: int = 50) -> list:
    """供 RuntimeServiceRemote.get_chat_conversations() 调用（v2.2：chat 表归运行域）。"""
    return _get_local("runtime").get_chat_conversations(repo_key, limit)


@router.get("/chat/conversations/{conv_id}/messages", operation_id="runtime_chat_messages_list", include_in_schema=False)
async def get_chat_messages(conv_id: str, limit: int = 100, before: int = 0) -> list:
    """供 RuntimeServiceRemote.get_chat_messages() 调用（v2.2：chat 表归运行域）。"""
    return _get_local("runtime").get_chat_messages(conv_id, limit, before)


# ---------- P2 服务接口补齐（N1–N5 / N11–N13） ----------
# include_in_schema=False：内部契约不进对外 /openapi.json（对外契约只含 /api/* 安全路由），
# 内部契约由 scripts/gen_internal_contract.py 单独快照。


@router.get("/skills", operation_id="runtime_skills_list", include_in_schema=False)
async def list_skills(repo_key: str = "", q: str = "", limit: int = 50,
                      offset: int = 0) -> list:
    """N1 技能列表（mgmt / api 读）。"""
    return _get_local("runtime").list_skills(repo_key, q, limit, offset)


@router.get("/skills/{skill_id}", operation_id="runtime_skills_detail",
            include_in_schema=False)
async def get_skill_detail(skill_id: int) -> dict:
    """N2 技能详情（含短时效 content_url）。"""
    return _get_local("runtime").get_skill_detail(skill_id)


@router.get("/engine-capabilities", operation_id="runtime_engine_capabilities_list",
            include_in_schema=False)
async def list_engine_capabilities(limit: int = 500) -> list:
    """N3 引擎×能力矩阵一次全量。"""
    return _get_local("runtime").list_engine_capabilities(limit)


@router.get("/runtime-credentials/{runtime_id}",
            operation_id="runtime_credentials_meta", include_in_schema=False)
async def get_runtime_credential_meta(runtime_id: int) -> dict:
    """N4 运行凭据元数据（不含明文；调用方须带服务令牌）。"""
    return _get_local("runtime").get_runtime_credential_meta(runtime_id)


@router.get("/meta/{namespace}", operation_id="runtime_meta_namespace",
            include_in_schema=False)
async def get_meta_namespace(namespace: str) -> dict:
    """N5 KV 元数据批量读（白名单 namespace）。"""
    try:
        return _get_local("runtime").get_meta_namespace(namespace)
    except ValueError as exc:
        return {"status": "rejected", "error": str(exc)}


@router.post("/tasks/batch", operation_id="runtime_tasks_batch", include_in_schema=False)
async def get_tasks_batch(body: dict) -> list:
    """N11 批量任务摘要（列表页一屏一次）。"""
    return _get_local("runtime").get_tasks_batch(list((body or {}).get("task_ids") or []))


@router.post("/stats/tasks", operation_id="runtime_stats_tasks", include_in_schema=False)
async def get_task_stats(body: dict) -> dict:
    """N12 任务聚合统计。"""
    body = body or {}
    return _get_local("runtime").get_task_stats(
        str(body.get("repo_key") or ""), str(body.get("since") or ""))


@router.get("/business-table-counts", operation_id="runtime_business_table_counts",
            include_in_schema=False)
async def business_table_counts(user_id: str) -> dict:
    """N14 某账号业务数据按表计数（供 RuntimeServiceRemote 调用；缺表 → count=None）。"""
    return _get_local("runtime").user_business_table_counts(user_id)


@router.post("/reconcile-engine-references", operation_id="runtime_reconcile_engine_refs",
             include_in_schema=False)
async def reconcile_engine_references(body: dict) -> dict:
    """N15 引擎引用对账（跨域写；幂等可重放）。"""
    body = body or {}
    return _get_local("runtime").reconcile_engine_references(
        list(body.get("runtime_ids") or []), int(body.get("keep_id") or 0),
        str(body.get("reason") or ""))


@router.post("/transfer-user-business-data", operation_id="runtime_transfer_user_data",
             include_in_schema=False)
async def transfer_user_business_data(body: dict) -> dict:
    """N16 占位账号合并：本域业务数据归属转移（幂等）。"""
    body = body or {}
    return _get_local("runtime").transfer_user_business_data(
        body.get("from_user_id"), body.get("to_user_id"))


@router.post("/xwrite/apply", operation_id="runtime_xwrite_apply", include_in_schema=False)
async def xwrite_apply(body: dict) -> dict:
    """N13 跨域写补偿应用（幂等键必填；action 白名单）。"""
    from src.domains.runtime import public as _rt

    body = body or {}
    action = str(body.get("action") or "")
    key = str(body.get("idempotency_key") or "")
    if not action or not key:
        return {"status": "rejected", "error": "action and idempotency_key are required"}
    try:
        return _rt.xwrite_apply(action, dict(body.get("payload") or {}), key)
    except ValueError as exc:
        return {"status": "rejected", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 目标库不可用时必须让调用方留待补偿
        return {"status": "failed", "error": str(exc)[:300]}
