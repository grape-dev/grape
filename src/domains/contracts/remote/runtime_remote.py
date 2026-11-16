"""RuntimeService RemoteProvider — HTTP API 调用 Runtime 进程（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.4 RemoteProvider.

集群模式：通过 HTTP API 调用 Runtime 进程的 /internal/* 端点。
"""
from __future__ import annotations

from typing import Any

from src.domains.contracts.remote.http_client import ServiceHttpClient
from src.domains.contracts.runtime_service import RuntimeService


class RuntimeServiceRemote(RuntimeService):
    """集群模式：通过 HTTP API 调用 Runtime 进程的 /internal/* 端点。"""

    def __init__(self):
        self.client = ServiceHttpClient("runtime", base_url="${GRAPE_RUNTIME_URL}")

    def get_events(self, repo_key: str, limit: int = 50) -> list:
        return self.client.get(
            "/internal/events", params={"repo_key": repo_key, "limit": limit}
        )

    def get_event_detail(self, seq: int) -> dict:
        return self.client.get(f"/internal/events/{seq}")

    def get_tasks(self, repo_key: str, limit: int = 50) -> list:
        return self.client.get(
            "/internal/tasks", params={"repo_key": repo_key, "limit": limit}
        )

    def get_task_messages(self, task_id: str, after_id: int = 0) -> list:
        return self.client.get(
            f"/internal/tasks/{task_id}/messages", params={"after_id": after_id}
        )

    def get_runtimes(self) -> list:
        return self.client.get("/internal/runtimes")

    def get_issue_state(self, repo_key: str, issue_number: int) -> dict:
        return self.client.get(f"/internal/issues/{repo_key}/{issue_number}/state")

    def get_issue_card(self, repo_key: str, issue_number: int) -> dict:
        # repo_key 含 ':' 和 '/'，不能进路径段（会被 URL 解析切碎 → 404）
        return self.client.get(
            "/internal/issues/card",
            params={"repo_key": repo_key, "issue_number": issue_number},
        )

    def update_issue_state(self, repo_key: str, issue_number: int, patch: dict) -> Any:
        return self.client.post(
            f"/internal/issues/{repo_key}/{issue_number}/state", json_data=patch
        )

    def get_chat_conversations(self, repo_key: str, limit: int = 50) -> list:
        return self.client.get(
            "/internal/chat/conversations", params={"repo_key": repo_key, "limit": limit}
        )

    def get_chat_messages(self, conv_id: str, limit: int = 100, before: int = 0) -> list:
        return self.client.get(
            f"/internal/chat/conversations/{conv_id}/messages",
            params={"limit": limit, "before": before},
        )


    # ---- P2 服务接口补齐（N1/N2/N3/N4/N5/N11/N12） ----

    def list_skills(self, repo_key: str = "", q: str = "", limit: int = 50,
                    offset: int = 0) -> list:
        return self.client.get("/internal/skills", params={
            "repo_key": repo_key, "q": q, "limit": limit, "offset": offset})

    def get_skill_detail(self, skill_id: int) -> dict:
        return self.client.get(f"/internal/skills/{int(skill_id)}")

    def list_engine_capabilities(self, limit: int = 500) -> list:
        return self.client.get("/internal/engine-capabilities", params={"limit": limit})

    def get_runtime_credential_meta(self, runtime_id: int) -> dict:
        return self.client.get(f"/internal/runtime-credentials/{int(runtime_id)}")

    def get_meta_namespace(self, namespace: str) -> dict:
        return self.client.get(f"/internal/meta/{namespace}")

    def get_tasks_batch(self, task_ids: list) -> list:
        return self.client.post("/internal/tasks/batch", json_data={"task_ids": list(task_ids or [])})

    def get_task_stats(self, repo_key: str = "", since: str = "") -> dict:
        return self.client.post("/internal/stats/tasks",
                                json_data={"repo_key": repo_key, "since": since})
