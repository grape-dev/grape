"""MgmtService 契约接口（Issue #699 Phase 2 空壳）。

Runtime/Insight 进程对管理域（grape_mgmt schema）的跨域只读/读写访问统一收口。
实现（Local/Remote provider）Phase 5 填充：src/domains/contracts/local/mgmt_local.py 等。

接口清单依据 design §4.4 跨域共享表分析与 §7 契约定义。
"""
from abc import ABC, abstractmethod
from typing import Any


class MgmtService(ABC):
    """管理域服务契约——供 Runtime（Event/Worker）与 Insight 调用。"""

    @abstractmethod
    def get_repo_webhook_config(self, repo_key: str) -> dict:
        """B 校验 webhook token——返回 repo_key, webhook_token, bot_username, org_id。"""

    @abstractmethod
    def get_agent_session(self, repo_key: str, agent_type: str) -> dict:
        """B 路由判定——返回 runtime_id, skill, skill_id。"""

    @abstractmethod
    def get_scene_config(self, repo_key: str) -> list:
        """B 读取场景节点——返回 scene_id, nodes_json, work_branch, deploy_url 列表。"""

    @abstractmethod
    def get_scene_pool_engine_ids(self, scene_id: str) -> list:
        """C 引擎选择——返回场景绑定的引擎 ID 列表。"""

    @abstractmethod
    def verify_session_token(self, token_hash: str) -> dict:
        """C Worker 鉴权——返回 credential_id, scope, owner_id。"""

    @abstractmethod
    def get_bot_config(self, repo_key: str) -> dict:
        """B/C 读取 Bot 配置——bot_token, bot_username。"""

    @abstractmethod
    def get_skill(self, skill_id: str) -> Any:
        """技能加载——返回技能定义（Phase 5 细化返回结构）。"""
