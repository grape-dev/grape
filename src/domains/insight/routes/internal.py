"""Insight 进程内部端点——仅供其他域 RemoteProvider 调用（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.7.

集群模式下挂载到 Insight 进程，提供 /internal/* 端点供
MgmtServiceRemote / RuntimeServiceRemote 调用。
单机模式不挂载（走 LocalProvider 直接查 DB）。
"""
from __future__ import annotations

from fastapi import APIRouter

from src.kernel.provider import get_insight_service

router = APIRouter(prefix="/internal", tags=["internal-insight"])


@router.post("/logs")
async def log_obs(body: dict) -> dict:
    """供 InsightServiceRemote.log_obs() 调用。"""
    return get_insight_service().log_obs(body)


@router.post("/llm-usage")
async def log_llm_usage(body: dict) -> dict:
    """供 InsightServiceRemote.log_llm_usage() 调用。"""
    return get_insight_service().log_llm_usage(body)


@router.post("/latency")
async def log_latency(body: dict) -> dict:
    """供 InsightServiceRemote.log_api_latency() 调用。"""
    return get_insight_service().log_api_latency(body)


@router.get("/metrics/{repo_key}")
async def get_metrics(repo_key: str, window: str = "1h") -> list:
    """供 InsightServiceRemote.query_metrics() 调用。"""
    return get_insight_service().query_metrics(repo_key, window)


@router.get("/slo/{repo_key}")
async def get_slo(repo_key: str) -> list:
    """供 InsightServiceRemote.query_slo() 调用。"""
    return get_insight_service().query_slo(repo_key)


@router.get("/alerts/{repo_key}")
async def get_alerts(repo_key: str) -> list:
    """供 InsightServiceRemote.query_alerts() 调用。"""
    return get_insight_service().query_alerts(repo_key)


@router.get("/dashboard/overview")
async def get_dashboard_overview() -> dict:
    """供 InsightServiceRemote.aggregate_runtime_stats() 调用。"""
    return get_insight_service().aggregate_runtime_stats()
