"""GitCode VCS 后端实现"""
from src.config import log
from .base import VcsBackend


class GitCodeBackend(VcsBackend):
    name = "gitcode"
    webhook_path = "/webhook/gitcode"
    event_header_key = "X-GitCode-Event"

    def normalize(self, raw, event_header) -> dict:
        payload = self.decode_payload(raw)
        event_type_map = {
            "push": "push", "issues": "issue_opened", "issue": "issue_opened",
            "Issue Hook": "issue_opened", "Note Hook": "issue_comment",
            "Merge Request Hook": "pull_request_merged", "pull_request": "pull_request_merged",
            "tag_push": "tag_created", "release": "release_created", "pipeline": "build_start",
        }
        evt_type = event_type_map.get(event_header, event_header)
        # 区分 Issue Hook 的动作
        if event_header == "Issue Hook":
            obj = payload.get("object_attributes", {})
            action = obj.get("action", "") if isinstance(obj, dict) else ""
            if action in ("open", "create"):
                evt_type = "issue_opened"
            else:
                evt_type = "issue_updated"
        # 区分 Note Hook 的类型（Issue 评论 vs PR 评论）
        if event_header == "Note Hook":
            obj = payload.get("object_attributes", {})
            noteable_type = obj.get("noteable_type", "") if isinstance(obj, dict) else ""
            if noteable_type == "MergeRequest":
                evt_type = "note_hook_pr"
            else:
                evt_type = "issue_comment"
        # 区分 merge / 非 merge 的 PR 事件（Issue #49 补充：按真实 action 细分，不再一律并成 opened）
        # open/create → pull_request_opened；merge → pull_request_merged；
        # update/reopen/close/审查(approve)等 → pull_request_updated，并上抛 action/actor/has_commits 供 L1 过滤。
        _pr_action = ""
        _pr_actor = ""
        _pr_has_commits = False
        if evt_type == "pull_request_merged":
            obj = payload.get("object_attributes", {})
            if isinstance(obj, dict):
                _pr_action = str(obj.get("action") or "").lower()
            else:
                _pr_action = "merge"
            if _pr_action == "merge":
                evt_type = "pull_request_merged"
            elif _pr_action in ("open", "create"):
                evt_type = "pull_request_opened"
            else:
                # update / reopen / close / 审查类(approve...) → pull_request_updated
                evt_type = "pull_request_updated"
                _pr_has_commits = bool(payload.get("commits"))
            # 操作者（供 L1 actor 判定）
            _pr_actor = payload.get("user_name", "")
            if not _pr_actor:
                _s = payload.get("sender", {})
                if isinstance(_s, dict):
                    _pr_actor = _s.get("username") or _s.get("login") or ""
        repo = ""
        if "project" in payload:
            p = payload["project"]
            if isinstance(p, dict):
                repo = p.get("path_with_namespace", p.get("full_name", p.get("name", "")))
        if not repo and "repository" in payload:
            r = payload["repository"]
            if isinstance(r, dict):
                repo = r.get("full_name", r.get("name", ""))
            elif isinstance(r, str):
                repo = r
        actor = payload.get("user_name", "")
        if not actor:
            sender = payload.get("sender", {})
            if isinstance(sender, dict):
                actor = sender.get("username", sender.get("login", ""))
        if not actor:
            # 评论类事件：顶层 user 是评论者/操作者，优先于 issue.author（issue 创建者）
            user = payload.get("user", {})
            if isinstance(user, dict):
                actor = user.get("username", user.get("login", ""))
        if not actor:
            issue = payload.get("issue", {})
            if isinstance(issue, dict):
                author = issue.get("author", {})
                if isinstance(author, dict):
                    actor = author.get("username", "")
        branch = ""
        ref = payload.get("ref", "")
        if isinstance(ref, str) and ref.startswith("refs/heads/"):
            branch = ref[11:]
        # 提取 Issue/PR 标题（webhook payload），供 event_pipeline 写入 events.title
        title = ""
        _obj = payload.get("object_attributes", {})
        if isinstance(_obj, dict):
            title = _obj.get("title", "") or ""
        if not title:
            _issue = payload.get("issue", {})
            if isinstance(_issue, dict):
                title = _issue.get("title", "") or ""
        if not title:
            _pr = payload.get("pull_request", {}) or payload.get("merge_request", {})
            if isinstance(_pr, dict):
                title = _pr.get("title", "") or ""
        evt = {
            "source": "gitcode", "type": evt_type,
            "repository": repo, "branch": branch,
            "actor": actor, "payload": payload, "title": title,
        }
        # Issue #49 补充：PR 事件上抛 action/actor/has_commits（供 L1 action 过滤/L2 分派对账）
        if evt_type in ("pull_request_opened", "pull_request_updated", "pull_request_merged"):
            evt["pr_action"] = _pr_action
            evt["pr_actor"] = _pr_actor
            evt["has_commits"] = _pr_has_commits
        return evt

    def get_collaborators_url(self, owner: str, repo: str) -> str:
        return f"https://api.gitcode.com/api/v5/repos/{owner}/{repo}/collaborators"
