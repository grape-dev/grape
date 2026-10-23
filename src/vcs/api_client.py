"""统一 VCS API 客户端：跨平台 Issue/PR REST API 调用抽象。

支持 GitCode / GitHub / GitLab 三个平台，提供统一的 API 接口。
逐步替代 repogit.py 中分散的 API 调用。

参见 docs/weekly-02/issue-49-multi-vcs-extension-design.md §3.8
"""
import json
import re
import urllib.request
import urllib.error
from src.config import log


# Issue #315: GitCode 编辑器上传图片（raw.gitcode.com/user-images/...）链接识别与鉴权下载。
# 该类资源匿名拉取 403，需带 token；提取为纯函数便于单测。
_USER_IMAGE_URL_RE = re.compile(r"https://raw\.gitcode\.com/user-images/[^\s\)\"'<>]+")
_USER_IMAGE_URL_PREFIX = "https://raw.gitcode.com/user-images/"
USER_IMAGE_MAX_SIZE = 20 * 1024 * 1024   # 单图 20MB 上限
USER_IMAGE_TIMEOUT = 15                  # 单图下载 15s 超时
USER_IMAGE_MAX_COUNT = 8                 # 单 Issue 最多采集 8 张（超出降级说明）


def extract_gitcode_user_image_urls(text: "str | None") -> list[str]:
    """从 Markdown / HTML / 裸链文本中提取 raw.gitcode.com/user-images 图片 URL。

    覆盖形态：![alt](url)、<img src="url">、裸 URL。
    去重保序（首个出现顺序）。纯函数，无 IO；text 为 None/空返回 []。
    """
    if not text:
        return []
    urls = _USER_IMAGE_URL_RE.findall(text)
    return list(dict.fromkeys(urls))


