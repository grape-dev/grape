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

# === 派发前错误事件（Issue #657）===
# 派发前技能解析失败（如 repo 级场景引用 user 级技能，scope 不匹配）→
# 立即产生该错误事件（status=failed）并阻断派发，替代 pending_self_report 1800s 超时
SKILL_RESOLVE_FAILED = "skill_resolve_failed"

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
    # 文件级变更（Issue #996）：各平台 commits/files 键归一为 files 维度
    "files": "files",
    "deleted_files": "files",
    "changed_files": "files",
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


# === 文件级变更清单（Issue #996）===
# 需要携带 files{added,modified,deleted} 的事件类型：产物清理/自动合入前清理
# 事件都落在这三类上（push、PR merged、PR updated）。
FILE_EVENT_TYPES = {PUSH, PR_MERGED, PR_UPDATED}

# added/modified 单桶上限；deleted 永不截断（审计需要完整删除路径，
# 见 Issue #996 AC：truncated=true 但 deleted 全量保留）。
FILES_BUCKET_LIMIT = 200

_STATUS_ADDED = {"added", "created", "new", "add", "create", "A"}
_STATUS_MODIFIED = {"modified", "changed", "updated", "edit", "modify", "M", "R", "renamed"}
_STATUS_DELETED = {"removed", "deleted", "delete", "D"}


def _file_path(item) -> str:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return ""
    return str(item.get("path") or item.get("filename") or item.get("name")
               or item.get("new_path") or "")


def _file_entry(item) -> dict:
    """文件条目统一为 {"path": str, "sha": optional}。"""
    path = _file_path(item)
    if not path:
        return {}
    entry = {"path": path}
    if isinstance(item, dict):
        sha = item.get("sha") or item.get("id") or item.get("blob_id")
        if sha:
            entry["sha"] = sha
    return entry


def _append_unique(bucket: list, entry: dict, seen: set) -> None:
    path = entry.get("path")
    if not path or path in seen:
        return
    seen.add(path)
    bucket.append(entry)


def _collect_from_commit(commit: dict, added, modified, deleted, seen) -> None:
    for key in ("added", "new", "added_files"):
        for item in commit.get(key) or []:
            _append_unique(added, _file_entry(item), seen)
    for key in ("modified", "changed", "modified_files"):
        for item in commit.get(key) or []:
            _append_unique(modified, _file_entry(item), seen)
    for key in ("removed", "deleted", "deleted_files"):
        for item in commit.get(key) or []:
            _append_unique(deleted, _file_entry(item), seen)


def _collect_from_file_item(item: dict, added, modified, deleted, seen) -> None:
    status = str(item.get("status") or item.get("change_type") or "").lower()
    entry = _file_entry(item)
    if not entry:
        return
    old_guess = item.get("old_path") or item.get("previous_filename") or ""
    if old_guess and old_guess != entry.get("path"):
        _append_unique(deleted, {"path": old_guess}, seen)
        _append_unique(added, entry, seen)
        return
    if status in _STATUS_DELETED or item.get("deleted_file") is True:
        _append_unique(deleted, entry, seen)
    elif status in _STATUS_ADDED or item.get("new_file") is True:
        _append_unique(added, entry, seen)
    elif status in _STATUS_MODIFIED or item.get("renamed_file") is True:
        # rename：旧路径进 deleted，新路径进 added，保证删除清单可用于守卫预检
        old = item.get("old_path") or item.get("previous_filename") or ""
        if old:
            _append_unique(deleted, {"path": old}, seen)
            _append_unique(added, entry, seen)
        else:
            _append_unique(modified, entry, seen)
    else:
        _append_unique(modified, entry, seen)


def _collect_normalized(source, added, modified, deleted, seen) -> None:
    for key, bucket in (("added", added), ("modified", modified), ("deleted", deleted)):
        values = source.get(key)
        if isinstance(values, list):
            for item in values:
                _append_unique(bucket, _file_entry(item), seen)


def normalize_files(source, limit: int = FILES_BUCKET_LIMIT) -> dict:
    """把各平台 payload / commits / files 归一为 files{added,modified,deleted}。

    支持输入：
    - dict 已含 added/modified/deleted（幂等复用）；
    - webhook payload（含 commits / files / changes）；
    - list（commit 列表或 file 列表）。

    返回 {"added": [{"path",...}], "modified": [...], "deleted": [...],
          "truncated": bool}。deleted 全量保留；仅 added/modified 受 limit 截断。
    """
    added: list = []
    modified: list = []
    deleted: list = []
    seen: set = set()
    try:
        if isinstance(source, list):
            for item in source:
                if not isinstance(item, dict):
                    continue
                if any(k in item for k in ("added", "modified", "removed", "deleted")):
                    _collect_from_commit(item, added, modified, deleted, seen)
                else:
                    _collect_from_file_item(item, added, modified, deleted, seen)
        elif isinstance(source, dict):
            _collect_normalized(source, added, modified, deleted, seen)
            for item in source.get("commits") or source.get("commits_list") or []:
                if isinstance(item, dict):
                    _collect_from_commit(item, added, modified, deleted, seen)
            for _obj_key in ("pull_request", "merge_request"):
                _pr_obj = source.get(_obj_key)
                if isinstance(_pr_obj, dict) and isinstance(_pr_obj.get("files"), list):
                    for item in _pr_obj["files"]:
                        if isinstance(item, dict):
                            _collect_from_file_item(item, added, modified, deleted, seen)
            files = source.get("files")
            if isinstance(files, dict):
                # 幂等复用已归一化形态 payload.files
                _collect_normalized(files, added, modified, deleted, seen)
            elif isinstance(files, list):
                for item in files:
                    if isinstance(item, dict):
                        _collect_from_file_item(item, added, modified, deleted, seen)
                    elif isinstance(item, str):
                        _append_unique(modified, {"path": item}, seen)
            changes = source.get("changes")
            if isinstance(changes, list):
                for item in changes:
                    if isinstance(item, dict):
                        _collect_from_file_item(item, added, modified, deleted, seen)
            # GitLab MR / GitCode diff：object_attributes.changes（dict 形态）
            oa = source.get("object_attributes")
            if isinstance(oa, dict) and isinstance(oa.get("changes"), list):
                for item in oa["changes"]:
                    if isinstance(item, dict):
                        _collect_from_file_item(item, added, modified, deleted, seen)
    except Exception:
        pass

    truncated = len(added) > limit or len(modified) > limit
    return {
        "added": added[:limit],
        "modified": modified[:limit],
        "deleted": deleted,
        "truncated": truncated,
    }


def has_file_changes(files: dict) -> bool:
    if not isinstance(files, dict):
        return False
    return bool(files.get("added") or files.get("modified") or files.get("deleted"))
