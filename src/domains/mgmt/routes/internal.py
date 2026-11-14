"""Mgmt 进程内部端点——P2 起改为**只读白名单 + 补偿应用端点**。

历史：2026-11 架构收敛期 mgmt 完全关闭业务型 `/internal/*`，导致 runtime 侧
`get_service("mgmt")` 的 webhook-config / bot-config 等调用 42 次 404（被 catch 后降级为空
token）。三库独立化方案 §D5 修订：mgmt 开放**只读白名单**内部端点，字段与作用域受控。

本文件只导出两个 router：
  · `readonly_router`：GET 白名单（不含任何写操作；仓库作用域 + 字段白名单）
  · `xwrite_router`：POST `/internal/xwrite/apply`（N13 补偿应用，幂等键必填，action 白名单）

`attach_internal_routes("mgmt")` 只 include 这两个 router；原全量 `router` 已废弃。
"""
from __future__ import annotations

from fastapi import APIRouter

from src.domains.contracts.local.mgmt_local import MgmtServiceLocal
from src.domains.mgmt import public as _mgmt_public
from src.kernel.gsb.provider import get_local_service as _get_local


def _mgmt_local() -> MgmtServiceLocal:
    """P2 新端点显式走 Local：服务端永远不 HTTP 回环调用自己（provider 可能被其它
    进程的路由配置成 remote，不能用来判定"本进程实现"）。"""
    return MgmtServiceLocal()

router = APIRouter(prefix="/internal", tags=["internal-mgmt"], include_in_schema=False)
readonly_router = APIRouter(prefix="/internal", tags=["internal-mgmt-readonly"],
                            include_in_schema=False)
xwrite_router = APIRouter(prefix="/internal", tags=["internal-mgmt-xwrite"],
                          include_in_schema=False)


# ---------- 只读白名单 ----------

@readonly_router.get("/repos/{repo_key:path}/webhook-config",
                     operation_id="mgmt_repo_webhook_config")
async def get_repo_webhook_config(repo_key: str) -> dict:
    """供 MgmtServiceRemote.get_repo_webhook_config() 调用。"""
    return _get_local("mgmt").get_repo_webhook_config(repo_key)


@readonly_router.get("/repos/{repo_key:path}/bot-config",
                     operation_id="mgmt_repo_bot_config")
async def get_bot_config(repo_key: str) -> dict:
    """供 MgmtServiceRemote.get_bot_config() 调用。"""
    return _get_local("mgmt").get_bot_config(repo_key)


@readonly_router.get("/sessions/{repo_key:path}/{agent_type}",
                     operation_id="mgmt_agent_session_get")
async def get_agent_session(repo_key: str, agent_type: str) -> dict:
    """供 MgmtServiceRemote.get_agent_session() 调用。"""
    return _get_local("mgmt").get_agent_session(repo_key, agent_type)


@readonly_router.get("/scenes/{repo_key:path}", operation_id="mgmt_scenes_list")
async def get_scenes(repo_key: str) -> list:
    """N7 场景配置（已定义，P2 起挂载）。"""
    return _get_local("mgmt").get_scene_config(repo_key)


@readonly_router.get("/scenes/{scene_id}/engine-ids", operation_id="mgmt_scene_engine_ids")
async def get_scene_engine_ids(scene_id: str) -> list:
    """供 MgmtServiceRemote.get_scene_pool_engine_ids() 调用。"""
    return _get_local("mgmt").get_scene_pool_engine_ids(scene_id)


@readonly_router.post("/tokens/verify", operation_id="mgmt_token_verify")
async def verify_token(body: dict) -> dict:
    """会话令牌校验（只读语义：不落写；worker 鉴权链路必需）。"""
    return _get_local("mgmt").verify_session_token((body or {}).get("token_hash", ""))


@readonly_router.get("/skills/{skill_id}", operation_id="mgmt_skill_get")
async def get_skill(skill_id: str) -> dict:
    """供 MgmtServiceRemote.get_skill() 调用。"""
    result = _get_local("mgmt").get_skill(skill_id)
    return result if isinstance(result, dict) else {"result": result}


@readonly_router.get("/accounts/{account_id}", operation_id="mgmt_accounts_get")
async def get_account(account_id: str) -> dict:
    """N8 账号只读（限字段：id/username/display_name/status/org_id）。"""
    return _mgmt_local().get_account(account_id) or {}


@readonly_router.get("/knowledge/{knowledge_id}", operation_id="mgmt_knowledge_get")
async def get_knowledge(knowledge_id: int) -> dict:
    """N9 知识条目元数据。"""
    return _mgmt_local().get_knowledge(knowledge_id) or {}


@readonly_router.post("/user-mapping", operation_id="mgmt_user_mapping_batch")
async def get_user_mapping_batch(body: dict) -> list:
    """N10 批量外部身份 → user_id（批量 ≤ 200）。"""
    return _mgmt_local().get_user_mapping_batch(list((body or {}).get("keys") or []))


@readonly_router.get("/repos/{repo_key:path}", operation_id="mgmt_repo_bundle")
async def get_repo_bundle(repo_key: str) -> dict:
    """N6 仓库聚合（配置 + 凭据有无 + webhook + 场景）。"""
    return _mgmt_local().get_repo_bundle(repo_key)


# ---------- N13 补偿应用（唯一写端点，幂等键必填） ----------

def _xwrite_apply(action: str, payload: dict, idempotency_key: str) -> dict:
    """mgmt 侧 action 白名单（只允许调用 public 写函数，不接受 SQL/表名）。"""
    from src.kernel import cross_write as xw

    handlers = {
        "feishu_onboard_task_delete": lambda p: {"deleted": _mgmt_public.delete_feishu_onboard_task(
            str(p["app_id"]))},
        "user_mapping_feishu_upsert": lambda p: _mgmt_public.upsert_user_mapping_feishu(
            str(p["user_id"]), str(p["open_id"]), str(p.get("feishu_name", "")),
            str(p.get("app_id", ""))),
        "user_mapping_feishu_clear": lambda p: {"cleared": _mgmt_public.clear_user_mapping_feishu(
            str(p.get("user_id", "")), str(p.get("app_id", "")))},
        "scene_nodes_set": lambda p: _mgmt_public.set_scene_nodes(
            str(p["nodes_json"]), scene_id=p.get("scene_id"), row_id=p.get("row_id"),
            repo_key=p.get("repo_key")),
        "scene_deploy_url_set": lambda p: {"updated": _mgmt_public.set_scene_deploy_url(
            str(p["deploy_url"]), scene_id=p.get("scene_id"), row_id=p.get("row_id"),
            repo_key=p.get("repo_key"))},
    }
    fn = handlers.get(action)
    if fn is None:
        raise ValueError(f"unregistered xwrite action: {action!r}")
    if not xw.claim_applied(idempotency_key, action):
        return {"status": "duplicate", "action": action, "idempotency_key": idempotency_key}
    return {"status": "applied", "action": action, "result": fn(payload or {})}


@xwrite_router.post("/xwrite/apply", operation_id="mgmt_xwrite_apply")
async def xwrite_apply(body: dict) -> dict:
    """N13 补偿应用（action 白名单 + 幂等键去重）。"""
    body = body or {}
    action = str(body.get("action") or "")
    key = str(body.get("idempotency_key") or "")
    if not action or not key:
        return {"status": "rejected", "error": "action and idempotency_key are required"}
    try:
        return _xwrite_apply(action, dict(body.get("payload") or {}), key)
    except ValueError as exc:
        return {"status": "rejected", "error": str(exc)}