"""MgmtService RemoteProvider — HTTP API 调用 Mgmt 进程（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.3 RemoteProvider.

集群模式：通过 HTTP API 调用 Mgmt 进程的 /internal/* 端点。
"""
from __future__ import annotations

from typing import Any

from src.domains.contracts.remote.http_client import ServiceHttpClient
from src.domains.contracts.mgmt_service import MgmtService


class MgmtServiceRemote(MgmtService):
    """集群模式：通过 HTTP API 调用 Mgmt 进程的 /internal/* 端点。"""

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
