"""MgmtService RemoteProvider — HTTP API 调用 Mgmt 进程（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.3 RemoteProvider.

集群模式：通过 HTTP API 调用 Mgmt 进程的 /internal/* 端点。
"""
from __future__ import annotations

from typing import Any

from src.domains.contracts.remote.http_client import ServiceHttpClient
from src.domains.contracts.mgmt_service import MgmtService


class MgmtServiceRemote(MgmtService):
    """集群模式：通过 HTTP API 调用 Mgmt 进程的 /internal/* 端点。

    **当前不投入使用**：``matrix_forces_lib("mgmt")`` 恒为 True，``get_service("mgmt")``
    永远返回 MgmtServiceLocal，任何进程（mgmt / event / worker / runtime / insight）
    都不会经由本类远程调用 mgmt。保留本类只是为了让 Provider 工厂保持 (Local, Remote)
    成对注册的形态。

    注意 base_url 仍是字面占位符 `${GRAPE_MGMT_URL}`（历史遗留，未接服务发现）——
    这正好意味着即便有人直接实例化本类，也打不出一次成功的 /internal 调用。
    真要恢复 mgmt 的远程调用，需要三件事一起做：解除矩阵守卫、把 base_url 接到
    GSB 服务发现、并确认 mgmt 自身的 /internal 不会形成回环。
    """

    def __init__(self):
        self.client = ServiceHttpClient("mgmt", base_url="${GRAPE_MGMT_URL}")

    def get_repo_webhook_config(self, repo_key: str) -> dict:
        return self.client.get(f"/internal/repos/{repo_key}/webhook-config")

    def get_agent_session(self, repo_key: str, agent_type: str) -> dict:
        return self.client.get(f"/internal/sessions/{repo_key}/{agent_type}")

    def get_scene_config(self, repo_key: str) -> list:
        return self.client.get(f"/internal/scenes/{repo_key}")

    def get_scene_pool_engine_ids(self, scene_id: str) -> list:
        return self.client.get(f"/internal/scenes/{scene_id}/engine-ids")

    def verify_session_token(self, token_hash: str) -> dict:
        return self.client.post("/internal/tokens/verify", {"token_hash": token_hash})

    def get_bot_config(self, repo_key: str) -> dict:
        return self.client.get(f"/internal/repos/{repo_key}/bot-config")

    def get_skill(self, skill_id: str) -> Any:
        return self.client.get(f"/internal/skills/{skill_id}")


    # ---- P2 服务接口补齐（N6/N8/N9/N10，mgmt 只读白名单） ----

    def get_repo_bundle(self, repo_key: str) -> dict:
        return self.client.get(f"/internal/repos/{repo_key}")

    def get_account(self, account_id: str) -> dict:
        return self.client.get(f"/internal/accounts/{account_id}")

    def get_knowledge(self, knowledge_id: int) -> dict:
        return self.client.get(f"/internal/knowledge/{int(knowledge_id)}")

    def get_user_mapping_batch(self, keys: list) -> list:
        return self.client.post("/internal/user-mapping", json_data={"keys": list(keys or [])})

    # ---- Issue #3632：平台卡跨域只读 ----

    def get_repo_platform_cards(self, enabled_only: bool = True) -> list:
        return self.client.get("/internal/platform-cards",
                               params={"enabled_only": "1" if enabled_only else "0"}) or []
