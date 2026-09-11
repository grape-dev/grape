"""GitHub VCS 后端实现 — 支持 GitHub Webhook 事件归一化"""
import json
from src.config import log
from .base import VcsBackend
from .event_types import (
    ISSUE_OPENED, ISSUE_UPDATED, ISSUE_CLOSED, ISSUE_REOPENED,
    ISSUE_ASSIGNED, ISSUE_LABELED, ISSUE_COMMENT,
    PR_OPENED, PR_UPDATED, PR_CLOSED, PR_MERGED, PR_REVIEW, PR_COMMENT,
    PUSH, TAG_CREATED, RELEASE_CREATED, BUILD_START, DEPLOY_START, DEPLOY_FINISH,
    normalize_changes,
)


class GitHubBackend(VcsBackend):
    name = "github"
    webhook_path = "/webhook/github"
    event_header_key = "X-GitHub-Event"

    def normalize(self, raw, event_header) -> dict:
        """GitHub Webhook → 统一事件格式"""
        payload = self.decode_payload(raw)

        # 提取 action（GitHub events 通常有 top-level action）
        action = (payload.get("action") or "").lower()

        # 提取 changes 并归一化
        raw_changes = payload.get("changes", {})
        changes = normalize_changes(raw_changes) if isinstance(raw_changes, dict) else {}

        evt_type = self._map_event_type(event_header, action, payload, changes)

        repo = self._extract_repo(payload)
        actor = self._extract_actor(payload, event_header)
        branch = self._extract_branch(payload)
        title = self._extract_title(payload, event_header)

        # 保留原始 action 和 normalized changes
        payload["_raw_action"] = action
        payload["_changes"] = changes

        return {
            "source": "github",
            "type": evt_type,
            "repository": repo,
            "branch": branch,
            "actor": actor,
            "payload": payload,
            "title": title,
        }

    def _map_event_type(self, header: str, action: str, payload: dict, changes: dict) -> str:
        # Issue events
        if header == "issues":
            if action == "opened": return ISSUE_OPENED
            if action == "closed": return ISSUE_CLOSED
            if action == "reopened": return ISSUE_REOPENED
            if action == "assigned": return ISSUE_ASSIGNED
            if action == "labeled": return ISSUE_LABELED
            if action == "edited": return ISSUE_UPDATED
            if action == "unassigned": return ISSUE_ASSIGNED
            if action == "unlabeled": return ISSUE_LABELED
            return ISSUE_UPDATED

        # Issue comment
        if header == "issue_comment":
            return ISSUE_COMMENT

        # Pull request events
        if header == "pull_request":
            pr = payload.get("pull_request", {})
            merged = pr.get("merged", False) if isinstance(pr, dict) else False
            if action == "closed" and merged: return PR_MERGED
            if action == "closed" and not merged: return PR_CLOSED
            if action == "opened" or action == "reopened": return PR_OPENED
            if action == "edited": return PR_UPDATED
            if action == "labeled": return PR_OPENED
            if action == "synchronize": return PR_UPDATED
            return PR_OPENED

        # Pull request review
        if header == "pull_request_review":
            return PR_REVIEW

        # Pull request review comment
        if header == "pull_request_review_comment":
            return PR_COMMENT

        # Other events
        m = {
            "push": PUSH,
            "create": TAG_CREATED,
            "release": RELEASE_CREATED,
            "workflow_run": BUILD_START,
            "deployment": DEPLOY_START,
            "deployment_status": DEPLOY_FINISH,
        }
        return m.get(header, header)

    def _extract_repo(self, payload: dict) -> str:
        repo = payload.get("repository", {})
        if isinstance(repo, dict):
            return repo.get("full_name", repo.get("name", ""))
        return ""

    def _extract_actor(self, payload: dict, event_header: str) -> str:
        sender = payload.get("sender", {})
        if isinstance(sender, dict):
            return sender.get("login", sender.get("username", ""))
        issue = payload.get("issue", {})
        if isinstance(issue, dict):
            user = issue.get("user", {})
            if isinstance(user, dict):
                return user.get("login", "")
        return ""

    def _extract_branch(self, payload: dict) -> str:
        ref = payload.get("ref", "")
        if isinstance(ref, str) and ref.startswith("refs/heads/"):
            return ref[11:]
        pull = payload.get("pull_request", {})
        if isinstance(pull, dict):
            base = pull.get("base", {})
            if isinstance(base, dict):
                return base.get("ref", "")
        return ""

    def _extract_title(self, payload: dict, event_header: str) -> str:
        issue = payload.get("issue", {})
        if isinstance(issue, dict):
            t = issue.get("title", "")
            if t:
                return t
        pull = payload.get("pull_request", {})
        if isinstance(pull, dict):
            t = pull.get("title", "")
            if t:
                return t
        return ""

    def get_collaborators_url(self, owner: str, repo: str) -> str:
        return f"https://api.github.com/repos/{owner}/{repo}/collaborators"
