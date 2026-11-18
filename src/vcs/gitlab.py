"""GitLab VCS 后端实现 — 支持 GitLab Webhook 事件归一化

同时提供 GitLab 实例级基础配置工具（自建 GitLab 需通过 GITLAB_BASE_URL /
meta gitlab_base_url / oauth_providers_config.gitlab.base_url 配置实例地址）。
"""
import json
import os
import urllib.parse

from src.config import log
from .base import VcsBackend
from .event_types import (
    ISSUE_OPENED, ISSUE_UPDATED, ISSUE_CLOSED, ISSUE_REOPENED,
    ISSUE_ASSIGNED, ISSUE_LABELED, ISSUE_COMMENT,
    PR_OPENED, PR_UPDATED, PR_CLOSED, PR_MERGED, PR_COMMENT,
    PUSH, TAG_CREATED, RELEASE_CREATED, BUILD_START,
    normalize_changes, normalize_files, has_file_changes, FILE_EVENT_TYPES,
)

_DEFAULT_GITLAB_BASE = "https://gitlab.com"


def get_gitlab_base_url() -> str:
    """获取 GitLab 实例根地址（自建实例可配），默认 gitlab.com。"""
    _env = (os.environ.get("GITLAB_BASE_URL") or "").strip()
    if _env:
        return _env.rstrip("/")
    try:
        from src.user import db as _user_db
        _meta = (_user_db.get_meta_value("gitlab_base_url") or "").strip()
        if _meta:
            return _meta.rstrip("/")
        _raw = _user_db.get_meta_value("oauth_providers_config") or ""
        if _raw:
            _cfg = (json.loads(_raw) or {}).get("gitlab") or {}
            _base = (_cfg.get("base_url") or "").strip()
            if _base:
                return _base.rstrip("/")
            _user_url = (_cfg.get("user_url") or "").strip()
            if "/api/" in _user_url:
                return _user_url.split("/api/", 1)[0].rstrip("/")
    except Exception as e:
        log.warning(f"get_gitlab_base_url: read config failed, fallback default: {e}")
    return _DEFAULT_GITLAB_BASE


def get_gitlab_api_base() -> str:
    """GitLab REST API v4 根地址。"""
    return get_gitlab_base_url() + "/api/v4"


def gitlab_project_path(owner: str, repo: str) -> str:
    """GitLab 项目 ID：整段 owner/repo 做 URL 编码（支持 group/subgroup/repo 嵌套命名空间）。"""
    return urllib.parse.quote(f"{owner}/{repo}", safe="")


