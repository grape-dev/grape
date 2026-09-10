"""跨平台统一事件类型规范（Canonical Event Types）。

所有 VCS backend 归一化时必须映射到这些类型。
不同平台的 webhook payload 结构不同，但归一化后 event_type 统一。

参见 docs/weekly-02/issue-49-multi-vcs-extension-design.md §3.1
"""

# === Issue 事件 ===
ISSUE_OPENED    = "issue_opened"
ISSUE_UPDATED   = "issue_updated"      # 内容更新（title/description 变更）
ISSUE_CLOSED    = "issue_closed"
ISSUE_REOPENED  = "issue_reopened"
ISSUE_ASSIGNED  = "issue_assigned"
ISSUE_LABELED   = "issue_labeled"
ISSUE_COMMENT   = "issue_comment"

# === PR 事件 ===
PR_OPENED       = "pull_request_opened"
PR_UPDATED      = "pull_request_updated"
PR_CLOSED       = "pull_request_closed"
PR_MERGED       = "pull_request_merged"
PR_REVIEW       = "pull_request_review"
PR_COMMENT      = "note_hook_pr"

# === 其他 ===
PUSH            = "push"
TAG_CREATED     = "tag_created"
RELEASE_CREATED = "release_created"
BUILD_START     = "build_start"
BUILD_FAILURE   = "build_failure"
DEPLOY_START    = "deploy_start"
DEPLOY_FINISH   = "deploy_finish"

# 所有需要阶段矩阵路由的事件
ROUTABLE_EVENTS = {
    ISSUE_OPENED, ISSUE_UPDATED, ISSUE_COMMENT, ISSUE_LABELED,
    PR_OPENED, PR_UPDATED, PR_MERGED, PR_COMMENT, PR_REVIEW,
}

# 不需要 Agent 处理的事件（直接过滤，不创建 event）
SKIP_EVENTS = {
    ISSUE_CLOSED, ISSUE_REOPENED, ISSUE_ASSIGNED,
    PR_CLOSED, PUSH,
}

# 副作用事件（执行副作用后短路，不进入 Agent）
SIDE_EFFECT_EVENTS = {
    ISSUE_CLOSED,  # → close_issue_tasks
}

# 所有 Issue 事件
ALL_ISSUE_EVENTS = {
    ISSUE_OPENED, ISSUE_UPDATED, ISSUE_CLOSED, ISSUE_REOPENED,
    ISSUE_ASSIGNED, ISSUE_LABELED, ISSUE_COMMENT,
}

# 所有 PR 事件
ALL_PR_EVENTS = {
    PR_OPENED, PR_UPDATED, PR_CLOSED, PR_MERGED, PR_REVIEW, PR_COMMENT,
}

# 统一 changes 键名映射（各平台 → canonical）
CHANGES_KEY_MAP = {
    # GitCode
    "description": "description",
    "title": "title",
    "assignees": "assignees",
    "labels": "labels",
    # GitHub
    "body": "description",
    "assignee": "assignees",
    "label": "labels",
    # GitLab
    "assignee_id": "assignees",
    "assignee_ids": "assignees",
}


def normalize_changes(raw_changes: dict) -> dict:
    """将平台专有 changes 键名统一为 canonical 键名。"""
    if not isinstance(raw_changes, dict):
        return {}
    result = {}
    for k, v in raw_changes.items():
        canonical_key = CHANGES_KEY_MAP.get(k, k)
        result[canonical_key] = v
    return result