def _download_user_image(url: str, token: str, max_size: int = USER_IMAGE_MAX_SIZE,
                         timeout: int = USER_IMAGE_TIMEOUT) -> dict:
    """带 token 下载单张 user-images 图片（内部实现，供 VcsApiClient 复用）。

    返回 {"content": bytes, "content_type": str, "size": int} 或 {"error": str}。
    token 仅构造 Authorization 请求头，绝不写入日志或返回值。
    """
    if not url.startswith(_USER_IMAGE_URL_PREFIX):
        return {"error": "url not allowed (only raw.gitcode.com/user-images)"}
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "image/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                # 非图片（如 403 返回 JSON 的场景已被 HTTPError 分支拦截，这里防御其他 200 非图）
                return {"error": f"non-image content-type: {ctype or 'unknown'}"}
            chunks = []
            total = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_size:
                    return {"error": f"image exceeds size limit ({max_size} bytes)"}
                chunks.append(chunk)
            if total == 0:
                return {"error": "empty image content"}
            content = b"".join(chunks)
            return {"content": content, "content_type": ctype, "size": total}
    except urllib.error.HTTPError as e:
        log.warning(f"user-image download failed: {url} -> HTTP {e.code}")
        return {"error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        _reason = getattr(e, "reason", e)
        if "timed out" in str(_reason).lower() or "timeout" in str(_reason).lower():
            log.warning(f"user-image download failed: {url} -> timeout ({timeout}s)")
            return {"error": "timeout"}
        log.warning(f"user-image download failed: {url} -> {_reason}")
        return {"error": str(_reason)}
    except Exception as e:
        log.warning(f"user-image download failed: {url} -> {e}")
        return {"error": str(e)}


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

    def _ledger(self, content_type: str, content_id, issue_number, body: str = "") -> None:
        """Issue #453 代发台账（写点8，客户端层收口）：写操作成功后入账，best-effort。

        在 VcsApiClient 四个写方法（评论/关issue/建PR/合PR）统一调用——所有调用方
        （git_repo_api / issues.py / issues_center / create_pr_for_repo）自动覆盖。
        content_id：字符串/数字 ID（评论 ID / issue 编号 / PR 编号）。
        """
        try:
            from src.outbound_ledger import record_outbound, content_hash_md5
            from src.repo_key import to_repo_key
            if content_id is None or str(content_id) == "":
                return
            _rk = to_repo_key(f"{self.owner}/{self.repo}", self.provider)
            record_outbound(self.provider, _rk, content_type, str(content_id),
                            issue_number=str(issue_number or ""),
                            content_hash=content_hash_md5(body) if body else "")
        except Exception as e:
            from src.config import log
            log.warning(f"VcsApiClient ledger record failed (non-blocking): {e}")

    def post_issue_comment(self, issue_number: int, body: str) -> dict:
        """在 Issue 上发表评论。"""
        if self.provider == "gitcode":
            _resp = self._gitcode_request(
                "POST", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments",
                {"body": body})
        elif self.provider == "github":
            _resp = self._github_request(
                "POST", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments",
                {"body": body})
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            _resp = self._gitlab_request(
                "POST", f"/projects/{project_id}/issues/{issue_number}/notes",
                {"body": body})
        else:
            return {"error": f"unsupported provider: {self.provider}"}
        # Issue #453 台账：响应 dict 里的评论 ID 入账（无 ID 时记 issue_number 兼值）
        if isinstance(_resp, dict) and "error" not in _resp:
            from src.outbound_ledger import extract_content_id
            _cid = extract_content_id(_resp, "issue_comment")
            self._ledger("issue_comment", _cid or issue_number, issue_number, body)
        return _resp

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
            # GitCode v5 更新 Issue（docs.gitcode.com patch-api-v5-...-issues-number）：
            # body 必填 repo+title，关闭用 state=close（reopen 开启/close 关闭；GitHub 的 state=closed 会被拒）
            try:
                _info = self.get_issue(issue_number)
                _title = str(_info.get("title") or "") if isinstance(_info, dict) else ""
            except Exception:
                _title = ""
            _resp = self._gitcode_request(
                "PATCH", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}",
                {"state": "close", "repo": f"{self.owner}/{self.repo}", "title": _title})
        elif self.provider == "github":
            _resp = self._github_request(
                "PATCH", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}",
                {"state": "closed"})
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            _resp = self._gitlab_request(
                "PUT", f"/projects/{project_id}/issues/{issue_number}",
                {"state_event": "close"})
        else:
            return {"error": f"unsupported provider: {self.provider}"}
        # Issue #453 台账（写点10）：关闭 issue 入账——issue_closed 事件回流对账
        if isinstance(_resp, dict) and "error" not in _resp:
            self._ledger("issue_close", issue_number, issue_number)
        return _resp

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
            _resp = self._gitcode_request(
                "POST", f"/repos/{self.owner}/{self.repo}/pulls", data)
        elif self.provider == "github":
            _resp = self._github_request(
                "POST", f"/repos/{self.owner}/{self.repo}/pulls", data)
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            _resp = self._gitlab_request(
                "POST", f"/projects/{project_id}/merge_requests", data)
        else:
            return {"error": f"unsupported provider: {self.provider}"}
        # Issue #453 台账（写点11）：建 PR 入账——PR 编号（number/iid）为对账键
        if isinstance(_resp, dict) and "error" not in _resp:
            from src.outbound_ledger import extract_content_id
            self._ledger("pr", extract_content_id(_resp, "issue"), "", body)
        return _resp

    def merge_pr(self, pr_number: int) -> dict:
        """合并 Pull Request。"""
        if self.provider == "gitcode":
            _resp = self._gitcode_request(
                "PUT", f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/merge", {})
        elif self.provider == "github":
            _resp = self._github_request(
                "PUT", f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/merge", {})
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            _resp = self._gitlab_request(
                "PUT", f"/projects/{project_id}/merge_requests/{pr_number}/merge", {})
        else:
            return {"error": f"unsupported provider: {self.provider}"}
        # Issue #453 台账（写点11）：合并 PR 入账——pr_merge 事件回流对账
        if isinstance(_resp, dict) and "error" not in _resp:
            self._ledger("pr_merge", pr_number, "")
        return _resp

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

    def repo_exists(self) -> bool:
        """探测仓库是否存在（Issue #340 代码仓删除感知）。

        404 → False（已删除）；200 → True；其他错误（403/网络）→ True（不误判，
        交由探活轮询/webhook 双通道兜底）。
        """
        info = self.get_repo_info()
        if isinstance(info, dict) and str(info.get("error", "")).strip() == "HTTP 404":
            return False
        return True

    # === Issue #315：user-images 图片鉴权下载 ===

    def download_user_image(self, url: str, max_size: int = USER_IMAGE_MAX_SIZE,
                            timeout: int = USER_IMAGE_TIMEOUT) -> dict:
        """携带平台 token 下载 GitCode 编辑器上传图片（raw.gitcode.com/user-images/...）。

        匿名拉取该类资源返回 403；带 Bearer token 实测可 200 拉取。
        token 仅用于请求头，不进入日志与返回值。
        """
        return _download_user_image(url, self.token, max_size=max_size, timeout=timeout)

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



def create_pr_for_repo(repo_key: str, token: str, title: str, head: str, base: str,
                        body: str = "") -> dict:
    """为指定仓库创建 PR（Issue #242 多仓库 PR 提交）。

    根据 repo_key 前缀自动选择 VCS 平台，复用 VcsApiClient.create_pr。
    """
    from src.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    client = VcsApiClient(provider=provider, token=token, owner=owner, repo=repo)
    return client.create_pr(title=title, head=head, base=base, body=body)

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
