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

    def list_credential_rows(self) -> list:
        resp = self.client.get("/internal/credentials/rows")
        return list((resp or {}).get("rows") or [])

    def list_engine_tags(self, runtime_id: int, enabled_only: bool = True) -> list:
        resp = self.client.get("/internal/engine-tags",
                               params={"runtime_id": int(runtime_id),
                                       "enabled_only": 1 if enabled_only else 0})
        return list((resp or {}).get("tags") or [])

    def get_agent_session_row(self, repo_keys: list, agent_type: str, token_field: str) -> list:
        r = self.client.post("/internal/agent-sessions/row",
                             json_data={"repo_keys": list(repo_keys or []),
                                        "agent_type": agent_type, "token_field": token_field})
        return list((r or {}).get("row") or [])

    def get_event_status_counts(self, repo_keys: list, agent_type: str) -> list:
        r = self.client.post("/internal/events/status-counts",
                             json_data={"repo_keys": list(repo_keys or []), "agent_type": agent_type})
        return list((r or {}).get("rows") or [])

    def search_events(self, repo_keys: list, agent_type: str, keyword: str = "",
                      page: int = 1, page_size: int = 10) -> dict:
        r = self.client.post("/internal/events/search",
                             json_data={"repo_keys": list(repo_keys or []), "agent_type": agent_type,
                                        "keyword": keyword, "page": page, "page_size": page_size})
        return r or {"total": 0, "rows": []}

    def get_credential_health_rows(self) -> list:
        return self.client.get("/internal/credentials/health")

    def get_inflight_task_count(self, repo_keys: list) -> int:
        # repo_keys 是一批 "gitcode:owner/repo"，只能走 query/body，不进路径
        r = self.client.post("/internal/tasks/inflight-count",
                             json_data={"repo_keys": list(repo_keys or [])})
        return int((r or {}).get("count") or 0)

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

    # ---- Issue #3097：占位账号业务数据判定 / 引擎引用对账 ----

    def user_business_table_counts(self, user_id) -> dict:
        return self.client.get("/internal/business-table-counts",
                               params={"user_id": str(user_id)})

    def reconcile_engine_references(self, runtime_ids: list, keep_id: int = 0,
                                    reason: str = "") -> dict:
        return self.client.post("/internal/reconcile-engine-references",
                                json_data={"runtime_ids": list(runtime_ids or []),
                                           "keep_id": int(keep_id), "reason": reason})

    def transfer_user_business_data(self, from_user_id, to_user_id) -> dict:
        return self.client.post("/internal/transfer-user-business-data",
                                json_data={"from_user_id": str(from_user_id),
                                           "to_user_id": str(to_user_id)})
