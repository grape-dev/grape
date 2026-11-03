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