class GitLabBackend(VcsBackend):
    name = "gitlab"
    webhook_path = "/webhook/gitlab"
    event_header_key = "X-Gitlab-Event"

    def normalize(self, raw, event_header) -> dict:
        """GitLab Webhook → 统一事件格式"""
        payload = self.decode_payload(raw)

        object_kind = payload.get("object_kind", "")
        if not object_kind:
            # 自建实例 / 反向代理可能不带 object_kind，用 X-Gitlab-Event 头兜底
            object_kind = self._kind_from_header(event_header)
        obj = payload.get("object_attributes", {})
        action = (obj.get("action") or "").lower() if isinstance(obj, dict) else ""

        raw_changes = payload.get("changes", {})
        changes = normalize_changes(raw_changes) if isinstance(raw_changes, dict) else {}

        evt_type = self._map_event_type(object_kind, action, changes, payload)

        repo = self._extract_repo(payload)
        actor = self._extract_actor(payload)
        branch = self._extract_branch(payload)
        title = obj.get("title", "") if isinstance(obj, dict) else ""

        payload["_raw_action"] = action
        payload["_changes"] = changes

        # Issue #996：变更类事件携带文件清单（payload 自带 commits/changes 时直接归一）
        if evt_type in FILE_EVENT_TYPES:
            _files = normalize_files(payload)
            if has_file_changes(_files):
                payload["files"] = _files

        # 统一编号：note 事件优先 noteable_iid（iid 是评论自身 id，不是 Issue 号）
        _oa = payload.get("object_attributes") if isinstance(payload.get("object_attributes"), dict) else {}
        _target = payload.get("issue") or payload.get("merge_request") or {}
        if not isinstance(_target, dict):
            _target = {}
        _iid = (_oa.get("noteable_iid") or _target.get("iid")
                or _oa.get("iid") or _oa.get("number") or "")

        return {
            "source": "gitlab",
            "type": evt_type,
            "repository": repo,
            "branch": branch,
            "actor": actor,
            "payload": payload,
            "title": title,
            "object_iid": str(_iid) if _iid else "",
        }

    @staticmethod
    def _kind_from_header(header: str) -> str:
        """X-Gitlab-Event 头 → object_kind（object_kind 缺失时的兜底）。"""
        h = str(header or "").lower()
        if "merge request" in h:
            return "merge_request"
        if "note" in h or "comment" in h:
            return "note"
        if "issue" in h:
            return "issue"
        if "tag" in h:
            return "tag_push"
        if "push" in h:
            return "push"
        if "pipeline" in h or "build" in h:
            return "pipeline"
        if "release" in h:
            return "release"
        return ""

    def _map_event_type(self, object_kind: str, action: str, changes: dict, payload: dict) -> str:
        # Issue events
        if object_kind == "issue":
            if action == "open": return ISSUE_OPENED
            if action == "close": return ISSUE_CLOSED
            if action == "reopen": return ISSUE_REOPENED
            if action == "update":
                if "assignees" in changes or "assignee_id" in changes:
                    return ISSUE_ASSIGNED
                if "labels" in changes:
                    return ISSUE_LABELED
                return ISSUE_UPDATED
            return ISSUE_UPDATED

        # Note (comment) events：commit note / snippet note 不应冒充 Issue 评论
        if object_kind == "note":
            obj = payload.get("object_attributes", {})
            noteable_type = str(obj.get("noteable_type", "")).lower() if isinstance(obj, dict) else ""
            if noteable_type == "mergerequest":
                return PR_COMMENT
            if noteable_type == "issue":
                return ISSUE_COMMENT
            return "unknown"

        # Merge request events：approved / unapproved / approval 等未知 action
        # 绝不能默认成 PR_OPENED，否则每次审批都会重复触发建任务
        if object_kind == "merge_request":
            if action == "merge": return PR_MERGED
            if action == "open": return PR_OPENED
            if action == "update": return PR_UPDATED
            if action == "close": return PR_CLOSED
            if action == "reopen": return PR_OPENED
            return "unknown"

        # Other events
        if object_kind == "push": return PUSH
        if object_kind == "tag_push": return TAG_CREATED
        if object_kind == "release": return RELEASE_CREATED
        if object_kind in ("pipeline", "build"): return BUILD_START

        # 未映射类型返回 unknown（不再把 "build" 之类原生字符串当事件类型）
        return "unknown"

    def _extract_repo(self, payload: dict) -> str:
        project = payload.get("project", {})
        if isinstance(project, dict):
            return project.get("path_with_namespace", project.get("full_name", ""))
        repo = payload.get("repository", {})
        if isinstance(repo, dict):
            return repo.get("full_name", repo.get("name", ""))
        return ""

    def _extract_actor(self, payload: dict) -> str:
        user = payload.get("user", {})
        if isinstance(user, dict):
            return user.get("username", user.get("name", ""))
        obj = payload.get("object_attributes", {})
        if isinstance(obj, dict):
            author = obj.get("author", {})
            if isinstance(author, dict):
                return author.get("username", author.get("name", ""))
        return ""

    def _extract_branch(self, payload: dict) -> str:
        ref = payload.get("ref", "")
        if isinstance(ref, str) and ref.startswith("refs/heads/"):
            return ref[11:]
        return ""

    def get_collaborators_url(self, owner: str, repo: str) -> str:
        return f"{get_gitlab_api_base()}/projects/{gitlab_project_path(owner, repo)}/members"
