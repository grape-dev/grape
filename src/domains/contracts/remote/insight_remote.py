"""InsightService RemoteProvider — HTTP API 调用 Insight 进程（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.5 RemoteProvider.

集群模式：通过 HTTP API 调用 Insight 进程的 /internal/* 端点。
"""
from __future__ import annotations

from typing import Any

from src.domains.contracts.remote.http_client import ServiceHttpClient
from src.domains.contracts.insight_service import InsightService


class InsightServiceRemote(InsightService):
    """集群模式：通过 HTTP API 调用 Insight 进程的 /internal/* 端点。"""

    def __init__(self):
        self.client = ServiceHttpClient("insight", base_url="${GRAPE_INSIGHT_URL}")

    def log_obs(self, entry: dict) -> Any:
        return self.client.post("/internal/logs", json_data=entry)

    def log_llm_usage(self, usage: dict) -> Any:
        return self.client.post("/internal/llm-usage", json_data=usage)

    def log_api_latency(self, entry: dict) -> Any:
        return self.client.post("/internal/latency", json_data=entry)

    def query_metrics(self, repo_key: str, window: str = "1h") -> list:
        return self.client.get(
            f"/internal/metrics/{repo_key}", params={"window": window}
        )

    def query_slo(self, repo_key: str) -> list:
        return self.client.get(f"/internal/slo/{repo_key}")

    def query_alerts(self, repo_key: str) -> list:
        return self.client.get(f"/internal/alerts/{repo_key}")

    def aggregate_runtime_stats(self) -> dict:
        return self.client.get("/internal/dashboard/overview")
