"""GitCode VCS 后端实现"""
from src.config import log
from .base import VcsBackend
from .event_types import (
    ISSUE_OPENED, ISSUE_UPDATED, ISSUE_CLOSED, ISSUE_REOPENED,
    ISSUE_ASSIGNED, ISSUE_LABELED, ISSUE_COMMENT,
    PR_OPENED, PR_UPDATED, PR_CLOSED, PR_MERGED, PR_REVIEW, PR_COMMENT,
    PUSH, TAG_CREATED, RELEASE_CREATED, BUILD_START,
    normalize_changes,
)


class GitCodeBackend(VcsBackend):
    name = "gitcode"
    webhook_path = "/webhook/gitcode"
    event_header_key = "X-GitCode-Event"

    def normalize(self, raw, event_header) -> dict:
        payload = self.decode_payload(raw)
        event_type_map = {
            "push": PUSH, "issues": ISSUE_OPENED, "issue": ISSUE_OPENED,
            "Issue Hook": ISSUE_OPENED, "Note Hook": ISSUE_COMMENT,
            "Merge Request Hook": PR_MERGED, "pull_request": PR_MERGED,
            "tag_push": TAG_CREATED, "release": RELEASE_CREATED, "pipeline": BUILD_START,
        }
        evt_type = event_type_map.get(event_header, event_header)

        # 提取 action 和 changes
        obj = payload.get("object_attributes", {})
        action = obj.get("action", "") if isinstance(obj, dict) else ""
        raw_changes = payload.get("changes", {})
        changes = normalize_changes(raw_changes) if isinstance(raw_changes, dict) else {}

        # Issue Hook action 细分
        if event_header == "Issue Hook":
            if action in ("open", "create"):
                evt_type = ISSUE_OPENED
            elif action == "close":
                evt_type = ISSUE_CLOSED
            elif action == "reopen":
                evt_type = ISSUE_REOPENED
            elif action == "update":
                if "assignees" in changes:
                    evt_type = ISSUE_ASSIGNED
                elif "labels" in changes:
                    evt_type = ISSUE_LABELED
                else:
                    evt_type = ISSUE_UPDATED
            else:
                evt_type = ISSUE_UPDATED

        # Note Hook: 区分 Issue 评论 vs PR 评论
        if event_header == "Note Hook":
            noteable_type = obj.get("noteable_type", "") if isinstance(obj, dict) else ""
            evt_type = PR_COMMENT if noteable_type == "MergeRequest" else ISSUE_COMMENT

        # Merge Request Hook action 细分
        if event_header in ("Merge Request Hook", "pull_request"):
            if action == "merge":
                evt_type = PR_MERGED
            elif action == "open":
                evt_type = PR_OPENED
            elif action == "update":
                evt_type = PR_UPDATED
            elif action == "close":
                evt_type = PR_CLOSED
            elif action == "reopen":
                evt_type = PR_OPENED
            elif action in ("review", "approve"):
                # 审查/通过审查：独立事件，避免被归入 PR_OPENED 误触发执行 agent
                evt_type = PR_REVIEW
            else:
                evt_type = PR_OPENED

        repo = self._extract_repo(payload)
        actor = self._extract_actor(payload)
        branch = self._extract_branch(payload)
        title = self._extract_title(payload)

        # 保留原始 action 和 normalized changes 供 event_rule 使用
        payload["_raw_action"] = action
        payload["_changes"] = changes

        return {
            "source": "gitcode", "type": evt_type,
            "repository": repo, "branch": branch,
            "actor": actor, "payload": payload, "title": title,
        }

    def _extract_repo(self, payload: dict) -> str:
        if "project" in payload:
            p = payload["project"]
            if isinstance(p, dict):
                return p.get("path_with_namespace", p.get("full_name", p.get("name", "")))
        if "repository" in payload:
            r = payload["repository"]
            if isinstance(r, dict):
                return r.get("full_name", r.get("name", ""))
            elif isinstance(r, str):
                return r
        return ""

    def _extract_actor(self, payload: dict) -> str:
        actor = payload.get("user_name", "")
        if not actor:
            sender = payload.get("sender", {})
            if isinstance(sender, dict):
                actor = sender.get("username", sender.get("login", ""))
        if not actor:
            user = payload.get("user", {})
            if isinstance(user, dict):
                actor = user.get("username", user.get("login", ""))
        if not actor:
            issue = payload.get("issue", {})
            if isinstance(issue, dict):
                author = issue.get("author", {})
                if isinstance(author, dict):
                    actor = author.get("username", "")
        return actor

    def _extract_branch(self, payload: dict) -> str:
        ref = payload.get("ref", "")
        if isinstance(ref, str) and ref.startswith("refs/heads/"):
            return ref[11:]
        return ""

    def _extract_title(self, payload: dict) -> str:
        _obj = payload.get("object_attributes", {})
        if isinstance(_obj, dict):
            t = _obj.get("title", "")
            if t:
                return t
        _issue = payload.get("issue", {})
        if isinstance(_issue, dict):
            t = _issue.get("title", "")
            if t:
                return t
        _pr = payload.get("pull_request", {}) or payload.get("merge_request", {})
        if isinstance(_pr, dict):
            t = _pr.get("title", "")
            if t:
                return t
        return ""

    def get_collaborators_url(self, owner: str, repo: str) -> str:
        return f"https://api.gitcode.com/api/v5/repos/{owner}/{repo}/collaborators"
