"""统一 VCS API 客户端：跨平台 Issue/PR REST API 调用抽象。

支持 GitCode / GitHub / GitLab 三个平台，提供统一的 API 接口。
逐步替代 repogit.py 中分散的 API 调用。

参见 docs/weekly-02/issue-49-multi-vcs-extension-design.md §3.8
"""
import json
import urllib.request
import urllib.error
from src.config import log


class VcsApiClient:
    """统一 VCS API 客户端：Issue 评论 / PR 操作 / 文件读写。

    Args:
        provider: VCS 平台标识（gitcode/github/gitlab）
        token: API access token
        owner: 仓库所有者
        repo: 仓库名称
    """

    def __init__(self, provider: str, token: str, owner: str, repo: str):
        self.provider = provider
        self.token = token
        self.owner = owner
        self.repo = repo

    # === Issue 评论 ===

    def post_issue_comment(self, issue_number: int, body: str) -> dict:
        """在 Issue 上发表评论。"""
        if self.provider == "gitcode":
            return self._gitcode_request(
                "POST", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments",
                {"body": body})
        elif self.provider == "github":
            return self._github_request(
                "POST", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments",
                {"body": body})
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            return self._gitlab_request(
                "POST", f"/projects/{project_id}/issues/{issue_number}/notes",
                {"body": body})
        return {"error": f"unsupported provider: {self.provider}"}

    # === Issue 查询 ===

    def get_issue(self, issue_number: int) -> dict:
        """获取 Issue 详情。"""
        if self.provider == "gitcode":
            return self._gitcode_request(
                "GET", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}")
        elif self.provider == "github":
            return self._github_request(
                "GET", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}")
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            return self._gitlab_request(
                "GET", f"/projects/{project_id}/issues/{issue_number}")
        return {"error": f"unsupported provider: {self.provider}"}

    # === Issue 关闭 ===

    def close_issue(self, issue_number: int) -> dict:
        """关闭 Issue（设置 state=closed）。

        Returns:
            dict: API 返回的 Issue JSON，或 {"error": ...} 错误信息。
        """
        if self.provider == "gitcode":
            # GitCode v5（Gitee 系）状态迁移字段为 state_event（reopen/close），非 GitHub 的 state=closed
            resp = self._gitcode_request(
                "PATCH", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}",
                {"state_event": "close"})
            # GitCode v5 已知缺陷：PATCH 返回 200 但其实现不迁移 issue state（有校验无应用），
            # 关闭后主动校验，避免把"未关闭"误报为成功
            if isinstance(resp, dict) and "error" not in resp:
                _st = self.get_issue_state(issue_number)
                if _st and _st != "closed":
                    return {"error": "gitcode_issue_close_not_effective",
                            "state": _st,
                            "detail": "GitCode v5 PATCH issues 校验但未应用 state_event（平台侧缺陷），请在该仓库页面手动关闭 Issue"}
            return resp
        elif self.provider == "github":
            return self._github_request(
                "PATCH", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}",
                {"state": "closed"})
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            return self._gitlab_request(
                "PUT", f"/projects/{project_id}/issues/{issue_number}",
                {"state_event": "close"})
        return {"error": f"unsupported provider: {self.provider}"}

    def get_issue_state(self, issue_number: int) -> str:
        """获取 Issue 当前 state（open/closed），失败返回空串。"""
        resp = self.get_issue(issue_number)
        if isinstance(resp, dict) and "error" not in resp:
            return str(resp.get("state", "")).lower()
        return ""

    # === PR 操作 ===

    def create_pr(self, title: str, head: str, base: str, body: str = "") -> dict:
        """创建 Pull Request。"""
        data = {"title": title, "head": head, "base": base, "body": body}
        if self.provider == "gitcode":
            return self._gitcode_request(
                "POST", f"/repos/{self.owner}/{self.repo}/pulls", data)
        elif self.provider == "github":
            return self._github_request(
                "POST", f"/repos/{self.owner}/{self.repo}/pulls", data)
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            return self._gitlab_request(
                "POST", f"/projects/{project_id}/merge_requests", data)
        return {"error": f"unsupported provider: {self.provider}"}

    def merge_pr(self, pr_number: int) -> dict:
        """合并 Pull Request。"""
        if self.provider == "gitcode":
            return self._gitcode_request(
                "PUT", f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/merge", {})
        elif self.provider == "github":
            return self._github_request(
                "PUT", f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/merge", {})
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            return self._gitlab_request(
                "PUT", f"/projects/{project_id}/merge_requests/{pr_number}/merge", {})
        return {"error": f"unsupported provider: {self.provider}"}

    def get_pr(self, pr_number: int) -> dict:
        """获取 PR 详情。"""
        if self.provider == "gitcode":
            return self._gitcode_request(
                "GET", f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}")
        elif self.provider == "github":
            return self._github_request(
                "GET", f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}")
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            return self._gitlab_request(
                "GET", f"/projects/{project_id}/merge_requests/{pr_number}")
        return {"error": f"unsupported provider: {self.provider}"}

    # === 仓库信息 ===

    def get_repo_info(self) -> dict:
        """获取仓库信息。"""
        if self.provider == "gitcode":
            return self._gitcode_request("GET", f"/repos/{self.owner}/{self.repo}")
        elif self.provider == "github":
            return self._github_request("GET", f"/repos/{self.owner}/{self.repo}")
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            return self._gitlab_request("GET", f"/projects/{project_id}")
        return {"error": f"unsupported provider: {self.provider}"}

    # === 平台内部实现 ===

    def _gitcode_request(self, method: str, path: str, data: dict | None = None) -> dict:
        # GitCode v5（Gitee 系）认证用 ?access_token= 查询参数，Authorisation header 会 401 "token not found"
        url = f"https://gitcode.com/api/v5{path}"
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}access_token={self.token}"
        return self._do_request(url, method, data, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _github_request(self, method: str, path: str, data: dict | None = None) -> dict:
        url = f"https://api.github.com{path}"
        return self._do_request(url, method, data, headers={
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        })

    def _gitlab_request(self, method: str, path: str, data: dict | None = None) -> dict:
        url = f"https://gitlab.com/api/v4{path}"
        return self._do_request(url, method, data, headers={
            "PRIVATE-TOKEN": self.token,
            "Content-Type": "application/json",
        })

    @staticmethod
    def _do_request(url: str, method: str, data: dict | None = None, headers: dict | None = None) -> dict:
        """执行 HTTP 请求，返回 JSON dict。"""
        try:
            body = json.dumps(data).encode("utf-8") if data else None
            req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")[:500]
            except Exception:
                pass
            log.warning(f"VcsApiClient: {method} {url} → HTTP {e.code}: {err_body}")
            return {"error": f"HTTP {e.code}", "detail": err_body}
        except Exception as e:
            log.warning(f"VcsApiClient: {method} {url} → {e}")
            return {"error": str(e)}


def create_client(repo_key: str, token: str = "") -> "VcsApiClient":
    """从 repo_key 创建 VcsApiClient。

    repo_key 格式: "gitcode:owner/repo" / "github:owner/repo" / "gitlab:owner/repo"
    """
    from src.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    if not token:
        # 从 DB 获取 token
        try:
            from src.user.db import get_gitcode_token
            token = get_gitcode_token(repo_key=repo_key) or ""
        except Exception:
            pass
    return VcsApiClient(provider, token, owner, repo)
