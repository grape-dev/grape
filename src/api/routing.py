"""事件路由 — 阶段矩阵 + 非 Issue 静态映射（替代 agent_type.subscribe_events 单级路由）。

见 vod-skill-upgrade-plan.md §3.0.3/§3.0.4：Issue/PR 事件按 issue_state 阶段矩阵判定；
非 Issue 事件（vod_signal/release/build/deploy 等）用静态映射兜底；订阅声明不再作为路由依据。
"""
from src.config import log

# 事件类型集合（阶段矩阵覆盖）
ISSUE_EVENT_TYPES = {"issue_opened", "issue_updated", "issue_comment", "issue_labeled"}
PR_EVENT_TYPES = {"pull_request_opened", "pull_request_updated", "pull_request_merged", "note_hook_pr"}

# PR 事件无关联 Issue / 无 issue_state 时的默认归属（PR 审查）
PR_DEFAULT_AGENT = "qa"

# 非 Issue 事件静态映射（兜底路由）
# push 显式忽略（开发由 issue 驱动，不触发 agent；如需刷新 project-info 可后续挂接）
# issue_labeled 与 issue_comment 一致，走阶段矩阵（ISSUE_EVENT_TYPES），不在此映射
NON_ISSUE_EVENT_ROUTE = {
    "vod_signal": "vod",
    "release_created": "sre",
    "tag_created": "sre",
    "build_start": "sre",
    "build_failure": "sre",
    "deploy_start": "sre",
    "deploy_failure": "sre",
}


def get_domain_agent(event_type: str) -> str:
    """事件 → 默认归属 Agent（静态：阶段矩阵 + 非 Issue 映射，不读 subscribe_events）"""
    if event_type == "issue_opened":
        return "vod"
    if event_type in ISSUE_EVENT_TYPES:
        return "issue"
    if event_type in PR_EVENT_TYPES:
        return PR_DEFAULT_AGENT
    return NON_ISSUE_EVENT_ROUTE.get(event_type, "")


def get_agent_skill_map() -> dict[str, str]:
    """返回 {event_type: agent_type} 映射，供前端 pipeline/Agent 页展示（静态源）"""
    result = {}
    for evt in ISSUE_EVENT_TYPES - {"issue_opened"}:
        result[evt] = "issue"
    result["issue_opened"] = "vod"          # D8：恒给 VOD
    for evt in PR_EVENT_TYPES:
        result[evt] = PR_DEFAULT_AGENT
    for evt, agent in NON_ISSUE_EVENT_ROUTE.items():
        result[evt] = agent
    return result
