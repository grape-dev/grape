"""InsightService 契约接口（Issue #699 Phase 2 空壳）。

Runtime/Mgmt 进程对数据域（grape_insight schema）的写入与聚合访问统一收口。
实现（Local/Remote provider）Phase 5 填充。接口依据 design §4.4 / §7 / §5.4。
"""
from abc import ABC, abstractmethod
from typing import Any


class InsightService(ABC):
    """数据域服务契约——供 Runtime（写观测数据）与 Mgmt（读面板/看板）调用。"""

    @abstractmethod
    def log_obs(self, entry: dict) -> Any:
        """Runtime 写可观测性日志（obs_log；未来改 HTTP API）。"""

    @abstractmethod
    def log_llm_usage(self, usage: dict) -> Any:
        """Runtime 写 LLM 用量（obs_llm_usage）。"""

    @abstractmethod
    def log_api_latency(self, entry: dict) -> Any:
        """A 前端延迟上报（api_latency_log）。"""

    @abstractmethod
    def query_metrics(self, repo_key: str, window: str = "1h") -> list:
        """A 指标聚合查询（obs_metric_rollup）。"""

    @abstractmethod
    def query_slo(self, repo_key: str) -> list:
        """A SLO 状态查询（obs_slo / obs_slo_snapshots）。"""

    @abstractmethod
    def query_alerts(self, repo_key: str) -> list:
        """A 告警查询（obs_alert_rules / obs_alert_events）。"""

    @abstractmethod
    def aggregate_runtime_stats(self) -> dict:
        """聚合 grape_runtime 数据（events/agent_tasks 维度，供看板）。"""
