"""Insight 进程内部端点——仅供其他域 RemoteProvider 调用（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.7.

集群模式下挂载到 Insight 进程，提供 /internal/* 端点供
MgmtServiceRemote / RuntimeServiceRemote 调用。
单机模式不挂载（走 LocalProvider 直接查 DB）。

服务端一律用 Local 实现（get_local_service），否则 remote 模式下会 HTTP 回环自调用。
"""
from __future__ import annotations

from fastapi import APIRouter

from src.kernel.gsb.provider import get_local_service as _get_local

router = APIRouter(prefix="/internal", tags=["internal-insight"])


@router.post("/logs", operation_id="insight_logs_ingest", include_in_schema=False)
async def log_obs(body: dict) -> dict:
    """供 InsightServiceRemote.log_obs() 调用。"""
    return _get_local("insight").log_obs(body)


@router.post("/llm-usage", operation_id="insight_llm_usage_ingest", include_in_schema=False)
async def log_llm_usage(body: dict) -> dict:
    """供 InsightServiceRemote.log_llm_usage() 调用。"""
    return _get_local("insight").log_llm_usage(body)


@router.post("/latency", operation_id="insight_latency_ingest", include_in_schema=False)
async def log_latency(body: dict) -> dict:
    """供 InsightServiceRemote.log_api_latency() 调用。"""
    return _get_local("insight").log_api_latency(body)


@router.get("/metrics/{repo_key}", operation_id="insight_metrics_get", include_in_schema=False)
async def get_metrics(repo_key: str, window: str = "1h") -> list:
    """供 InsightServiceRemote.query_metrics() 调用。"""
    return _get_local("insight").query_metrics(repo_key, window)


@router.get("/slo/{repo_key}", operation_id="insight_slo_get", include_in_schema=False)
async def get_slo(repo_key: str) -> list:
    """供 InsightServiceRemote.query_slo() 调用。"""
    return _get_local("insight").query_slo(repo_key)


@router.get("/alerts/{repo_key}", operation_id="insight_alerts_list", include_in_schema=False)
async def get_alerts(repo_key: str) -> list:
    """供 InsightServiceRemote.query_alerts() 调用。"""
    return _get_local("insight").query_alerts(repo_key)


@router.get("/dashboard/overview", operation_id="insight_dashboard_overview", include_in_schema=False)
async def get_dashboard_overview() -> dict:
    """供 InsightServiceRemote.aggregate_runtime_stats() 调用。"""
    return _get_local("insight").aggregate_runtime_stats()
