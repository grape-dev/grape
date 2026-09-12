"""事件路由 — 阶段矩阵 + 非 Issue 静态映射（替代 agent_type.subscribe_events 单级路由）。

多 VCS 扩展（Issue #49 multi-vcs）：事件类型从 vcs/event_types.py 导入统一规范。

见 vod-skill-upgrade-plan.md §3.0.3/§3.0.4：Issue/PR 事件按 issue_state 阶段矩阵判定；
非 Issue 事件（vod_signal/release/build/deploy 等）用静态映射兜底；订阅声明不再作为路由依据。
"""
from src.config import log
from src.vcs.event_types import (
    ALL_ISSUE_EVENTS, ALL_PR_EVENTS,
    ISSUE_OPENED, ISSUE_CLOSED, ISSUE_REOPENED, ISSUE_ASSIGNED,
    PR_CLOSED, PUSH, BUILD_START, BUILD_FAILURE, DEPLOY_START, DEPLOY_FINISH,
    RELEASE_CREATED, TAG_CREATED,
)

# 事件类型集合（阶段矩阵覆盖）—— 使用 event_types.py 统一规范
ISSUE_EVENT_TYPES = ALL_ISSUE_EVENTS
PR_EVENT_TYPES = ALL_PR_EVENTS

# 不路由的事件（由 event_rule L1 过滤或副作用短路处理）
NON_ROUTABLE_EVENTS = {ISSUE_CLOSED, ISSUE_REOPENED, ISSUE_ASSIGNED, PR_CLOSED, PUSH}

# PR 事件无关联 Issue / 无 issue_state 时的默认归属。
PR_DEFAULT_AGENT = ""

# 非 Issue 事件静态映射（兜底路由）
NON_ISSUE_EVENT_ROUTE = {
    "vod_signal": "vod",
    RELEASE_CREATED: "sre",
    TAG_CREATED: "sre",
    BUILD_START: "sre",
    BUILD_FAILURE: "sre",
    DEPLOY_START: "sre",
    "deploy_failure": "sre",
    DEPLOY_FINISH: "sre",
    "onboarding_exp": "vod",
    "onboarding_explore": "vod",
    "onboarding_issue": "vod",
}


def get_domain_agent(event_type: str) -> str:
    """事件 → 默认归属 Agent（静态：阶段矩阵 + 非 Issue 映射，不读 subscribe_events）"""
    if event_type == ISSUE_OPENED:
        return "vod"
    if event_type in NON_ROUTABLE_EVENTS:
        return ""
    if event_type in ISSUE_EVENT_TYPES:
        return "issue"
    if event_type in PR_EVENT_TYPES:
        return PR_DEFAULT_AGENT
    return NON_ISSUE_EVENT_ROUTE.get(event_type, "")


def get_agent_skill_map() -> dict[str, str]:
    """返回 {event_type: agent_type} 映射，供前端 pipeline/Agent 页展示（静态源）"""
    result = {}
    for evt in ISSUE_EVENT_TYPES - {ISSUE_OPENED}:
        if evt not in NON_ROUTABLE_EVENTS:
            result[evt] = "issue"
    result[ISSUE_OPENED] = "vod"
    for evt in PR_EVENT_TYPES:
        if evt not in NON_ROUTABLE_EVENTS:
            result[evt] = PR_DEFAULT_AGENT
    for evt, agent in NON_ISSUE_EVENT_ROUTE.items():
        result[evt] = agent
    return result
