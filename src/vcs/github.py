"""GitHub VCS 后端实现 — 支持 GitHub Webhook 事件归一化"""
import json
from src.config import log
from .base import VcsBackend


class GitHubBackend(VcsBackend):
    name = "github"
    webhook_path = "/webhook/github"
    event_header_key = "X-GitHub-Event"

    def normalize(self, raw, event_header) -> dict:
        """GitHub Webhook → 统一事件格式"""
        payload = self.decode_payload(raw)
        evt_type = self._map_event_type(event_header)
        repo = self._extract_repo(payload)
        actor = self._extract_actor(payload, event_header)
        branch = self._extract_branch(payload)
        return {
            "source": "github",
            "type": evt_type,
            "repository": repo,
            "branch": branch,
            "actor": actor,
            "payload": payload,
        }

    def _map_event_type(self, header: str) -> str:
        m = {
            "push": "push",
            "issues": "issue_opened",
            "issue_comment": "issue_comment",
            "pull_request": "pull_request_opened",
            "pull_request_review": "pull_request_review",
            "pull_request_review_comment": "note_hook_pr",
            "release": "release_created",
            "create": "tag_created",
            "workflow_run": "build_start",
            "deployment": "deploy_start",
            "deployment_status": "deploy_finish",
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
