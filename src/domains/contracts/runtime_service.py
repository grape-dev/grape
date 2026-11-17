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
    def list_credential_rows(self) -> list:
        """runtime_credentials 全量行（只含密文/元数据列，解密由调用方负责）。"""

    @abstractmethod
    def list_engine_tags(self, runtime_id: int, enabled_only: bool = True) -> list:
        """某引擎的能力标签列表。"""

    @abstractmethod
    def get_agent_session_row(self, repo_keys: list, agent_type: str, token_field: str) -> list:
        """某类 Agent 在仓库集合内的会话行（列序由 owner 域固定）。"""

    @abstractmethod
    def get_event_status_counts(self, repo_keys: list, agent_type: str) -> list:
        """某类 Agent 在仓库集合内的事件状态分布 [(status, count)]。"""

    @abstractmethod
    def search_events(self, repo_keys: list, agent_type: str, keyword: str = "",
                      page: int = 1, page_size: int = 10) -> dict:
        """事件历史搜索 → {"total": int, "rows": list}。"""

    @abstractmethod
    def get_credential_health_rows(self) -> list:
        """全部未吊销凭据的 (name, status, last_seen_at)（DevStage 环境列表用）。"""

    @abstractmethod
    def get_inflight_task_count(self, repo_keys: list) -> int:
        """这批仓库上的在制任务数（queued/dispatched/running 三态之和）。"""

    @abstractmethod
    def get_issue_card(self, repo_key: str, issue_number: int) -> dict:
        """A Issue 卡片状态（issue 单表优先，字段已归一）。

        与 get_issue_state 的区别：后者只读 issue_state 过渡表；本方法读**权威** issue 单表，
        返回前端卡片需要的字段集合（stage / owner_agent / a2a_seq / assignee / scene_* /
        parent_issue_id / is_subtask_mode / allow_scene_override）。语义与
        api.session_store.read_issue_state 的第一段完全一致，供其迁移到 provider 通道。
        """

    @abstractmethod
    def update_issue_state(self, repo_key: str, issue_number: int, patch: dict) -> Any:
        """A 用户操作推进 Issue 状态（Phase 5 细化事务边界）。"""

    @abstractmethod
    def get_chat_conversations(self, repo_key: str, limit: int = 50) -> list:
        """A 用户面聊天对话列表（v2.2：chat 表归运行域，Mgmt 经本接口读）。

        返回 chat_conversations 行列表。
        """

    @abstractmethod
    def get_chat_messages(self, conv_id: str, limit: int = 100, before: int = 0) -> list:
        """A 用户面聊天消息分页（Mgmt SSE 流/历史展示）。

        返回 chat_messages 行列表（按 id 倒序取 limit，before>0 时取 id<before）。
        """

    # ---- P2 服务接口补齐（N1/N2/N3/N4/N5/N11/N12） ----

    @abstractmethod
    def list_skills(self, repo_key: str = "", q: str = "", limit: int = 50,
                    offset: int = 0) -> list:
        """N1 技能列表（mgmt/api 读）：不返回 OBS 原件地址。"""

    @abstractmethod
    def get_skill_detail(self, skill_id: int) -> dict:
        """N2 技能详情：N1 字段 + `content_url`（短时效签名）。"""

    @abstractmethod
    def list_engine_capabilities(self, limit: int = 500) -> list:
        """N3 引擎×能力矩阵一次全量（< 500 行）。"""

    @abstractmethod
    def get_runtime_credential_meta(self, runtime_id: int) -> dict:
        """N4 运行凭据元数据（**不含 token 明文**）。"""

    @abstractmethod
    def get_meta_namespace(self, namespace: str) -> dict:
        """N5 KV 元数据批量读（仅白名单 namespace）。"""

    @abstractmethod
    def get_tasks_batch(self, task_ids: list) -> list:
        """N11 批量任务摘要（列表页一屏一次，替代 N+1）。"""

    @abstractmethod
    def get_task_stats(self, repo_key: str = "", since: str = "") -> dict:
        """N12 任务聚合统计（替代洞察侧宽 SQL）。"""

    # ---- Issue #3097：占位账号业务数据判定 / 引擎引用对账 ----

    @abstractmethod
    def user_business_table_counts(self, user_id: Any) -> dict:
        """N14 某账号在本域的业务数据按表计数（mgmt 占位账号合并判定用）。

        返回 ``{表名: {"count": int}}``；缺表/不可读返回 ``{"count": None, ...}``
        （缺表语义由调用方决定：定稿为「视为无数据 + 结构化告警」）。
        """

    @abstractmethod
    def reconcile_engine_references(self, runtime_ids: list, keep_id: int = 0,
                                    reason: str = "") -> dict:
        """N15 引擎行删除/去重前的运行态引用对账（跨域写，幂等可重放）。

        对账语义见 ``runtime.engine.refs_reconcile``；mgmt 进程删用户/合并账号时调用。
        """

    @abstractmethod
    def transfer_user_business_data(self, from_user_id: Any, to_user_id: Any) -> dict:
        """N16 把某账号在本域的业务数据归属转到另一账号（占位账号自动合并，幂等）。

        返回 ``{表名: 受影响行数}``；任一表失败抛异常（调用方据此回滚身份转移）。
        """
