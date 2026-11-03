"""RuntimeService 契约接口（Issue #699 Phase 2 空壳）。

Mgmt/Insight 进程对运行域（grape_runtime schema）的跨域只读访问统一收口。
实现（Local/Remote provider）Phase 5 填充。接口依据 design §4.4 / §7。
"""
from abc import ABC, abstractmethod
from typing import Any


class RuntimeService(ABC):
    """运行域服务契约——供 Mgmt 与 Insight 调用。"""

    @abstractmethod
    def get_events(self, repo_key: str, limit: int = 50) -> list:
        """A 用户面事件列表——返回 events 行列表。"""

    @abstractmethod
    def get_event_detail(self, seq: int) -> dict:
        """A 事件详情。"""

    @abstractmethod
    def get_tasks(self, repo_key: str, limit: int = 50) -> list:
        """A 任务列表——返回 agent_tasks 行列表。"""

    @abstractmethod
    def get_task_messages(self, task_id: str, after_id: int = 0) -> list:
        """A SSE 流——返回 task_messages 增量行列表。"""

    @abstractmethod
    def get_runtimes(self) -> list:
        """A 引擎列表——返回 agent_runtimes 行列表。"""

    @abstractmethod
    def get_issue_state(self, repo_key: str, issue_number: int) -> dict:
        """A Issue 阶段状态——返回 issue_state 当前行。"""

    @abstractmethod
    def update_issue_state(self, repo_key: str, issue_number: int, patch: dict) -> Any:
        """A 用户操作推进 Issue 状态（Phase 5 细化事务边界）。"""
